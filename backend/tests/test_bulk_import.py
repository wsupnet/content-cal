"""POST /api/entries/bulk: row-level errors, ordering, partial success (M4)."""
from conftest import do_setup


def test_bulk_all_good_rows(client):
    do_setup(client, "alice")
    payload = [
        {"title": "row0", "channel": 0, "date": "", "status": "idea"},
        {"title": "row1", "channel": 0, "date": "", "status": "filming"},
    ]
    r = client.post("/api/entries/bulk", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert len(body["created"]) == 2
    assert body["errors"] == []


def test_bulk_partial_success_preserves_row_index(client):
    do_setup(client, "alice")
    payload = [
        {"title": "ok0", "channel": 0, "date": "", "status": "idea"},        # ok
        {"title": "bad1", "channel": 99, "date": "", "status": "idea"},      # bad channel
        {"title": "ok2", "channel": 0, "date": "", "status": "idea"},        # ok
        {"title": "bad3", "channel": 0, "date": "nope", "status": "idea"},   # bad date
        {"title": "bad4", "channel": 0, "date": "", "status": "bogus"},      # bad status
    ]
    r = client.post("/api/entries/bulk", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert {x["title"] for x in body["created"]} == {"ok0", "ok2"}
    err_indices = sorted(e["index"] for e in body["errors"])
    assert err_indices == [1, 3, 4]


def test_bulk_empty_returns_empty_lists(client):
    do_setup(client, "alice")
    r = client.post("/api/entries/bulk", json=[])
    assert r.status_code == 200
    assert r.json() == {"created": [], "errors": []}


def test_bulk_over_limit_rejected(client):
    do_setup(client, "alice")
    payload = [{"title": f"r{i}", "channel": 0, "date": "", "status": "idea"} for i in range(1001)]
    r = client.post("/api/entries/bulk", json=payload)
    assert r.status_code == 400
