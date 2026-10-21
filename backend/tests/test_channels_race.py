"""Optimistic concurrency on PUT /api/channels via X-Channels-Version (H1)."""
from conftest import do_setup, get_data


CH = lambda name: {"name": name, "pillars": [], "color": "", "icon": ""}


def test_correct_version_succeeds_and_bumps(client):
    do_setup(client, "alice")
    v0 = get_data(client)["channels_version"]
    r = client.put(
        "/api/channels",
        json=[CH("A"), CH("B")],
        headers={"X-Channels-Version": str(v0)},
    )
    assert r.status_code == 200
    assert r.json()["channels_version"] == v0 + 1


def test_stale_version_returns_409(client):
    do_setup(client, "alice")
    v0 = get_data(client)["channels_version"]
    # Client A wins.
    r1 = client.put(
        "/api/channels",
        json=[CH("A")],
        headers={"X-Channels-Version": str(v0)},
    )
    assert r1.status_code == 200
    # Client B (still holding v0) loses.
    r2 = client.put(
        "/api/channels",
        json=[CH("B")],
        headers={"X-Channels-Version": str(v0)},
    )
    assert r2.status_code == 409


def test_invalid_version_header_rejected(client):
    do_setup(client, "alice")
    r = client.put(
        "/api/channels",
        json=[CH("A")],
        headers={"X-Channels-Version": "not-an-int"},
    )
    assert r.status_code == 400


def test_missing_header_still_accepted_for_backcompat(client):
    do_setup(client, "alice")
    r = client.put("/api/channels", json=[CH("A")])
    assert r.status_code == 200


def test_delete_channel_also_bumps_version(client):
    do_setup(client, "alice")
    v0 = get_data(client)["channels_version"]
    # Need ≥2 channels to delete one.
    client.put("/api/channels", json=[CH("A"), CH("B")], headers={"X-Channels-Version": str(v0)})
    v1 = get_data(client)["channels_version"]
    r = client.delete("/api/channels/0")
    assert r.status_code == 200
    assert r.json()["channels_version"] == v1 + 1
