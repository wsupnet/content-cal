"""User B (in a different workspace) cannot read or mutate user A's data."""
from conftest import do_setup, do_signup, me, add_entry, get_data


def _setup_two_isolated(make_client):
    ca = make_client()
    cb = make_client()
    a = do_setup(ca, "alice")
    do_signup(cb, "bob")
    return ca, cb, a["workspace"]["id"]


def test_get_data_returns_only_own_workspace(make_client):
    ca, cb, _ = _setup_two_isolated(make_client)
    add_entry(ca, title="alice-secret", channel=0, status="idea")
    # Bob's /api/data must not see alice's entry.
    b_data = get_data(cb)
    assert not any(e["title"] == "alice-secret" for e in b_data["entries"])


def test_b_cannot_get_a_entry_feed(make_client):
    ca, cb, _ = _setup_two_isolated(make_client)
    e = add_entry(ca, title="alice-entry")
    r = cb.get(f"/api/entries/{e['id']}/feed")
    assert r.status_code == 404


def test_b_cannot_put_a_entry(make_client):
    ca, cb, _ = _setup_two_isolated(make_client)
    e = add_entry(ca)
    r = cb.put(f"/api/entries/{e['id']}", json={
        "title": "hijacked", "channel": 0, "date": "", "status": "idea",
        "pillar": "", "notes": "", "format": "", "link": "",
    })
    assert r.status_code == 404


def test_b_cannot_delete_a_entry(make_client):
    ca, cb, _ = _setup_two_isolated(make_client)
    e = add_entry(ca)
    r = cb.delete(f"/api/entries/{e['id']}")
    assert r.status_code == 404
    # Sanity: entry still there for A.
    a_data = get_data(ca)
    assert any(x["id"] == e["id"] for x in a_data["entries"])


def test_b_cannot_comment_on_a_entry(make_client):
    ca, cb, _ = _setup_two_isolated(make_client)
    e = add_entry(ca)
    r = cb.post(f"/api/entries/{e['id']}/comments", json={"body": "hi"})
    assert r.status_code == 404


def test_b_cannot_edit_or_delete_a_comment(make_client):
    ca, cb, _ = _setup_two_isolated(make_client)
    e = add_entry(ca)
    cm = ca.post(f"/api/entries/{e['id']}/comments", json={"body": "hello"}).json()
    r1 = cb.put(f"/api/comments/{cm['id']}", json={"body": "hacked"})
    r2 = cb.delete(f"/api/comments/{cm['id']}")
    assert r1.status_code == 404
    assert r2.status_code == 404


def test_b_cannot_list_a_members_or_invites(make_client):
    ca, cb, ws_id = _setup_two_isolated(make_client)
    assert cb.get(f"/api/workspaces/{ws_id}/members").status_code == 403
    assert cb.get(f"/api/workspaces/{ws_id}/invites").status_code == 403


def test_b_cannot_create_or_revoke_a_invite(make_client):
    ca, cb, ws_id = _setup_two_isolated(make_client)
    # B can't mint invites for A's workspace.
    assert cb.post(f"/api/workspaces/{ws_id}/invites", json={}).status_code == 403
    # A creates one; B can't revoke it.
    inv = ca.post(f"/api/workspaces/{ws_id}/invites", json={}).json()
    assert cb.delete(f"/api/invites/{inv['code']}").status_code == 403


def test_b_cannot_remove_a_members(make_client):
    ca, cb, ws_id = _setup_two_isolated(make_client)
    a_uid = me(ca)["user"]["id"]
    r = cb.delete(f"/api/workspaces/{ws_id}/members/{a_uid}")
    assert r.status_code == 403


def test_b_cannot_rename_a_workspace(make_client):
    ca, cb, ws_id = _setup_two_isolated(make_client)
    r = cb.put(f"/api/workspaces/{ws_id}", json={"name": "owned"})
    assert r.status_code == 403


def test_b_goal_is_independent(make_client):
    ca, cb, _ = _setup_two_isolated(make_client)
    ca.put("/api/goal", json={"name": "alice goal", "target": 10, "deadline": ""})
    cb.put("/api/goal", json={"name": "bob goal", "target": 5, "deadline": ""})
    assert get_data(ca)["goal"]["name"] == "alice goal"
    assert get_data(cb)["goal"]["name"] == "bob goal"


def test_b_cannot_switch_into_a_workspace(make_client):
    ca, cb, ws_id = _setup_two_isolated(make_client)
    r = cb.put("/api/me/active-workspace", json={"workspace_id": ws_id})
    assert r.status_code == 403
