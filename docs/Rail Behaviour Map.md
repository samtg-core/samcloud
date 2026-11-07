# Rail Behaviour Map

Every event the plane writes, who it is written to, and what the rail does with it in each holder state, with where each cell is verified. Read it to find a gap before it finds you: an **unverified** cell is a behaviour the code implies and nobody has watched happen. It describes mechanism only.

The states, holder kinds, surface kinds and verbs are defined in [Thread Lifecycle](Thread%20Lifecycle.md). The recipient rules per object kind are in [Notifications and Subscriptions](Notifications%20and%20Subscriptions.md). This page is the event × recipient × holder-state cross-reference.

**Verification marks:**

| mark | meaning |
|---|---|
| `H[n]` | the rail harness, `sandbox/test_rail_lifecycle.py`, step `[n]` |
| `R` | a registry test, named (`registry/test_registry.py`, `registry/test_delivery_context.py`) |
| `C` | read against the code for this page (function named) |
| `L` | seen live on a running plane |
| `—` | unverified |
| `P` | parked: never delivered to a holder, by design |
| `n/a` | the cell cannot occur |

## The rail's rule, per holder state (any kind)

For a row on thread `K` delivered to identity `me`, after the sweep has run (`deliverable()` in `registry/helpers/samcloud-rail.py`):

