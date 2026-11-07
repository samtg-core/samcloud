#!/usr/bin/env python3
"""RFC 385/386: exercise both real renderers without a plane or inference."""
import ast
import tempfile
from pathlib import Path
from test_rail_tail_on_wake_only import load

ROOT = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as td:
    rail = load(Path(td))
    rail._api = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network"))
    rail._whoami = lambda: "reviewer"
    rail._thread_info = lambda key: {"root": "doc:386", "owners": ["reviewer"], "open_members": True}
    rail._thread_title = lambda key, rows: "delivery context"
    rail._footer = lambda key: "FOOTER"
    sibling = "Other open conversations of your identity: [abcdef12] thread 17 task #7 (process recorded)"
    pending = "Other undelivered inbox events for your identity: 2; GET /api/v1/notifications?unread=true"
    record = {"id": 1, "actor": "peer", "doc_id": 386, "thread_id": 18,
              "event_type": "doc.revised", "created_at": "2026-10-12 07:00:00",
              "read_object": "GET /api/v1/docs/386?rev=2", "siblings_text": sibling, "pending_inbox_text": pending,
              "payload": {"title": "delivery context", "rev": 2}}
    comment = {**record, "id": 2, "event_type": "doc.commented",
               "payload": {"body": "Keep this exact criterion: answer only the question."}}
    for prompt in (rail.wake_prompt("thread-18", [record, comment], False),
                   rail.wake_prompt("thread-18", [record, comment], True),
                   rail.continue_prompt("thread-18", [record, comment]),
                   rail.oneshot_prompt("thread-18", [record, comment], fresh=True)):
        assert prompt.count(sibling) == 1
        assert prompt.count(pending) == 1
        assert record["read_object"] in prompt
        assert comment["payload"]["body"] in prompt
    no_siblings = {**comment, "siblings_text": ""}
    assert "Other open conversations" not in rail.continue_prompt("thread-18", [no_siblings])
    publish = {**record, "id": 3, "event_type": "doc.published"}
    assert rail._receiver_rows([publish, comment], {"sid": "receiver"}, "reviewer") == [comment]
    assert rail._receiver_rows([publish], None, "reviewer") == []

    # Run the subscriber's real pure formatting functions and group-render loop.
    # Its top-level networking loop is deliberately not imported.
    tree = ast.parse((ROOT / "registry/helpers/samcloud-subscribe.py").read_text())
    loop = next(n for n in tree.body if isinstance(n, ast.While))
    defs = [n for n in loop.body if isinstance(n, ast.FunctionDef)
            and n.name in {"_sentence", "_message"}]
    render = next(n for n in loop.body if isinstance(n, ast.For)
                  and isinstance(n.target, ast.Tuple))
    emitted = []
    ns = {"groups": {"thread-18": [record, comment]},
          "_ref": lambda key: "thread 18", "_obj": lambda r: "doc 386",
          "_thread": lambda key: {"root": "doc:386", "title": "delivery context"},
          "_held": lambda key: {"conversation_id": "receiver"},
          "_emit": lambda text, key: emitted.append(text)}
    exec(compile(ast.Module(body=defs + [render], type_ignores=[]), "subscriber-render", "exec"), ns)
    assert len(emitted) == 1 and emitted[0].count(sibling) == 1
    assert emitted[0].count(pending) == 1
    assert record["read_object"] in emitted[0]
    assert comment["payload"]["body"] in emitted[0]
    ns["_obj"] = lambda r: "task 7" if r.get("task_id") else "doc 386"
    ns["_held"] = lambda key: None
    latest = {"id": 3, "actor": "peer", "task_id": 7, "event_type": "task.commented",
              "payload": {"body": "a different task", "title": "current task"}}
    ns["groups"] = {"thread-18": [record, latest]}
    exec(compile(ast.Module(body=[render], type_ignores=[]), "subscriber-render", "exec"), ns)
    assert emitted[-1].startswith("Shared conversation — thread 18 · latest on task 7: current task\n")
    # An untitled new object must not borrow the preceding object's title.
    latest["payload"].pop("title")
    exec(compile(ast.Module(body=[render], type_ignores=[]), "subscriber-render", "exec"), ns)
    assert emitted[-1].startswith("Shared conversation — thread 18\n")
    assert "latest on task 7: delivery context" not in emitted[-1]
    changed = {"actor": "peer", "task_id": 7, "event_type": "task.updated",
               "payload": {"status": "todo", "assignee": "reviewer", "changed": ["detail"]}}
    for sentence in (rail._status_sentence, ns["_sentence"]):
        assert sentence(changed) == "peer updated the task (detail)"
        done = {**changed, "payload": {"status": "done", "changed": ["status"]}}
        assert sentence(done) == "peer marked done the task"
        noted = {**record, "payload": {"rev": 2, "note": "Review wording"}}
        assert sentence(noted) == "peer revised the doc (rev 2); author's note: Review wording"

print("ALL PASSED: shared sibling context, record references, comments and publish exclusion")
