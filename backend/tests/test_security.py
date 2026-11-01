"""Regression tests for the security hardening pass: username charset, auth
throttling, spoofed X-Forwarded-For, session expiry/revocation, and the
request body cap."""
import pathlib

import main
from conftest import do_setup, do_signup, DEFAULT_PW


# ── Usernames ───────────────────────────────────────────────────────
def test_signup_rejects_script_username(client):
    do_setup(client, "alice")
    r = client.post("/api/auth/signup", json={
        "username": "evil'),alert(1),('", "password": DEFAULT_PW,
    })
    assert r.status_code == 400


def test_setup_rejects_markup_username(client):
    r = client.post("/api/auth/setup", json={"username": "<img src=x>", "password": DEFAULT_PW, "setup_code": main.SETUP_CODE})
    assert r.status_code == 400


def test_plain_usernames_still_allowed(make_client):
    do_setup(make_client(), "alice")
    do_signup(make_client(), "bob.smith_2-x")


def test_existing_odd_username_can_still_log_in(client):
    # Accounts created before the charset rule keep working.
    from sqlmodel import Session
    with Session(main.engine) as s:
        s.add(main.User(username="old name!", password_hash=main.hash_password(DEFAULT_PW)))
        s.commit()
    r = client.post("/api/auth/login", json={"username": "old name!", "password": DEFAULT_PW})
    # 500 = logged in but no workspace; anything but 400/401 proves the name passed.
    assert r.status_code not in (400, 401)


# ── Login throttling ────────────────────────────────────────────────
def _login(c, username="alice", password=DEFAULT_PW):
    return c.post("/api/auth/login", json={"username": username, "password": password})


def _client_at(ip):
    from fastapi.testclient import TestClient
    return TestClient(main.app, client=(ip, 50000))


def test_failures_lock_that_account_from_that_ip(reset_db):
    do_setup(_client_at("10.0.0.1"), "alice")
    attacker = _client_at("10.0.0.66")
    for _ in range(main.LOGIN_MAX_FAILS_PER_ACCOUNT_IP):
        assert _login(attacker, password="wrongwrong").status_code == 401
    assert _login(attacker).status_code == 429  # even the right password


def test_lock_from_one_ip_does_not_block_another_ip(reset_db):
    do_setup(_client_at("10.0.0.1"), "alice")
    attacker = _client_at("10.0.0.66")
    for _ in range(main.LOGIN_MAX_FAILS_PER_ACCOUNT_IP + 2):
        _login(attacker, password="wrongwrong")
    assert _login(_client_at("10.0.0.2")).status_code == 200


def test_known_device_survives_lock_on_its_own_ip(reset_db):
    owner = _client_at("10.0.0.1")
    do_setup(owner, "alice")                  # owner's browser gets a device cookie
    attacker = _client_at("10.0.0.1")         # same shared IP (NAT, Docker Desktop…)
    for _ in range(main.LOGIN_MAX_FAILS_PER_ACCOUNT_IP + 2):
        _login(attacker, password="wrongwrong")
    assert _login(attacker).status_code == 429
    assert _login(owner).status_code == 200


def test_distributed_attack_hits_account_wide_cap_but_not_known_device(reset_db, monkeypatch):
    monkeypatch.setattr(main, "LOGIN_MAX_FAILS_PER_ACCOUNT", 5)
    owner = _client_at("10.0.0.1")
    do_setup(owner, "alice")
    for i in range(5):
        _login(_client_at(f"10.1.0.{i}"), password="wrongwrong")
    assert _login(_client_at("10.2.0.1")).status_code == 429   # unknown browser
    assert _login(owner).status_code == 200                    # known device


def test_case_variant_failures_do_not_lock_the_real_account(reset_db):
    do_setup(_client_at("10.0.0.1"), "alice")
    attacker = _client_at("10.0.0.1")
    for _ in range(main.LOGIN_MAX_FAILS_PER_ACCOUNT_IP + 2):
        _login(attacker, username="ALICE", password="wrongwrong")
    assert _login(_client_at("10.0.0.1")).status_code == 200


