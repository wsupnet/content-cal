"""Cascading deletes: entry → comments+activity, channel shrink → entries+,
last member leaves → entire workspace gone (L7, M3)."""
import main
from sqlmodel import Session, select

from conftest import do_setup, do_signup, add_entry, get_data, me


def _row_counts_for_entry(entry_id: int) -> tuple[int, int]:
    with Session(main.engine) as s:
        c = s.exec(select(main.Comment).where(main.Comment.entry_id == entry_id)).all()
        a = s.exec(select(main.ActivityEvent).where(main.ActivityEvent.entry_id == entry_id)).all()
        return len(c), len(a)


def test_delete_entry_cascades_comments_and_activity(client):
    do_setup(client, "alice")
    e = add_entry(client, title="t", channel=0, status="idea")
    # Generate one comment and one activity event (status change).
    client.post(f"/api/entries/{e['id']}/comments", json={"body": "first"})
    client.put(f"/api/entries/{e['id']}", json={
        "title": "t", "channel": 0, "date": "", "status": "filming",
    })
    cc, ac = _row_counts_for_entry(e["id"])
    assert cc == 1 and ac >= 1
    assert client.delete(f"/api/entries/{e['id']}").status_code == 200
    cc2, ac2 = _row_counts_for_entry(e["id"])
    assert cc2 == 0 and ac2 == 0


def test_delete_channel_cascades_its_entries(client):
    do_setup(client, "alice")
    # Default seed has 3 channels. Add an entry pinned to channel 0, with a comment.
    e = add_entry(client, title="ch0-entry", channel=0, status="idea")
    client.post(f"/api/entries/{e['id']}/comments", json={"body": "hi"})
    e2 = add_entry(client, title="ch1-entry", channel=1, status="idea")
    # Delete channel 0.
    r = client.delete("/api/channels/0")
    assert r.status_code == 200
    # ch0-entry should be gone; ch1-entry remains, with channel reindexed to 0.
    remaining = {x["title"]: x for x in r.json()["entries"]}
    assert "ch0-entry" not in remaining
    assert remaining["ch1-entry"]["channel"] == 0
    # Comments for the dropped entry should be gone.
    cc, ac = _row_counts_for_entry(e["id"])
    assert cc == 0 and ac == 0


def test_last_member_leaving_deletes_workspace(make_client):
    """When the last member of a workspace removes themselves, all rows scoped
    to that workspace must be gone."""
    ca = make_client()
    a = do_setup(ca, "alice")
    ws_id = a["workspace"]["id"]
    a_uid = a["user"]["id"]
    add_entry(ca, title="will-be-orphaned", channel=0, status="idea")
    # Alice (sole member) leaves.
    r = ca.delete(f"/api/workspaces/{ws_id}/members/{a_uid}")
    assert r.status_code == 200
    # Workspace and all scoped rows are gone.
    with Session(main.engine) as s:
        assert s.get(main.Workspace, ws_id) is None
        assert s.exec(select(main.Entry).where(main.Entry.workspace_id == ws_id)).all() == []
        assert s.exec(select(main.Channel).where(main.Channel.workspace_id == ws_id)).all() == []
        assert s.exec(select(main.WorkspaceMember).where(main.WorkspaceMember.workspace_id == ws_id)).all() == []


def test_open_invites_revoked_when_workspace_deleted(make_client):
    ca = make_client()
    a = do_setup(ca, "alice")
    ws_id = a["workspace"]["id"]
    a_uid = a["user"]["id"]
    inv = ca.post(f"/api/workspaces/{ws_id}/invites", json={}).json()
    ca.delete(f"/api/workspaces/{ws_id}/members/{a_uid}")
    with Session(main.engine) as s:
        row = s.exec(select(main.WorkspaceInvite).where(main.WorkspaceInvite.code == inv["code"])).first()
        assert row is not None
        assert row.revoked_at is not None
