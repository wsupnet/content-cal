import pathlib

import main
from conftest import do_setup, do_signup, do_login, me, DEFAULT_PW


def test_setup_required_true_on_empty_db(client):
    r = client.get("/api/auth/setup-required")
    assert r.status_code == 200
    assert r.json()["setup_required"] is True


def test_setup_required_false_after_setup(client):
    do_setup(client)
    r = client.get("/api/auth/setup-required")
    assert r.json()["setup_required"] is False


def test_setup_creates_user_workspace_and_session(client):
    data = do_setup(client, "alice", workspace_name="Studio")
    assert data["user"]["username"] == "alice"
    assert data["workspace"]["name"] == "Studio"
    # Session cookie should be set; /api/me works without re-logging in.
    info = me(client)
    assert info["user"]["username"] == "alice"
    assert info["active_workspace_id"] == data["workspace"]["id"]


def test_setup_refuses_second_run(client):
    do_setup(client, "alice")
    r = client.post("/api/auth/setup", json={"username": "bob", "password": DEFAULT_PW, "setup_code": main.SETUP_CODE})
    assert r.status_code == 400
    assert "already" in r.json()["detail"].lower()


def test_signup_creates_personal_workspace(client):
    do_setup(client, "alice")  # so signup isn't blocked by setup-required UX
    c2 = client.__class__(client.app)
    data = do_signup(c2, "bob")
    assert data["user"]["username"] == "bob"
    info = me(c2)
    # Bob has exactly one (personal) workspace.
    assert len(info["workspaces"]) == 1
    assert info["workspaces"][0]["name"].startswith("bob")


def test_signup_rejects_duplicate_username(client):
    do_setup(client, "alice")
    r = client.post("/api/auth/signup", json={"username": "alice", "password": DEFAULT_PW})
    assert r.status_code == 400


def test_signup_rejects_short_password(client):
    do_setup(client, "alice")
    r = client.post("/api/auth/signup", json={"username": "bob", "password": "short"})
    assert r.status_code == 400


def test_login_success(client, make_client):
    do_setup(client, "alice")
    fresh = make_client()
    data = do_login(fresh, "alice")
    assert data["user"]["username"] == "alice"
    # Cookie set → /api/me works.
    assert me(fresh)["user"]["username"] == "alice"


def test_login_bad_password(client, make_client):
    do_setup(client, "alice")
    fresh = make_client()
    r = fresh.post("/api/auth/login", json={"username": "alice", "password": "wrongpw11"})
    assert r.status_code == 401


def test_login_unknown_user(client):
    r = client.post("/api/auth/login", json={"username": "nobody", "password": DEFAULT_PW})
    assert r.status_code == 401


def test_logout_clears_session(client):
    do_setup(client, "alice")
    assert me(client)["user"]["username"] == "alice"
    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    r2 = client.get("/api/me")
    assert r2.status_code == 401


def test_me_without_cookie_is_401(client):
    r = client.get("/api/me")
    assert r.status_code == 401


def test_session_persists_across_requests(client):
    do_setup(client, "alice")
    for _ in range(3):
        assert client.get("/api/me").status_code == 200


def test_setup_log_warns_when_existing_data_was_expected(client, capsys):
    main._print_setup_code_if_needed()
    out = capsys.readouterr().out
    assert main.SETUP_CODE in out
    assert "CONTENTCAL_DATA" in out


def test_setup_screen_warns_when_existing_data_was_expected():
    # The test client serves a stub page, so read the real frontend file.
    html = (pathlib.Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text()
    assert "No existing accounts found." in html
    assert "CONTENTCAL_DATA" in html
