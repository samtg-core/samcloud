"""Task 1472: an `online` service that stops reporting health reads `stale`, not
`online`, once its last report is older than SERVICE_STALE_AFTER_S. Computed on
read, so the stored status is untouched and the next report clears it."""
from datetime import datetime, timedelta, timezone

import db
import enrollment
import main
from test_registry import client, AuthedClient, _register_device, _register_service


def _svc(client, name="svc", port=8800):
    assert _register_device(client, "box").status_code == 200
    assert _register_service(client, name, "box", port).status_code == 200
    return f"box/{name}", AuthedClient(client._client, enrollment.generate_service_token(f"box/{name}"))


def _age(sid, seconds, fmt="iso"):
    at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    value = at.isoformat() if fmt == "iso" else at.strftime("%Y-%m-%d %H:%M:%S")   # both forms are in the table
    db.execute("UPDATE services SET last_health = ?, status = 'online' WHERE id = ?", (value, sid))


def _status(client, sid):
    return {s["id"]: s for s in client.get("/api/v1/services").json()}[sid]


def test_a_fresh_report_reads_online_with_its_age(client):
    sid, svc = _svc(client)
    assert svc.post(f"/api/v1/services/{sid}/health").status_code == 200
    row = _status(client, sid)
    assert row["status"] == "online" and 0 <= row["health_age_s"] < 60
    assert client.get(f"/api/v1/services/{sid}").json()["status"] == "online"


def test_an_old_report_reads_stale_in_the_list_and_the_entry(client):
    sid, _ = _svc(client)
    _age(sid, main.SERVICE_STALE_AFTER_S + 60)
    assert _status(client, sid)["status"] == "stale"
    assert client.get(f"/api/v1/services/{sid}").json()["status"] == "stale"
    assert db.query_one("SELECT status FROM services WHERE id = ?", (sid,))["status"] == "online"   # stored as is


def test_a_naive_timestamp_is_read_as_utc(client):
    sid, _ = _svc(client)
    _age(sid, main.SERVICE_STALE_AFTER_S + 60, fmt="naive")
    assert _status(client, sid)["status"] == "stale"
    _age(sid, 30, fmt="naive")
    assert _status(client, sid)["status"] == "online"


def test_just_inside_the_window_is_still_online(client):
    sid, _ = _svc(client)
    _age(sid, main.SERVICE_STALE_AFTER_S - 60)
    assert _status(client, sid)["status"] == "online"


def test_the_next_report_clears_stale(client):
    sid, svc = _svc(client)
    _age(sid, main.SERVICE_STALE_AFTER_S + 60)
    assert _status(client, sid)["status"] == "stale"
    assert svc.post(f"/api/v1/services/{sid}/health").status_code == 200
    assert _status(client, sid)["status"] == "online"


def test_only_online_ages_out(client):
    """A service that never reported, or set itself `maintenance`, keeps what it said."""
    sid, _ = _svc(client)
    assert _status(client, sid)["health_age_s"] is None
    before = _status(client, sid)["status"]
    assert before != "stale"
    db.execute("UPDATE services SET last_health = ?, status = 'maintenance' WHERE id = ?",
               ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(), sid))
    assert _status(client, sid)["status"] == "maintenance"


def test_status_filter_uses_the_status_as_read(client):
    sid, _ = _svc(client, "old", 8801)
    _register_service(client, "new", "box", 8802)
    _age(sid, main.SERVICE_STALE_AFTER_S + 60)
    _age("box/new", 10)
    online = [s["id"] for s in client.get("/api/v1/services?status=online").json()]
    stale = [s["id"] for s in client.get("/api/v1/services?status=stale").json()]
    assert "box/new" in online and sid not in online
    assert stale == [sid]
