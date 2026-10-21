"""Comment CRUD + ownership rules (M5)."""
from conftest import add_entry, me


def test_user_can_edit_own_comment(two_users_shared):
    ca = two_users_shared["ca"]
    e = add_entry(ca, title="t")
    c = ca.post(f"/api/entries/{e['id']}/comments", json={"body": "v1"}).json()
    r = ca.put(f"/api/comments/{c['id']}", json={"body": "v2"})
    assert r.status_code == 200
    assert r.json()["body"] == "v2"
    assert r.json()["edited_at"] is not None


def test_user_can_delete_own_comment(two_users_shared):
    ca = two_users_shared["ca"]
    e = add_entry(ca, title="t")
    c = ca.post(f"/api/entries/{e['id']}/comments", json={"body": "v1"}).json()
    assert ca.delete(f"/api/comments/{c['id']}").status_code == 200


def test_user_cannot_edit_others_comment(two_users_shared):
    ca, cb = two_users_shared["ca"], two_users_shared["cb"]
    e = add_entry(ca, title="t")
    c = ca.post(f"/api/entries/{e['id']}/comments", json={"body": "v1"}).json()
    r = cb.put(f"/api/comments/{c['id']}", json={"body": "hacked"})
    assert r.status_code == 403


def test_member_cannot_delete_others_comment(two_users_shared):
    ca, cb = two_users_shared["ca"], two_users_shared["cb"]
    e = add_entry(ca, title="t")
    c = ca.post(f"/api/entries/{e['id']}/comments", json={"body": "v1"}).json()
    r = cb.delete(f"/api/comments/{c['id']}")
    assert r.status_code == 403


def test_owner_can_delete_others_comment(two_users_shared):
    """Workspace owner (alice) can moderate bob's comment."""
    ca, cb = two_users_shared["ca"], two_users_shared["cb"]
    e = add_entry(ca, title="t")
    bobs_comment = cb.post(f"/api/entries/{e['id']}/comments", json={"body": "spam"}).json()
    r = ca.delete(f"/api/comments/{bobs_comment['id']}")
    assert r.status_code == 200


def test_empty_comment_rejected(two_users_shared):
    ca = two_users_shared["ca"]
    e = add_entry(ca, title="t")
    r = ca.post(f"/api/entries/{e['id']}/comments", json={"body": "   "})
    assert r.status_code == 400


def test_comment_too_long_rejected(two_users_shared):
    ca = two_users_shared["ca"]
    e = add_entry(ca, title="t")
    r = ca.post(f"/api/entries/{e['id']}/comments", json={"body": "x" * 6000})
    assert r.status_code == 400


def test_comment_count_in_get_data(two_users_shared):
    ca = two_users_shared["ca"]
    e = add_entry(ca, title="t")
    ca.post(f"/api/entries/{e['id']}/comments", json={"body": "a"})
    ca.post(f"/api/entries/{e['id']}/comments", json={"body": "b"})
    data = ca.get("/api/data").json()
    row = [x for x in data["entries"] if x["id"] == e["id"]][0]
    assert row["comment_count"] == 2