def test_case_variant_usernames_cannot_be_registered(make_client):
    do_setup(make_client(), "alice")
    r = make_client().post("/api/auth/signup", json={"username": "Alice", "password": DEFAULT_PW})
    assert r.status_code == 400


def test_successful_logins_do_not_count_toward_lock(client):
    do_setup(client, "alice")
    for _ in range(main.LOGIN_MAX_FAILS_PER_ACCOUNT_IP + 2):
        assert _login(client).status_code == 200


def test_login_ip_cap(client, monkeypatch):
    monkeypatch.setattr(main, "LOGIN_MAX_PER_IP", 5)
    do_setup(client, "alice")
    codes = [_login(client, username=f"user{i}", password="x").status_code for i in range(6)]
    assert codes == [401] * 5 + [429]


def test_client_ip_ignores_forwarded_for_header():
    from starlette.requests import Request
    scope = {
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [(b"x-forwarded-for", b"6.6.6.6"), (b"x-real-ip", b"7.7.7.7")],
        "client": ("10.0.0.5", 1234),
    }
    assert main._client_ip(Request(scope)) == "10.0.0.5"


def test_signup_ip_cap(make_client, monkeypatch):
    monkeypatch.setattr(main, "SIGNUP_MAX_PER_IP", 3)
    do_setup(make_client(), "alice")
    c = make_client()
    codes = [
        c.post("/api/auth/signup", json={"username": f"user{i}", "password": DEFAULT_PW}).status_code
        for i in range(4)
    ]
    assert codes == [200, 200, 200, 429]


# ── Rate limiter bookkeeping ────────────────────────────────────────
def test_peek_does_not_create_bucket(reset_db):
    assert main._check_rate("peek-only", max_hits=1, window_seconds=60, record=False)
    assert "peek-only" not in main._RATE_BUCKETS


def test_sweep_keeps_buckets_still_inside_their_own_window(reset_db, monkeypatch):
    import time
    now = time.monotonic()
    main._RATE_BUCKETS["long"] = [900, main.deque([now - 120])]   # 2 min old, 15-min window
    main._RATE_BUCKETS["short"] = [60, main.deque([now - 120])]   # 2 min old, 1-min window
    main._sweep_rate_buckets(now)
    assert "long" in main._RATE_BUCKETS and "short" not in main._RATE_BUCKETS


def test_sweep_caps_total_buckets(reset_db, monkeypatch):
    import time
    monkeypatch.setattr(main, "_RATE_MAX_KEYS", 100)
    now = time.monotonic()
    for i in range(150):
        main._RATE_BUCKETS[f"k{i}"] = [900, main.deque([now])]
    main._sweep_rate_buckets(now)
    assert len(main._RATE_BUCKETS) == 100 and "k149" in main._RATE_BUCKETS


# ── Sessions ────────────────────────────────────────────────────────
def test_copied_cookie_stops_working_after_logout(make_client):
    c = make_client()
    do_setup(c, "alice")
    stolen = make_client()
    stolen.cookies.set(main.SESSION_COOKIE, c.cookies.get(main.SESSION_COOKIE))
    assert stolen.get("/api/me").status_code == 200
    c.post("/api/auth/logout")
    assert stolen.get("/api/me").status_code == 401


def test_login_after_logout_works(client):
    do_setup(client, "alice")
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"username": "alice", "password": DEFAULT_PW}).status_code == 200
    assert client.get("/api/me").status_code == 200


def test_expired_cookie_rejected(client, monkeypatch):
    do_setup(client, "alice")
    monkeypatch.setattr(main, "SESSION_MAX_AGE", -1)
    assert client.get("/api/me").status_code == 401


