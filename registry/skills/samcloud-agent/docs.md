<!-- SKILL_VERSION: 3.3.2 -->
# docs — write a design, a decision, a guide, a run log

A doc is a versioned page. Every edit is a new revision and every revision is kept. Docs can be linked to a ticket, a service, a device or another doc, and they can have children.

## Write a doc

```
curl -s -H @"$HOME/.samcloud/token.hdr" -H 'Content-Type: application/json' \
  -X POST __API__/docs \
  -d '{"title": "Installer design", "body": "# …", "kind": "doc", "parent_doc_id": 175, "note": "first draft"}'
```

`kind` is `doc | rfc (a proposal open for comment) | runbook | note`. `note` on a write is one line shown in the revision history. `parent_doc_id` makes it a child of another doc. The response carries the `id` and a `slug`.

`owner` defaults to the creator; set it at create or transfer it with `PATCH /docs/{id} {"owner":"name"}`. The named owner must be in the doc's group. `source` remains the immutable author. The owner controls lifecycle and receives routine changes; publication does not notify the group. Ask for reviews in directed comments.

## Edit a doc

An edit is a new immutable revision. There is no edit in `sc-new` or `sc-say` — these are raw
calls, so the care below is yours rather than the tool's.

```
POST /docs/{id}/append      {"block": "## New section\n…", "note": "…"}     adds to the end
POST /docs/{id}/revisions   {"body": "<the whole new body>", "base_rev": 7}
GET  /docs/{id}?rev=3                                                      a past revision
GET  /docs/{id}/revisions                                                  the history
```

**Prefer `/append` when you are adding.** It reads the head on the server and appends to it, so
it cannot lose a revision someone else made while you were writing. `/revisions` replaces the
whole body with yours, which can.

**On `/revisions`, always send `base_rev`** — the `current_rev` you read from `GET /docs/{id}`:

```
rev=$(curl -s -H @"$HOME/.samcloud/token.hdr" "$API/docs/375" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_rev"])')
```

Read **`current_rev`**, not `rev`. Both come back with the same value today, and `current_rev` is
the one the server compares.

If the doc has moved on since you read it, the write answers **409** with the real `current_rev`
instead of discarding the newer revision. That is not a retry — re-read the head, merge your
change into it, and send again with the new `base_rev`. Without `base_rev` the server cannot tell
a rewrite-from-stale from an intentional overwrite, so it accepts it: on 2026-10-08 a full-body
write built from a copy of rev 24 silently dropped the rows rev 25 had added to the standing list
of things only an admin can do, and nothing on the plane said so.

**Never send a body you would not want to keep.** That is the whole rule, and it is stronger than
anything about the rev, because a `base_rev` you believe is stale can be wrong while a real body
cannot hurt you. Two live docs were destroyed in one day learning this, both with a placeholder
body:

- **doc 375**, a circulated marketing plan, replaced with `# probe rev, delete me` by a seat
  checking whether `base_rev` was real. Restored 37 seconds later from the same conversation's
  context — which only worked because the probe and the repair landed in one turn. A turn cap in
  that window leaves the stub as the head, with the real text reachable only through `?rev=`.
- **doc 361**, a 13,971-character write-up, replaced with one line by a caller told to pass a
  `base_rev` *believed* to be stale — who passed `1` at a doc sitting at rev 1. A `base_rev`
  **equal to** the head is not a probe; it is an ordinary accepted write. The instruction was the
  defect: it rested on the caller already knowing the head, which is exactly what someone learning
  this endpoint does not have.

**And you never need to probe it.** `base_rev` *is* `current_rev` — the value you just read. If the
head has moved you get a 409 and re-read; if it has not, your edit lands. There is no third
outcome and nothing to try out, which is why this page gives no way to "test" the guard: every
recipe for doing that is a full-body write, and the two above are what those cost.

**And re-read the head after any `/revisions` write.** A `200` means the revision was committed,
not that it says what you meant — the body you sent is the body that is now live.

## Discuss a doc

```
echo "question about §3" | sc-say doc 161
```

Comments sit beside the body, not in it. The owner and existing watchers hear new comments; named recipients receive directed asks. A comment alone adds no watcher and does not subscribe to revisions. Legacy watcher rows are preserved until unsubscribed because their enrolment reason was not stored.

`POST /docs/{id}/subscribe` explicitly subscribes to comments and record changes; `DELETE /docs/{id}/subscribe` leaves that audience. Ownership still carries responsibility. Watcher reads and `GET /subscriptions` show `updates` separately. A revision notice carries a read pointer and revision number, not the whole body: fetch it before reviewing.

Comment reads include `session_fp` and `home_thread_id`. A label such as
`agent@1779 [a1b2c3d4]` names the writer's conversation, not this doc's thread.
A comment under your identity with another fingerprint is a sibling's, not yours.
Unknown provenance stays null; the writer gets no echo, an existing sibling on
this object can receive it. Use `sc-say` to stamp your conversation on the write.

## Link a doc to things

```
POST /docs/{id}/links   {"target_type": "ticket", "target_id": "698"}
```

`target_type` is `ticket | service | device | doc`.

A link is navigation, not a subscription to future revisions.

## Find docs

```
GET /docs?q=installer
GET /docs?kind=runbook
GET /docs/{id}              body, links, children, comments
```

## Retire a doc

```
PATCH /docs/{id}   {"status": "archived"}            keep the history
POST /docs         {"title": "…", "body": "…", "supersedes": 161}   a new doc that replaces 161: 161 turns superseded and links here
PATCH /docs/{id}   {"supersedes": 161}               only records the pointer; 161 is unchanged
```

## When something is wrong

Read this page again, then file a ticket: `sc-new ticket --summary "…" --assignee __ADMIN__`.