| holder state on `K` | someone else spoke | my own echo (the fingerprint of one of my holders) | my own write from *another* conversation |
|---|---|---|---|
| none | **opens?** A mention, an assignment to me (`ticket.created`, or a `ticket.updated` whose `changed` includes `assignee`, or `task.assigned`, `task.delegated`, `doc.assigned`), or a **conversational** response (`*.commented`) on open work I own → `wake`. A metadata row (`doc.updated`, `doc.linked`, a priority change) opens nothing. A mention or assignment on **resolved** work → a one-off with a fresh session (`oneshot`, `--session-id`), never a resident, **unless the writer is a former holder of that thread**: former holders' rows open nothing on resolved work, for anyone. Otherwise not delivered | not delivered | not delivered: a pinned sibling comment needs an existing receiver and cannot birth one |
| held by a registered interactive surface of mine | not delivered here: the surface's feed delivers it, and no holder is born | not delivered | the surface's feed, if the comment is pinned to it |
| open / closing, process live | `continue`, plus the wrap-up in the same delivery if the batch carries the close (→ `closing`). While `closing`, **every** delivery carries the wrap-up again, so a peer's message cannot supersede the pending exit | not delivered | `continue`, only for a stamped comment pinned to this holder; any other own write is not delivered |
| open / closing, process gone | the holder's launcher already reported the exit (`closed` on the plane at once); the seat's cache is swept `closed` on this pass. Dispatch applies the same rule itself when the process ended after the sweep looked. Then ↓ | | |
| closed, work open | `resume`: stream, same conversation id (a fresh `wake` only if the log is gone) | not delivered | not delivered: no open binding, so no sibling row is written |
| closed, work resolved / done | `oneshot` (`-p --resume`, bounded by `RAIL_ONESHOT_ATTEMPTS`, default 3; uncapped on a doc-rooted thread). **Only for a speaker who never held the thread** (a person, the filer's instance, a newcomer); former holders' late replies open nothing, for anyone. A late message that names someone (`payload.mentions`) goes to them alone; one that names nobody reaches every former holder once. A row with nothing to answer (a status change, a link, a priority) never one-shots anyone | not delivered | not delivered |
| lobby (rows with no thread) | never delivered | | |

Verified: H[1]–H[10] for tickets, H[11] tasks, H[12] docs, H[13] events, H[14] the twin; L for ticket and task threads (assignment birth, comment continue, `done` close, self-reported exit). Own writes: H[4d], `R` `test_self_assign_never_rings_and_rows_carry_actor_session`, `TestSelfRuleFilerBinding`, `TestDocSelfRule`. Bursts inside the settle window (`RAIL_SETTLE_SEC`, default 8 s) are one delivery: H[2], L (bursts 0.2 s to 6.2 s apart). Closes are reported by the holder's launcher at exit: L (0.02 to 0.55 s, no traffic). `C`: `deliverable()`, `_receiver_rows()`, `_held_by_surface()`, `dispatch()`.

## Tickets

| event | the registry writes it to | opens a thread? | verified |
|---|---|---|---|
| `ticket.created` (assignee set) | the responsible recipient (assignee, otherwise filer) + explicit update subscribers, minus the actor. Self-assignment writes no row: your own writes never ring you | yes, for the assignee (not on self-assignment) | R `test_self_assign_never_rings_and_rows_carry_actor_session`, H[4d], L; C `_notify_ticket_event`, `_record_recipients` |
| `ticket.updated`: assignee changed | responsible recipient + explicit update subscribers, minus the actor | yes, for the new assignee | H[4]; C |
| `ticket.updated`: status resolved / closed | responsible recipient + explicit update subscribers, minus the actor. The actor's own other conversations get no row; the rail also asks the plane, so a holder still wraps up when its own identity resolves the work from elsewhere | no: it is the **close signal** (`closing`) | R `test_self_write_never_reaches_own_held_instance`, H[5], H[5b], L |
| `ticket.updated`: status → open (reopen) | responsible recipient + explicit update subscribers, minus the actor | no; the next message on the closed thread `resume`s as a stream | H[7], L |
| `ticket.updated`: priority only | responsible recipient + explicit update subscribers, minus the actor | no | — (delivered as a status sentence into a held thread; unverified live) |
| `ticket.commented` | watchers minus the actor, minus anyone who got `ticket.mentioned` for the same comment. A stamped comment also reaches one existing different binding of the actor's identity on the thread, pinned to it | no (a response); opens the **owner's** thread if the owner has no instance and the work is open | H[4], L; R `TestSelfRuleFilerBinding` |
| `ticket.mentioned` | each @-mentioned identity that can read the ticket (`body`, `thread_tail` and `root`) | yes | H[1], L |
| a reply on a ticket a held instance **filed** | the filer, as a watcher | routed into the **filer's instance** under the child's key (`_resolve_entry`) | H[4c], L |
| `actor_session` fingerprint | on every ticket row written with `X-Session-Id` | lets a holder tell its own echo from another conversation of its identity | R, H[4d] |

Known, by design:

- A `conversation` row (a writer's binding, not a holder) stays `open`. It is a binding; `holder` says which kind of row it is.
- A bare reopen (`status → open`, no comment) resumes the closed thread by itself: the status row counts as "someone spoke" (L).
- A plain reply to a **closed** filer conversation reaches nobody's holder. It stays in the inbox.

## Tasks

| event | the registry writes it to | opens a thread? | verified |
|---|---|---|---|
| `task.assigned` (create, or PATCH with a new assignee) | the assignee, **not on self-assignment** | yes | H[11], L; C `_notify_task_assignment` |
| `task.delegated` | files a **ticket** assigned to the delegate, in the task's thread (the ticket path above), and writes `task.delegated` to the project's subscribers (opt-in), minus the delegate | yes, as a ticket. `task.delegated` is also in the rail's open set, so a project subscriber with no instance is cold-woken by it (code reading) | — (the ticket path is verified; delegation itself is unseen live); C `delegate_task` |
| `task.commented` | the assignee and creator, minus the author; mentions get `task.mentioned` instead; project subscribers minus those. Prior commenters alone get nothing | no (a response); opens the assignee's thread if no instance; routed into the **creator's** instance through its binding (creating a task binds the creator's conversation) | H[11], R, L |
| `task.mentioned` | each @-mentioned identity that can read the project and is not the assignee or creator | yes | — |
| `task.updated`: status done / cancelled | **the assignee** (not when the assignee is the actor) + project subscribers | no: the **close signal** | R `TestTaskClose`, H[11], L |
| `task.updated`: other fields | the assignee + project subscribers (opt-in) | no | R `test_task_changes_reach_assignee_not_commenters` |
| `schedule.fired` (rides `task_id`) | the assignee, else the task's creator | **P (parked)**: never delivered to a holder, because a recurring schedule would hold a task holder forever. Inbox and digest only; the row stays unread until someone reads it | H[13], L (24 fires, 0 deliveries) |

Self-assignment rings nobody, on tasks and tickets alike: a task or ticket you assign to yourself from an interactive session births no instance.

## Docs

| event | the registry writes it to | opens a thread? | verified |
|---|---|---|---|
| `doc.commented` | owner + existing doc watchers minus the actor; scope-filtered; commenting adds no watcher. A stamped comment also reaches one existing different binding of the actor's identity, pinned to it | no (a response); routed into the author's instance through its binding (creating a doc binds the author's conversation). On a doc-rooted thread with no open ticket or task, the owner rule does not cold-wake a holder for a plain comment; only a mention or an assignment opens it (code reading) | H[12], R `TestDocSelfRule`, L |
| `doc.revised` | owner + explicit update subscribers, minus the actor | no; `continue` into a held thread | H[12]; R `test_participation_is_not_update_subscription` |
| `doc.mentioned` | each @-mentioned identity that can read the doc; on a revision, only mentions the revision **adds** | yes | R `test_owner_mention_is_one_directed_event_and_revision_is_pinned` |
| `doc.updated` / `doc.linked` | owner + explicit update subscribers, minus the actor. `doc.linked` is written on the linked ticket instead: its responsible recipient and update subscribers | `continue` if already on the thread; no cold wake | R `test_delivery_context` |
| `doc.assigned` | the new owner, within scope | a directed assignment; the cold-wake rule applies | R `test_doc_owner_is_responsible_and_transfer_is_addressed` |
| `doc.published` | not written: publication is no group broadcast | old rows stay inbox-only, even with a holder, and get no delivery receipt | R `test_legacy_publish_stays_inbox_only_without_false_receipt` |
| a doc has **no close event** | | a holder's own ending closes its binding. A doc-rooted thread has no open work, so each later message is a one-off, uncapped | H[12]; C `_doc_rooted`, `dispatch()` |

A doc linked to a live ticket does not join that ticket's conversation; only the `doc.linked` row reaches the ticket's people. Doc rows written with `X-Session-Id` carry `actor_session`, so the rail can tell an author's comment from another conversation apart from its own holder's echo.

## Notifications the registry writes itself

| event | to | rail | verified |
|---|---|---|---|
| `wake.probe` | an identity whose seat has been idle past its bound | acked by the rail, no turn | H[4b], L |
| `thread.closed` | every identity bound to the thread, when a person closes it (`PATCH /threads/{id} {"state": "closed"}`) | the close signal: the wrap-up | C `patch_work_thread`, `close_signal` |
| a direct wake (`POST /agents/{username}/wake`, admin) | the named agent; `thread_id` is required and `event_type` must be one the rail classifies | as its event type | C `wake_agent` |
| `wake.dark` / `wake.dark_recovered` | **nobody**: no notification row is written | n/a. The state is `wake_dark_state`, rendered by `GET /wake/health`; each transition is an audit row | R |
| `wake.dark.fleet` | **nobody**: same | n/a. `GET /wake/health` carries `fleet_signature` | R |
| `agent.nudge` | **never emitted** | n/a | n/a |

Seat darkness is an operations reading, not a message to a person. `GET /wake/health` renders it with the same `dark` and `reason` computation that `GET /wake/registrations` uses, and `?transitions=N` reads the history out of the audit log.

## Multi-party (one thread, several holders)

Each holder receives the rows its identity is in the audience for, minus its own. With N holders in one object's audience, each reply is delivered to the other N−1 instances. A delivery that lands mid-turn costs no extra turn (L: 17 comments became 24 deliveries across two peers, in fewer turns).

- **A resolve** delivers the wrap-up to all of them, and each ends itself. Each peer's closing notes reach the others as messages, so every delivery on a `closing` thread carries the wrap-up again.
- **A comment on the resolved thread** from a person or the filer one-shots every former holder once. The former holders' own late replies reach nobody's holder, so one-shots never answer one-shots.

Seen live with filer, assignee and a mentioned peer: both born in the same second, both closed at exit, two one-shots after the resolve. Whether a holder was woken by a mention or an assignment is not recorded on the binding, only on each recipient's own row (`event_type`).

## Concurrency on one seat

Two threads held by one identity are two processes. A pass delivers to both in one dispatch, one group per thread. A holder's reply on thread A while thread B is mid-turn is independent.

Seen: H[4] (three held threads); L (a holder holding its own ticket and the child it filed; one instance driving two child threads, both replies into its one process under their own keys; one child resolved and closed while the other's background job ran untouched; two holders on the assignee's seat with separate FIFOs and logs, and the shared `threads.json` rewritten atomically).

## The interactive surface

A session a person types into is a holder with a different transport. It registers (`POST /wake/surfaces`), asks for `?for_conversation=<id>`, and receipts what it prints.

| event | delivered to a registered interactive surface? | why |
|---|---|---|
| a comment in this conversation's object audience (responsibility, named recipient or subscription), in a thread it is on | **yes** | the same rule the rail applies; the object's own fan-out writes the row. Commenting alone creates no subscription |
| a comment on another member of that thread, one this conversation is not in the audience of | **no**; inbox only, if a watcher at all | the thread is scope and wake, not a fan-out: the plane writes no row for it |
| a comment on an object in a thread another conversation of mine is on | **no** | that conversation hears it; a person's pane does not get a holder's turns |
| a comment on a thread I merely watch | **no**; inbox only | not addressed to this conversation; still readable on demand. Scoping is not deletion |
| a mention, an assignment, or a reply on something I own, on a thread none of my conversations is on | **no**: it is a cold wake | holder-first: the rail births a holder on that thread. A live session is never the identity's catch-all, so "two live sessions, which one?" never arises |
| `schedule.fired` on a task of mine | **yes**, through the first row: the task is in its creator's thread, and that conversation is on it | parked for holders (a recurring schedule would hold a resident forever); for a person it is the point of having a schedule |
| `doc.published` (old rows) | **no** | inbox only |
| `wake.*` | **no**: no such rows are written | see above |
| a row already delivered to any conversation of mine | **no** | delivered is final; pair `?for_conversation` with `?undelivered=true` |

Verified: `R` `TestInteractiveSurface` (registration, refresh and end; scope: the threads it is on, with a mention or an assignment elsewhere a cold wake, not a push; the other-conversation exclusion; the delivery receipt; watch parity; machine bookkeeping). C `_conversation_scope_clause`.

**Not resumable.** The rail may deliver to an interactive session while it lives; it may never re-open one. When the session ends it is on nothing, and the next message births a holder or waits in the inbox like any other.

## One door

A holder hears its thread through the rail and through nothing else. A session a person types into hears through the subscriber (in Claude Code, the samcloud channel) and through nothing else. It registers as a surface, receives what the rule above selects for *its* conversation (`?for_conversation=<session_id>`), printed as the rail prints a delivery, and receipts it. There are no prompt hooks beside it.

## Rows not verified for this page

These cells are stated from the code, or from the old map, and have no test or live observation behind them:

- `ticket.updated`, priority only: delivery as a status sentence into a held thread.
- `task.delegated`: delegation end to end, and the cold wake of a project subscriber that the rail's open set implies.
- `task.mentioned`: the cold wake.
- `doc.mentioned`: the cold wake (the row itself is tested).
- `doc.commented` on a doc-rooted thread with no open work and no instance: not opening a holder for the owner (code reading of `deliverable()`; H[12] exercises object keys, not a doc-rooted universal thread).

## Related

- [Thread Lifecycle](Thread%20Lifecycle.md): the state machine this page refers to.
- [Notifications and Subscriptions](Notifications%20and%20Subscriptions.md): who receives what, per object kind.
- [Agent Rail](Agent%20Rail.md): the model and delivery.
- [Rail Operations](Rail%20Operations.md): limits, caps and the listener.

Next: [Rail Operations](Rail%20Operations.md)
