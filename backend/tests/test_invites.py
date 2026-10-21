"""Invite preview + accept + revoke + atomic concurrent accept (M1)."""
from concurrent.futures import ThreadPoolExecutor
import threading

from conftest import do_setup, do_signup, me


def test_invite_preview_returns_workspace_name(client):
    a = do_setup(client, "alice", workspace_name="Studio A")
    ws_id = a["workspace"]["id"]
    inv = client.post(f"/api/workspaces/{ws_id}/invites", json={}).json()
    r = client.get(f"/api/invites/{inv['code']}")
    assert r.status_code == 200
    body = r.json()
    assert body["workspace_name"] == "Studio A"
    assert body["workspace_id"] == ws_id


def test_invite_preview_404_for_unknown(client):
    do_setup(client, "alice")
    assert client.get("/api/invites/does-not-exist").status_code == 404


def test_signup_with_invite_joins_workspace(make_client):
    ca = make_client()
    cb = make_client()
    a = do_setup(ca, "alice")
    ws_id = a["workspace"]["id"]
    inv = ca.post(f"/api/workspaces/{ws_id}/invites", json={}).json()
    do_signup(cb, "bob", invite_code=inv["code"])
    # Bob should have 2 workspaces: his personal one + alice's.
    wss = me(cb)["workspaces"]
    assert ws_id in [w["id"] for w in wss]


def test_revoked_invite_cannot_be_previewed_or_accepted(make_client):
    ca = make_client()
    cb = make_client()
    a = do_setup(ca, "alice")
    ws_id = a["workspace"]["id"]
    inv = ca.post(f"/api/workspaces/{ws_id}/invites", json={}).json()
    assert ca.delete(f"/api/invites/{inv['code']}").status_code == 200
    assert ca.get(f"/api/invites/{inv['code']}").status_code == 410
    # Bob signs up without the invite, then tries to accept the revoked code.
    do_signup(cb, "bob")
    r = cb.post(f"/api/invites/{inv['code']}/accept")
    assert r.status_code == 410


def test_used_invite_returns_410_on_second_accept(make_client):
    ca = make_client()
    cb = make_client()
    cc = make_client()
    a = do_setup(ca, "alice")
    ws_id = a["workspace"]["id"]
    inv = ca.post(f"/api/workspaces/{ws_id}/invites", json={}).json()
    do_signup(cb, "bob")
    r1 = cb.post(f"/api/invites/{inv['code']}/accept")
    assert r1.status_code == 200
    do_signup(cc, "carol")
    r2 = cc.post(f"/api/invites/{inv['code']}/accept")
    assert r2.status_code == 410


def test_concurrent_accept_only_one_wins(make_client):
    """Two users hit /accept simultaneously; exactly one gets 200, the other 410."""
    ca = make_client()
    cb = make_client()
    cc = make_client()
    a = do_setup(ca, "alice")
    ws_id = a["workspace"]["id"]
    inv = ca.post(f"/api/workspaces/{ws_id}/invites", json={}).json()
    do_signup(cb, "bob")
    do_signup(cc, "carol")

    barrier = threading.Barrier(2)

    def attempt(c):
        barrier.wait()
        return c.post(f"/api/invites/{inv['code']}/accept").status_code

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(attempt, cb)
        f2 = ex.submit(attempt, cc)
        results = sorted([f1.result(), f2.result()])
    assert results == [200, 410]
