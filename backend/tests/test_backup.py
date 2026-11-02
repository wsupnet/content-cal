"""Daily database snapshots (main.backup_db)."""
import sqlite3

import pytest

import main
from conftest import do_setup


@pytest.fixture
def backup_dir(tmp_path, monkeypatch):
    d = tmp_path / "backups"
    monkeypatch.setattr(main, "BACKUP_DIR", d)
    monkeypatch.setattr(main, "BACKUP_KEEP", 3)
    return d


def test_snapshot_is_a_complete_readable_copy(client, backup_dir):
    do_setup(client, "alice")
    path = main.backup_db()
    assert path is not None and path.parent == backup_dir
    con = sqlite3.connect(path)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        names = [r[0] for r in con.execute("SELECT username FROM user")]
    finally:
        con.close()
    assert names == ["alice"]


def test_keeps_only_the_newest_snapshots(reset_db, backup_dir):
    backup_dir.mkdir()
    old = [backup_dir / f"calendar-2020010{i}T000000Z.db" for i in range(1, 6)]
    for f in old:
        f.write_bytes(b"")
    newest = main.backup_db()
    kept = sorted(p.name for p in backup_dir.glob("calendar-*.db"))
    assert kept == sorted([old[3].name, old[4].name, newest.name])


def test_cleans_up_interrupted_snapshot(reset_db, backup_dir):
    backup_dir.mkdir()
    stale = backup_dir / "calendar-20200101T000000Z.db.tmp"
    stale.write_bytes(b"half written")
    main.backup_db()
    assert not stale.exists()
    assert not list(backup_dir.glob("*.tmp"))


def test_keep_zero_turns_snapshots_off(reset_db, backup_dir, monkeypatch):
    monkeypatch.setattr(main, "BACKUP_KEEP", 0)
    assert main.backup_db() is None
    assert not backup_dir.exists()
