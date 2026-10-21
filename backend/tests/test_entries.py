"""Entry payload validation (review H5)."""
from conftest import do_setup, add_entry


def _setup(client):
    do_setup(client, "alice")


def test_channel_index_out_of_range_rejected(client):
    _setup(client)
    r = client.post("/api/entries", json={
        "title": "x", "channel": 99, "date": "", "status": "idea",
    })
    assert r.status_code == 400
    assert "channel" in r.json()["detail"].lower()


def test_negative_channel_index_rejected(client):
    _setup(client)
    r = client.post("/api/entries", json={"title": "x", "channel": -1, "date": "", "status": "idea"})
    assert r.status_code == 400


def test_malformed_date_rejected(client):
    _setup(client)
    r = client.post("/api/entries", json={
        "title": "x", "channel": 0, "date": "not-a-date", "status": "idea",
    })
    assert r.status_code == 400
    assert "iso" in r.json()["detail"].lower() or "date" in r.json()["detail"].lower()


def test_invalid_status_rejected(client):
    _setup(client)
    r = client.post("/api/entries", json={
        "title": "x", "channel": 0, "date": "", "status": "bogus",
    })
    assert r.status_code == 400


def test_scheduled_without_date_rejected(client):
    _setup(client)
    r = client.post("/api/entries", json={
        "title": "x", "channel": 0, "date": "", "status": "scheduled",
    })
    assert r.status_code == 400


def test_published_without_date_rejected(client):
    _setup(client)
    r = client.post("/api/entries", json={
        "title": "x", "channel": 0, "date": "", "status": "published",
    })
    assert r.status_code == 400


def test_empty_title_rejected(client):
    _setup(client)
    r = client.post("/api/entries", json={"title": "", "channel": 0, "status": "idea"})
    assert r.status_code == 400


def test_invalid_format_rejected(client):
    _setup(client)
    r = client.post("/api/entries", json={
        "title": "x", "channel": 0, "status": "idea", "format": "novel",
    })
    assert r.status_code == 400


def test_valid_idea_entry_created(client):
    _setup(client)
    e = add_entry(client, title="Hello world", channel=0, status="idea")
    assert e["title"] == "Hello world"
    assert e["status"] == "idea"


def test_update_entry_with_invalid_status_rejected(client):
    _setup(client)
    e = add_entry(client)
    r = client.put(f"/api/entries/{e['id']}", json={
        "title": "x", "channel": 0, "date": "", "status": "garbage",
    })
    assert r.status_code == 400