def test_old_untimed_cookie_rejected(client):
    from itsdangerous import URLSafeSerializer
    do_setup(client, "alice")
    old = URLSafeSerializer(main.SESSION_SECRET, salt="session-v1").dumps({"uid": 1, "wsid": 1})
    client.cookies.clear()
    client.cookies.set(main.SESSION_COOKIE, old)
    assert client.get("/api/me").status_code == 401


# ── Body size cap ───────────────────────────────────────────────────
def test_oversized_body_rejected(client):
    big = "a" * (main.MAX_REQUEST_BYTES + 1)
    r = client.post("/api/auth/login", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_oversized_chunked_body_rejected(client):
    def gen():
        chunk = b"a" * 65536
        for _ in range(main.MAX_REQUEST_BYTES // len(chunk) + 2):
            yield chunk
    r = client.post("/api/auth/login", content=gen(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


# ── No third-party requests from the frontend ──────────────────────
def test_frontend_loads_no_external_scripts_or_styles():
    import re
    html = (pathlib.Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text()
    external = re.findall(r'<(?:script|link|img|iframe)\b[^>]*\b(?:src|href)=["\']?(?:https?:)?//', html, re.I)
    external += re.findall(r'(?:@import|url\()\s*["\']?(?:https?:)?//', html, re.I)
    assert not external, external


def test_frontend_js_makes_no_outbound_calls():
    import re
    frontend = pathlib.Path(__file__).resolve().parents[2] / "frontend"
    html = (frontend / "index.html").read_text()
    sources = {"index.html <script>": "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))}
    for js in sorted((frontend / "vendor").glob("*.js")):
        sources[js.name] = js.read_text()
    allowed_urls = {"http://www.w3.org/2000/svg"}
    for name, code in sources.items():
        for api in ("XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource"):
            assert api not in code, (name, api)
        if name != "index.html <script>":
            assert "fetch(" not in code, (name, "fetch(")
        urls = set(re.findall(r"https?://[^\s\"'`)<>]+", code)) - allowed_urls
        assert not urls, (name, urls)
    # index.html's own fetch() calls must all go through apiFetch to this origin.
    for target in re.findall(r"\bfetch\(([^,)]+)", sources["index.html <script>"]):
        assert target.strip().startswith("API+"), target


# ── Members can't change workspace structure ───────────────────────
def test_member_cannot_change_channels_or_goal(two_users_shared):
    cb = two_users_shared["cb"]
    assert cb.put("/api/channels", json=[{"name": "pwned", "pillars": []}]).status_code == 403
    assert cb.delete("/api/channels/0").status_code == 403
    assert cb.put("/api/goal", json={"name": "pwned", "target": 1}).status_code == 403


def test_owner_can_still_change_channels_and_goal(two_users_shared):
    ca = two_users_shared["ca"]
    chans = ca.get("/api/data").json()["channels"]
    assert ca.put("/api/channels", json=chans).status_code == 200
    assert ca.put("/api/goal", json={"name": "50 videos", "target": 50}).status_code == 200


def test_member_can_still_manage_videos(two_users_shared):
    cb = two_users_shared["cb"]
    e = cb.post("/api/entries", json={"title": "t", "channel": 0, "status": "idea"})
    assert e.status_code == 200
    assert cb.delete(f"/api/entries/{e.json()['id']}").status_code == 200


def test_member_sees_no_invite_codes(two_users_shared):
    two_users_shared["ca"].post(f"/api/workspaces/{two_users_shared['ws_id']}/invites", json={})
    assert two_users_shared["cb"].get(f"/api/workspaces/{two_users_shared['ws_id']}/invites").json() == []
    assert two_users_shared["ca"].get(f"/api/workspaces/{two_users_shared['ws_id']}/invites").json()


def test_me_reports_role(two_users_shared):
    roles = lambda c: {w["id"]: w["role"] for w in c.get("/api/me").json()["workspaces"]}
    ws = two_users_shared["ws_id"]
    assert roles(two_users_shared["ca"])[ws] == "owner"
    assert roles(two_users_shared["cb"])[ws] == "member"


# ── Channel icons ───────────────────────────────────────────────────
def test_remote_icon_rejected(client):
    do_setup(client, "alice")
    chans = client.get("/api/data").json()["channels"]
    chans[0]["icon"] = "https://tracker.example/pixel.png"
    assert client.put("/api/channels", json=chans).status_code == 400


def test_uploaded_icon_accepted(client):
    do_setup(client, "alice")
    chans = client.get("/api/data").json()["channels"]
    chans[0]["icon"] = "data:image/png;base64,iVBORw0KGgo="
    assert client.put("/api/channels", json=chans).status_code == 200


def test_legacy_remote_icon_hidden_and_save_still_works(client):
    from sqlmodel import Session, select
    do_setup(client, "alice")
    with Session(main.engine) as s:
        ch = s.exec(select(main.Channel).where(main.Channel.position == 0)).first()
        ch.icon = "https://tracker.example/pixel.png"
        s.add(ch)
        s.commit()
    chans = client.get("/api/data").json()["channels"]
    assert chans[0]["icon"] == ""
    assert client.put("/api/channels", json=chans).status_code == 200


# ── API docs / first-run setup ─────────────────────────────────────
def test_api_docs_disabled(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_setup_requires_code(client):
    body = {"username": "mallory", "password": DEFAULT_PW}
    assert client.post("/api/auth/setup", json=body).status_code == 403
    assert client.post("/api/auth/setup", json={**body, "setup_code": "WRONG123"}).status_code == 403
    assert client.get("/api/auth/setup-required").json()["setup_required"] is True
    assert client.post("/api/auth/setup", json={**body, "setup_code": main.SETUP_CODE.lower()}).status_code == 200


# ── Security headers ───────────────────────────────────────────────
def test_security_headers_on_api_and_pages(client):
    for path in ("/api/auth/setup-required", "/"):
        h = client.get(path).headers
        assert h["x-content-type-options"] == "nosniff"
        assert h["x-frame-options"] == "DENY"
        assert h["referrer-policy"] == "no-referrer"
        csp = h["content-security-policy"]
        assert "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp


# ── Cookie flags ────────────────────────────────────────────────────
def test_cookie_not_secure_by_default(client):
    r = client.post("/api/auth/setup", json={"username": "alice", "password": DEFAULT_PW, "setup_code": main.SETUP_CODE})
    assert "secure" not in r.headers["set-cookie"].lower()


def test_cookie_secure_when_enabled(client, monkeypatch):
    monkeypatch.setattr(main, "COOKIE_SECURE", True)
    r = client.post("/api/auth/setup", json={"username": "alice", "password": DEFAULT_PW, "setup_code": main.SETUP_CODE})
    assert "secure" in r.headers["set-cookie"].lower()


# ── Invite-only sign-up ─────────────────────────────────────────────
def _row_counts():
    from sqlmodel import Session, select, func
    with Session(main.engine) as s:
        return {t.__name__: s.exec(select(func.count()).select_from(t)).one()
                for t in (main.User, main.Workspace, main.WorkspaceMember, main.Channel)}


def test_invite_only_blocks_plain_signup(make_client, monkeypatch):
    monkeypatch.setattr(main, "ALLOW_OPEN_SIGNUP", False)
    do_setup(make_client(), "alice")
    c = make_client()
    assert c.get("/api/auth/setup-required").json()["open_signup"] is False
    assert c.post("/api/auth/signup", json={"username": "stranger", "password": DEFAULT_PW}).status_code == 403


def test_invite_only_bad_invite_creates_no_account(make_client, monkeypatch):
    monkeypatch.setattr(main, "ALLOW_OPEN_SIGNUP", False)
    do_setup(make_client(), "alice")
    before = _row_counts()
    c = make_client()
    r = c.post("/api/auth/signup", json={"username": "stranger", "password": DEFAULT_PW, "invite_code": "bogus"})
    assert r.status_code == 410
    assert c.post("/api/auth/login", json={"username": "stranger", "password": DEFAULT_PW}).status_code == 401
    assert _row_counts() == before


def test_invite_only_valid_invite_works(make_client, monkeypatch):
    monkeypatch.setattr(main, "ALLOW_OPEN_SIGNUP", False)
    ca = make_client()
    ws = do_setup(ca, "alice")["workspace"]["id"]
    code = ca.post(f"/api/workspaces/{ws}/invites", json={}).json()["code"]
    cb = make_client()
    do_signup(cb, "bob", invite_code=code)
    assert cb.get("/api/me").json()["active_workspace_id"] == ws


# ── Username probing by timing ──────────────────────────────────────
def test_unknown_user_login_runs_a_password_check(client, monkeypatch):
    calls = []
    real = main.verify_password
    monkeypatch.setattr(main, "verify_password", lambda pw, h: calls.append(h) or real(pw, h))
    client.post("/api/auth/login", json={"username": "nobody", "password": DEFAULT_PW})
    assert calls == [main._DUMMY_HASH]


# ── Data file permissions ──────────────────────────────────────────
def test_database_not_world_readable():
    import os, stat, sys
    if sys.platform.startswith("win"):
        return
    main.init_db()
    assert not os.stat(main.DB_FILE).st_mode & (stat.S_IRWXG | stat.S_IRWXO)


# ── H1: request guard ───────────────────────────────────────────────
def test_public_route_body_cap(client):
    big = '{"username":"' + "a" * (main.PUBLIC_BODY_BYTES + 10) + '","password":"x"}'
    r = client.post("/api/auth/login", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_anonymous_body_to_login_required_route_is_not_parsed(client):
    # Malformed JSON would be a 422 if it were parsed; the guard answers 401 first.
    r = client.post("/api/entries", content=b"{" * 100_000, headers={"Content-Type": "application/json"})
    assert r.status_code == 401


def test_forged_session_cookie_is_rejected_before_body(client):
    client.cookies.set(main.SESSION_COOKIE, "forged.value")
    r = client.post("/api/entries", content=b"{" * 100_000, headers={"Content-Type": "application/json"})
    assert r.status_code == 401


def test_authenticated_normal_route_cap(client):
    do_setup(client, "alice")
    big = b'{"title":"' + b"a" * (main.DEFAULT_BODY_BYTES + 10) + b'"}'
    r = client.post("/api/entries", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_validation_errors_do_not_echo_input(client):
    do_setup(client, "alice")
    marker = "ECHO-MARKER-" + "x" * 2000
    r = client.post("/api/entries", json={"title": 123, "channel": marker, "status": marker})
    assert r.status_code == 422
    assert "ECHO-MARKER" not in r.text
    assert len(r.content) < 2000


def test_huge_bulk_array_rejected_before_item_validation(client):
    do_setup(client, "alice")
    r = client.post("/api/entries/bulk", json=[{}] * 5000)
    assert r.status_code == 400
    assert len(r.content) < 500


def test_bulk_reports_bad_rows_individually(client):
    do_setup(client, "alice")
    r = client.post("/api/entries/bulk", json=[{"title": "ok", "channel": 0, "status": "idea"}, {"nope": 1}])
    assert r.status_code == 200
    body = r.json()
    assert len(body["created"]) == 1 and [e["index"] for e in body["errors"]] == [1]


def test_channel_save_with_six_max_size_icons_fits(client):
    do_setup(client, "alice")
    icon = "data:image/png;base64," + "A" * (main.MAX_ICON_LEN - 30)
    chans = [{"name": f"C{i}", "pillars": [], "icon": icon} for i in range(6)]
    assert client.put("/api/channels", json=chans).status_code == 200


# ── M3: sign-up doesn't reveal usernames ────────────────────────────
def test_bogus_invite_same_response_for_existing_and_new_usernames(make_client, monkeypatch):
    monkeypatch.setattr(main, "ALLOW_OPEN_SIGNUP", False)
    do_setup(make_client(), "alice")
    responses = [
        make_client().post("/api/auth/signup", json={"username": u, "password": DEFAULT_PW, "invite_code": "bogus"})
        for u in ("alice", "nobody_here")
    ]
    assert [(r.status_code, r.json()) for r in responses][0] == [(r.status_code, r.json()) for r in responses][1]
    assert responses[0].status_code == 410


# ── M4: per-install cookie names ────────────────────────────────────
def test_cookie_name_is_unique_per_install(client):
    assert main.SESSION_COOKIE.startswith(("contentcal_session_", "__Host-contentcal_session_"))
    assert main.SESSION_COOKIE != main.LEGACY_SESSION_COOKIE
    r = client.post("/api/auth/setup", json={"username": "alice", "password": DEFAULT_PW, "setup_code": main.SETUP_CODE})
    cookies = r.headers.get_list("set-cookie")
    assert any(c.startswith(main.SESSION_COOKIE + "=") for c in cookies)
    assert any(c.startswith(main.LEGACY_SESSION_COOKIE + "=") and "max-age=0" in c.lower() for c in cookies)


# ── L1 / L4: roles reach the client ─────────────────────────────────
def test_new_workspace_response_includes_owner_role(client):
    do_setup(client, "alice")
    assert client.post("/api/workspaces", json={"name": "Second"}).json()["role"] == "owner"


def test_data_reports_current_role(two_users_shared):
    assert two_users_shared["ca"].get("/api/data").json()["role"] == "owner"
    assert two_users_shared["cb"].get("/api/data").json()["role"] == "member"


# ── L10: a failing bulk row doesn't undo earlier rows ───────────────
def test_bulk_db_failure_keeps_earlier_rows(client, monkeypatch):
    do_setup(client, "alice")
    existing = client.post("/api/entries", json={"title": "existing", "channel": 0, "status": "idea"}).json()["id"]
    real = main._validate_entry_payload

    def collide_on_dup(session, ws_id, entry):
        v = real(session, ws_id, entry)
        if entry.title == "dup":
            v["id"] = existing  # primary-key clash -> genuine IntegrityError on insert
        return v

    monkeypatch.setattr(main, "_validate_entry_payload", collide_on_dup)
    rows = [{"title": t, "channel": 0, "status": "idea"} for t in ("first", "dup", "third")]
    body = client.post("/api/entries/bulk", json=rows).json()
    assert [e["title"] for e in body["created"]] == ["first", "third"]
    assert [e["index"] for e in body["errors"]] == [1]
    titles = sorted(e["title"] for e in client.get("/api/data").json()["entries"])
    assert titles == ["existing", "first", "third"]


# ── L11: password change ────────────────────────────────────────────
def test_password_change_requires_current_password(client):
    do_setup(client, "alice")
    r = client.post("/api/me/password", json={"current_password": "wrongwrong", "new_password": "newpass123"})
    assert r.status_code == 403
    assert _login(client).status_code == 200


def test_password_change_signs_out_other_devices_only(make_client):
    here = make_client()
    do_setup(here, "alice")
    other = make_client()
    _login(other)
    r = here.post("/api/me/password", json={"current_password": DEFAULT_PW, "new_password": "newpass123"})
    assert r.status_code == 200
    assert here.get("/api/me").status_code == 200
    assert other.get("/api/me").status_code == 401
    assert _login(make_client(), password="newpass123").status_code == 200
    assert _login(make_client()).status_code == 401


def test_password_change_rejects_short_password(client):
    do_setup(client, "alice")
    r = client.post("/api/me/password", json={"current_password": DEFAULT_PW, "new_password": "short"})
    assert r.status_code == 400
