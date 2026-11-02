"""Server backups in the UI: admin-only list / back up now / download /
restore from a snapshot / restore from an uploaded file."""
import sqlite3

import pytest

import main
from conftest import do_login, do_setup, do_signup, me


@pytest.fixture
def backup_dir(tmp_path, monkeypatch):
    d = tmp_path / "backups"
    monkeypatch.setattr(main, "BACKUP_DIR", d)
    monkeypatch.setattr(main, "BACKUP_KEEP", 5)
    return d


@pytest.fixture
def admin(client, backup_dir):
    do_setup(client, "alice")
    return client


def usernames(path):
    con = sqlite3.connect(path)
    try:
        return sorted(r[0] for r in con.execute('SELECT username FROM "user"'))
    finally:
        con.close()


def test_only_the_first_account_is_admin(admin, make_client):
    bob = make_client()
    do_signup(bob, "bob")
    assert me(admin)["user"]["is_admin"] is True
    assert me(bob)["user"]["is_admin"] is False
    assert admin.get("/api/admin/backups").status_code == 200
    for method, url in [
        ("get", "/api/admin/backups"),
        ("post", "/api/admin/backups"),
        ("get", "/api/admin/backups/calendar-20200101T000000Z.db"),
        ("post", "/api/admin/backups/calendar-20200101T000000Z.db/restore"),
        ("post", "/api/admin/restore"),
    ]:
        assert getattr(bob, method)(url).status_code == 403, url


def test_back_up_now_then_list_and_download(admin):
    r = admin.post("/api/admin/backups")
    assert r.status_code == 200, r.text
    snap = r.json()
    listed = admin.get("/api/admin/backups").json()
    assert listed["keep"] == 5
    assert [b["name"] for b in listed["backups"]] == [snap["name"]]
    d = admin.get(f"/api/admin/backups/{snap['name']}")
    assert d.status_code == 200
    assert d.content.startswith(b"SQLite format 3\x00")
    assert snap["name"] in d.headers["content-disposition"]


def test_back_up_now_works_with_automatic_snapshots_off(admin, monkeypatch):
    monkeypatch.setattr(main, "BACKUP_KEEP", 0)
    assert admin.post("/api/admin/backups").status_code == 200


@pytest.mark.parametrize("name", ["..%2Fcalendar.db", "calendar.db", "calendar-20200101T000000Z.db"])
def test_unknown_or_unsafe_names_are_refused(admin, name):
    # An encoded "/" decodes into the path, so that one never even reaches the
    # restore route (the static mount answers 405).
    assert admin.get(f"/api/admin/backups/{name}").status_code == 404
    assert admin.post(f"/api/admin/backups/{name}/restore").status_code in (404, 405)


def test_restore_snapshot_rolls_back_and_signs_everyone_out(admin, make_client, backup_dir):
    snap = admin.post("/api/admin/backups").json()["name"]
    bob = make_client()
    do_signup(bob, "bob")  # made after the snapshot, so the restore removes it

    r = admin.post(f"/api/admin/backups/{snap}/restore")
    assert r.status_code == 200, r.text
    safety = r.json()["safety_backup"]
    # The data from just before the restore was kept, so it can be undone.
    assert usernames(backup_dir / safety) == ["alice", "bob"]

    assert admin.get("/api/me").status_code == 401
    assert bob.get("/api/me").status_code == 401
    assert bob.post("/api/auth/login", json={"username": "bob", "password": "password123"}).status_code == 401
    do_login(admin, "alice")
    assert me(admin)["user"]["username"] == "alice"


def test_restore_works_when_the_safety_snapshot_prunes_the_source(admin, monkeypatch):
    monkeypatch.setattr(main, "BACKUP_KEEP", 1)
    snap = admin.post("/api/admin/backups").json()["name"]
    # Make the safety snapshot's name differ from the source's.
    monkeypatch.setattr(main, "_utcnow", lambda: main.datetime(2099, 1, 1))
    r = admin.post(f"/api/admin/backups/{snap}/restore")
    assert r.status_code == 200, r.text


def test_restore_from_uploaded_file(admin, make_client):
    snap = admin.post("/api/admin/backups").json()["name"]
    data = admin.get(f"/api/admin/backups/{snap}").content
    do_signup(make_client(), "bob")
    r = admin.post("/api/admin/restore", content=data,
                   headers={"content-type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    assert usernames(main.DB_FILE) == ["alice"]
    assert not list(main.BACKUP_DIR.glob("restore-*.tmp"))


def test_upload_that_isnt_a_database_is_rejected_and_changes_nothing(admin, make_client):
    do_signup(make_client(), "bob")
    for junk in (b"not a database at all", b"SQLite format 3\x00" + b"\x00" * 200):
        r = admin.post("/api/admin/restore", content=junk,
                       headers={"content-type": "application/octet-stream"})
        assert r.status_code == 400, r.text
    assert usernames(main.DB_FILE) == ["alice", "bob"]
    assert admin.get("/api/me").status_code == 200  # nobody was signed out
    assert not list(main.BACKUP_DIR.glob("restore-*.tmp"))


def test_upload_of_a_database_without_accounts_is_rejected(admin, tmp_path):
    empty = tmp_path / "empty.db"
    con = sqlite3.connect(empty)
    for t in main.RESTORE_REQUIRED_TABLES:
        con.execute(f'CREATE TABLE "{t}" (id INTEGER)')
    con.commit()
    con.close()
    r = admin.post("/api/admin/restore", content=empty.read_bytes(),
                   headers={"content-type": "application/octet-stream"})
    assert r.status_code == 400
    assert "no accounts" in r.json()["detail"]


def test_empty_upload_is_rejected(admin):
    r = admin.post("/api/admin/restore", content=b"",
                   headers={"content-type": "application/octet-stream"})
    assert r.status_code == 400
