"""Past-dated scheduled entries auto-flip to published on /api/data with a
user_id=0 system activity event, and only the active workspace is affected
(review H2)."""
from datetime import date, timedelta

from conftest import do_setup, do_signup, add_entry, get_data, me


def test_past_scheduled_flips_to_published(client):
    do_setup(client, "alice")
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    e = add_entry(client, title="past", channel=0, date=yesterday, status="scheduled")
    data = get_data(client)
    flipped = [x for x in data["entries"] if x["id"] == e["id"]][0]
    assert flipped["status"] == "published"


def test_future_scheduled_stays_scheduled(client):
    do_setup(client, "alice")
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    e = add_entry(client, title="future", channel=0, date=tomorrow, status="scheduled")
    data = get_data(client)
    still = [x for x in data["entries"] if x["id"] == e["id"]][0]
    assert still["status"] == "scheduled"


def test_auto_publish_emits_system_activity_event(client):
    do_setup(client, "alice")
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    e = add_entry(client, title="t", channel=0, date=yesterday, status="scheduled")
    get_data(client)  # triggers auto-publish
    feed = client.get(f"/api/entries/{e['id']}/feed").json()
    sys_events = [
        x for x in feed
        if x["kind"] == "activity"
        and x["event_type"] == "status_changed"
        and x["to_value"] == "published"
        and x["user_id"] == 0
    ]
    assert len(sys_events) == 1


def test_auto_publish_does_not_touch_other_workspaces(make_client):
    ca = make_client()
    cb = make_client()
    do_setup(ca, "alice")
    do_signup(cb, "bob")
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    # Both workspaces have a past-scheduled entry.
    e_a = add_entry(ca, title="a", date=yesterday, status="scheduled")
    e_b = add_entry(cb, title="b", date=yesterday, status="scheduled")
    # Alice fetches /api/data — only her entry flips.
    a_data = get_data(ca)
    a_entry = [x for x in a_data["entries"] if x["id"] == e_a["id"]][0]
    assert a_entry["status"] == "published"
    # Bob hasn't fetched yet, but querying his data should still show his entry
    # was scheduled until *his* /api/data is hit. After hitting it, it flips,
    # but the important invariant: alice's call must not have touched bob's
    # entry, so until bob calls /api/data the row is still scheduled. Verify
    # via a direct DB check.
    import main
    from sqlmodel import Session, select
    with Session(main.engine) as session:
        row = session.get(main.Entry, e_b["id"])
        assert row.status == "scheduled"
