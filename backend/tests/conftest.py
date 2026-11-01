"""Shared fixtures and helpers for the ContentCal regression test suite.

Critical: this module sets DATA_DIR / SESSION_SECRET env vars and
monkey-patches `fastapi.staticfiles.StaticFiles` BEFORE importing the app, so
that (a) the production sqlite DB at /app/data/calendar.db is never touched, and
(b) the hardcoded `directory="/frontend"` mount in main.py doesn't blow up on
the test runner's machine.
"""
from __future__ import annotations

import os
import sys
import pathlib
import tempfile

import pytest

# ── Test environment bootstrap (must happen before importing main) ──
_TMP_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="ccal_test_"))
_DATA_DIR = _TMP_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_FRONTEND_STUB = _TMP_ROOT / "frontend_stub"
_FRONTEND_STUB.mkdir(parents=True, exist_ok=True)
(_FRONTEND_STUB / "index.html").write_text("<!-- test stub -->")

os.environ["DATA_DIR"] = str(_DATA_DIR)
os.environ.pop("REQUIRE_EXISTING_DB", None)
os.environ["SESSION_SECRET"] = "test-secret-do-not-use-anywhere-real"
# Most tests create standalone accounts; invite-only mode has its own tests.
os.environ["ALLOW_OPEN_SIGNUP"] = "1"

# Patch StaticFiles so the hardcoded "/frontend" directory in main.py doesn't
# crash at import time on a dev box.
import fastapi.staticfiles as _sf  # noqa: E402
_orig_sf_init = _sf.StaticFiles.__init__
def _patched_sf_init(self, *args, **kwargs):  # type: ignore[no-redef]
    kwargs["directory"] = str(_FRONTEND_STUB)
    kwargs.setdefault("check_dir", True)
    return _orig_sf_init(self, *args, **kwargs)
_sf.StaticFiles.__init__ = _patched_sf_init  # type: ignore[assignment]

# Add backend/ to sys.path so `import main` works regardless of where pytest is run.
_BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402


# ── Per-test DB reset ───────────────────────────────────────────────
@pytest.fixture
def reset_db():
    """Drop and recreate every table before each test so tests are
    independent. The engine itself is reused across tests (cheap)."""
    SQLModel.metadata.drop_all(main.engine)
    SQLModel.metadata.create_all(main.engine)
    main._RATE_BUCKETS.clear()
    # Seed legacy workspace_id=0 default channels exactly the way the app's
    # startup hook does, so the first user's setup adopts them and ends up
    # with a workspace that has the default 3-channel grid.
    from sqlmodel import Session
    with Session(main.engine) as s:
        for i, ch in enumerate(main.DEFAULT_CHANNELS):
            s.add(main.Channel(position=i, name=ch["name"], pillars=ch["pillars"]))
        s.commit()
    yield


@pytest.fixture
def client(reset_db):
    """Single TestClient with its own cookie jar."""
    return TestClient(main.app)


@pytest.fixture
def make_client(reset_db):
    """Factory for additional TestClients (each with an independent cookie
    jar) so multi-user scenarios can be exercised in a single test."""
    def _factory():
        return TestClient(main.app)
    return _factory


# ── Helpers ─────────────────────────────────────────────────────────
DEFAULT_PW = "password123"


def do_setup(c: TestClient, username: str = "alice", password: str = DEFAULT_PW,
             workspace_name: str | None = None) -> dict:
    payload = {"username": username, "password": password, "setup_code": main.SETUP_CODE}
    if workspace_name:
        payload["workspace_name"] = workspace_name
    r = c.post("/api/auth/setup", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def do_signup(c: TestClient, username: str, password: str = DEFAULT_PW,
              invite_code: str | None = None) -> dict:
    payload: dict = {"username": username, "password": password}
    if invite_code:
        payload["invite_code"] = invite_code
    r = c.post("/api/auth/signup", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def do_login(c: TestClient, username: str, password: str = DEFAULT_PW) -> dict:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def me(c: TestClient) -> dict:
    r = c.get("/api/me")
    assert r.status_code == 200, r.text
    return r.json()


def get_data(c: TestClient) -> dict:
    r = c.get("/api/data")
    assert r.status_code == 200, r.text
    return r.json()


def add_entry(c: TestClient, **kwargs) -> dict:
    payload = {
        "title": kwargs.get("title", "Test entry"),
        "channel": kwargs.get("channel", 0),
        "date": kwargs.get("date", ""),
        "status": kwargs.get("status", "idea"),
        "pillar": kwargs.get("pillar", ""),
        "notes": kwargs.get("notes", ""),
        "format": kwargs.get("format", ""),
        "link": kwargs.get("link", ""),
    }
    r = c.post("/api/entries", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


# ── Two-user-in-shared-workspace fixture ────────────────────────────
@pytest.fixture
def two_users_shared(make_client):
    """user_a is owner of workspace W; user_b joined W via invite. Both are
    pointed at workspace W in their session. Returns (ca, cb, ws_id, a_info, b_info)."""
    ca = make_client()
    cb = make_client()
    a = do_setup(ca, "alice")
    ws_id = a["workspace"]["id"]
    inv = ca.post(f"/api/workspaces/{ws_id}/invites", json={}).json()
    do_signup(cb, "bob", invite_code=inv["code"])
    return {
        "ca": ca, "cb": cb, "ws_id": ws_id,
        "a_user_id": a["user"]["id"],
        "b_user_id": me(cb)["user"]["id"],
        "invite_code": inv["code"],
    }
