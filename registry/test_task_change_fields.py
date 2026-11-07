"""Task notifications expose which fields were edited for delivery policy."""
import json

import db
import pytest
from fastapi.testclient import TestClient
from test_registry import client, _get_app, _invite_and_activate


@pytest.mark.parametrize("edit", [
    {"status": "done"},
    {"status": "done", "detail": "A changed requirement"},
    {"status": "done", "scheduled_at": ""},
])
def test_task_notification_preserves_changed_fields(client, edit):
    with TestClient(_get_app()) as raw:
        _invite_and_activate(client, raw, "status-reader", [])
    slug = client.post("/api/v1/projects", json={"name": "status metadata"}).json()["slug"]
    task = client.post(f"/api/v1/projects/{slug}/tasks",
                       json={"title": "task", "assignee": "status-reader"}).json()["id"]
    result = client.patch(f"/api/v1/tasks/{task}", json=edit)
    assert result.status_code == 200, result.text
    rows = db.query("SELECT payload FROM notifications WHERE task_id = ? "
                    "AND event_type = 'task.updated' AND recipient = ?", (task, "status-reader"))
    assert len(rows) == 1
    assert set(json.loads(rows[0]["payload"])["changed"]) == set(edit)
