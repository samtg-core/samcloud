#!/usr/bin/env python3
"""Status-only adapter deliveries retain context without starting inference."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="adapter-status-") as tmp:
        state = Path(tmp)
        spec = importlib.util.spec_from_file_location(
            "rail_status", os.environ.get("RAIL_UNDER_TEST") or
            REPO / "registry/helpers/samcloud-rail.py")
        rail = importlib.util.module_from_spec(spec)
        with patch("os.path.expanduser", side_effect=lambda p: str(state) + p[1:] if p.startswith("~") else p):
            spec.loader.exec_module(rail)
        rail.STATE_DIR = state
        rail.DRY = False
        rail.RUNTIME_STREAMS = False
        rail.RUNTIME = "test"
        rail.log = Mock()
        rail._api = Mock(side_effect=AssertionError("unexpected network"))
        rail._api_quiet = Mock()
        rail._thread_done = Mock(return_value=None)
        rail.close_signal = Mock(return_value=None)
        rail.post_binding = Mock(return_value=10)
        rail.release_dead_claims = Mock()
        rail.holder_alive = Mock(return_value=False)
        key = "thread-1"
        entry = {"key": key, "sid": "conversation-1", "holder": "adapter",
                 "state": "open", "pid": 100, "pids": [100]}
        reg = {key: entry}
        row = {"id": 1, "task_id": 10, "event_type": "task.updated",
               "actor": "peer", "payload": {"status": "done", "changed": ["status"]}}
        assert rail._informational_status(row)
        for payload in ({"priority": "high", "changed": ["priority"]},
                        {"tags": [], "changed": ["tags"]}):
            assert rail._informational_status({**row, "payload": payload})
        assert rail._informational_status({**row, "event_type": "thread.closed", "payload": {}})
        for event in ("task.assigned", "task.delegated", "task.commented", "new.event"):
            assert not rail._informational_status({**row, "event_type": event})
        for payload in (
            {"status": "done"}, {"status": "cancelled", "changed": ["status"]},
            {"status": "todo", "changed": ["status"]},
            {"status": "done", "changed": ["status", "detail"]},
            {"status": "done", "changed": ["status"], "body": "Please check"},
        ):
            assert not rail._informational_status({**row, "payload": payload})
        # A failed durable write must not consume the notification.
        with patch.object(rail, "_write_status_context", side_effect=OSError("disk full")):
            try:
                rail.dispatch(key, [row], reg)
                raise AssertionError("expected the persistence failure")
            except OSError:
                pass
        rail._api_quiet.assert_not_called()
        with patch.object(rail, "start_adapter_turn") as start:
            assert rail.dispatch(key, [row], reg) == "idle"
            start.assert_not_called()
        context = state / "threads" / key / "status-context.json"
        assert json.loads(context.read_text())["rows"] == [row]
        assert rail._api_quiet.call_args.args[1] == "/notifications/delivered"
        assert not any(c.args[1].endswith("/ack") for c in rail._api_quiet.call_args_list)
        # Re-imported processes see the persisted facts; repeated offering deduplicates.
        rail.dispatch(key, [row], reg)
        assert len(rail._read_status_context(key)["rows"]) == 1
        assert json.loads((context.parent / "adapter.jsonl").read_text().splitlines()[-1])["no_turn"] == "informational_status"
        # Closure while running marks closing, never closes another turn's binding.
        rail.close_signal.return_value = ("peer", "done")
        rail.holder_alive.return_value = True
        assert rail.dispatch(key, [row], reg) == "closing"
        assert entry["state"] == "closing"
        assert rail.post_binding.call_args.args[2] == "closing"
        rail.holder_alive.return_value = False
        assert rail.dispatch(key, [row], reg) == "closed"
        assert entry["state"] == "closed"
        # The real launch boundary includes retained facts and clears only after launch.
        rail._reply_target = Mock(return_value="task 11")
        rail._holder_env = Mock(return_value={})
        with patch.object(rail.subprocess, "Popen", side_effect=OSError("launch failed")):
            try:
                rail.start_adapter_turn(key, key, entry, "SUBSTANTIVE", [2], 10)
            except OSError:
                pass
        assert rail._read_status_context(key)["rows"]
        rail._whoami = Mock(return_value="test")
        rail._row_ref = Mock(return_value="task #10")
        with patch.object(rail.subprocess, "Popen", return_value=Mock(pid=200)) as popen:
            rail.start_adapter_turn(key, key, entry, "SUBSTANTIVE", [2], 10)
            argv = popen.call_args.args[0]
            prompt = Path(argv[argv.index("--prompt-file") + 1]).read_text()
        assert '"status": "done"' in prompt and "SUBSTANTIVE" in prompt
        assert '"changed": ["status"]' in prompt
        assert json.loads(argv[argv.index("--ids") + 1]) == [2]
        assert not rail._read_status_context(key)["rows"]
        # Mixed batches are substantive and reach normal dispatch unchanged.
        rail.close_signal.return_value = None
        rail._object_open = Mock(return_value=True)
        rail._key_parts = Mock(return_value=("task", 10))
        rail._plane_binding = Mock(return_value={"state": "closed"})
        with patch.object(rail, "dispatch_adapter", return_value="resume") as dispatch:
            message = {**row, "id": 2, "event_type": "task.commented", "payload": {"body": "Please check"}}
            assert rail.dispatch(key, [row, message], reg) == "resume"
            assert dispatch.call_args.args[2] == [row, message]
        # Dry runs never persist or receipt.
        rail.DRY = True
        rail._api_quiet.reset_mock()
        rail.dispatch(key, [row], reg)
        assert not rail._read_status_context(key)["rows"]
        rail._api_quiet.assert_not_called()
    print("ALL PASSED: adapter status context")


if __name__ == "__main__":
    main()
