# Thread Lifecycle

This page is the rail's state machine: the four things that have state, how they move, and what the rail does in each state when something happens. It is the one place the states, holder kinds, surface kinds and dispatch verbs are defined. `sandbox/docs_coverage.py` reads those vocabularies out of the code that enforces them (`_THREAD_STATES`, `_RAIL_HOLDERS`, `_SURFACE_KINDS`, `DISPATCH_VERBS`) and fails if this page does not name every value. It describes mechanism only. For the model in plain words, start at [Agent Rail](Agent%20Rail.md).

## The one sentence

**A thread is the unit of work; a conversation is the addressee; delivery is receipt; nothing runs on a timer.** Everything below follows from those four.

## The model

<!-- model -->
**A thread is shared conversation context, not a ticket or an assignment.** The first thing a conversation makes starts its thread; everything it makes after joins it. Its origin is background, not everyone's current job. A conversation is on a thread or it is not; "held" is not a state. A thread is open while any ticket or task in it is open, or until a person closes it. Comments reach their object's responsible people, named recipients and subscribers. Commenting alone does not subscribe its writer. Routine record changes reach the responsible recipient and explicit update subscribers: a task’s assignee, a ticket’s assignee (otherwise its filer), or a doc’s owner. Group membership and thread presence subscribe nobody. A mention, an assignment or a reply on something you own, on a thread none of your conversations is on, is a cold wake: the rail starts a holder there, and a live session is never handed it.
<!-- /model -->

That paragraph is stated once, here. `sandbox/docs_coverage.py` requires it verbatim in the served `samcloud-agent` skill (`registry/skills/samcloud-agent/SKILL.md`) and the other places a reader first meets the word "thread".

How a conversation gets on a thread:

| how | who |
|---|---|
| by creating in it | any conversation: its first create starts its own thread, later creates join it |
| by being woken on it | a rail holder |
| by writing on it | a person's session, only when no open or closing binding of its identity is already there |

A holder is on one thread. Its write on another thread is a message there and binds nothing.

Being on a thread decides two things: where the conversation's creates land, and that it is the one woken there. What it hears is decided object by object, by each object's own audience ([Notifications and Subscriptions](Notifications%20and%20Subscriptions.md)). The thread is scope for wake and routing, not a fan-out: a member of your thread that you are not on reaches you nowhere.

Delivery framing follows the same split. A delivery is headed with the shared thread id and the latest message's object, not the title of the thread's first object. A cold mention can carry a read pointer to the thread's origin, labelled as background. The audience split, read pointers and sibling snapshot that the rail and the channel share are described in [Agent Rail](Agent%20Rail.md#what-a-delivery-carries).

A one-off comment records authorship and conversation provenance. It does not enrol its writer for future comments. Explicit subscriptions, named recipients and responsibility route messages. Existing ticket and doc watcher rows stay as they are, because the reason each was created is not stored. Task comment history is not a recipient list. To bring a prior contributor back, mention them; to follow task activity, subscribe to the project.

## Conversation provenance

An identity is an account; each conversation has its own history. Comments on tickets, docs and tasks carry `session_fp` and `home_thread_id`.

- **The home** is the thread of the conversation's first rail birth, or of its first create for an interactive session. It is not the thread of the object being discussed.
- **A separate origin record** (`conversation_origins`) survives binding replacement and object moves.
- **Unknown stays unknown.** Ambiguous origins and unstamped comments stay null. A comment is never attributed after the fact to whoever holds the thread now.

The label is `identity@home-thread [fingerprint]`. It is attribution, not an address or a permission. The API never exposes another identity's raw session id in a comment. The fingerprint is a short display hash (the first 8 hex characters of a SHA-256 of the conversation id), not an authentication key.

Self-echo suppression for comments is per conversation:

- A stamped comment can reach one *existing, different* open or closing binding of the writer's identity on the object's thread, within the object's audience. The row is pinned to that receiver's fingerprint (`recipient_session`).
- The writer, unrelated conversations and later replacement holders do not receive it. A sibling comment never starts a holder.
- Unstamped self-writes and every non-comment self event (an assignment, a status change) stay suppressed. Your own writes never ring you.

## The four lifecycles

| | states | who moves it | where it lives |
|---|---|---|---|
| **thread**: the work | `open` → `closed` | its last open ticket or task resolving, or a person's `PATCH /threads/{id}` | `work_threads` |
| **binding**: who holds it | `open` → `closing` → `closed` | the rail at birth, wrap-up and exit; the plane on a surface's goodbye | `threads` |
| **surface**: a conversation that can receive | `registered` → `ended` | the conversation itself, on purpose | `surfaces` |
| **holder**: what is reading | born → alive → gone | the OS | a process, or a person's terminal |

`closing` is not always traversed: a process that dies goes `open` → `closed` in one step, observed by the sweep. There is no state for "was working and stopped", because there is no deadline to be late against. A thread lives while its work is open, whether that is a minute or a month.

Only a person or an admin closes a thread outright. An agent token that tries gets `403`.

### What keeps each kind of object's thread open

| kind | responsible | open while | ends on |
|---|---|---|---|
| ticket | assignee (otherwise filer) | status open | status → `resolved` or `closed` |
| task | assignee | status not `done` or `cancelled` | status → `done` or `cancelled`; the assignee is notified, project subscribers are opt-in |
| doc | owner (defaults to the author) | never "open work": docs do not count toward `open_members` | no close event; a holder's own ending closes its binding |

`GET /threads/{id}` answers `open_members`: true while any ticket or task in the thread is open. That flag, or a person's close, is the whole thread lifecycle. A doc-rooted thread with no open ticket or task has no open work by definition, so it is never "done" for want of it, and each message on it is answered once (see [The verbs](#the-verbs)).

A response on an object is delivered to the responsible identity's live instance on that thread if there is one, else it cold-starts one. Before close, a thread is a **stream**; after close, a later message is a **one-shot** native resume. The rail only notices endings: a thread is held while its process lives, and a gone process is a closed binding, whatever ended it. The rail notices on the identity's next event, not at the instant. Resume, open or closed, means the client reconnecting to its own conversation log on the same conversation id. The rail never re-sends rows. A binding's `opened_at` moves only when its conversation id does.

## Where a thread starts

A thread starts when a conversation makes its first object: a new chat, a cold-woken session, a person on the dashboard starting one. Everything else that conversation makes joins it:

- a subtask joins its parent's thread;
- a holder's creations join the thread it holds;
- a write that names no conversation joins the identity's own thread (`conversation_threads`).

The full order is in [Agent Rail](Agent%20Rail.md#where-a-create-lands). Who is woken follows from it: the seats a thread names are woken on that thread, once.

## Holder kinds

A binding names what is holding the thread. `stream`, `tmux`, `oneshot` and `adapter` are the rail's own (`_RAIL_HOLDERS`). `conversation` is a person's session, which the rail did not create and may not resume.

| kind | what it is | ends when |
|---|---|---|
| `stream` | Claude on a FIFO by default, or an explicit `RAIL_STREAM_CMD` client using the same input envelope | its process exits |
| `tmux` | the same, in a tmux window you can attach to | the window dies |
| `oneshot` | one native `--resume`, one answer, no resident | the answer is given |
| `adapter` | one turn of a runtime without a configured streaming client | the **last** turn on the thread returns. A message arriving mid-turn queues a second turn behind the seat's turn lock, and the thread stays held until both end |
| `conversation` | a person's session, bound by writing when no open or closing sibling binding exists on the thread | `{"ended": true}`, and only that |

**At most one live holder per (thread, identity).** A binding row is unique per thread and identity. When a person's session holds the thread as a registered surface, the plane refuses a rail holder's binding with `409`. Every birth path posts its binding *before* it births, and declines on that answer. This is the no-twin rule, enforced at the plane rather than by each seat's goodwill. A `conversation` binding whose surface has ended, or that never registered a surface, holds nothing: the next message births a holder as usual.

A successful comment is not necessarily a receiving binding.

- `_bind_conversation` leaves an existing non-closed binding untouched, so an interactive sibling writing on that thread does not join it. The "another conversation of yours is on this thread" hint returned with the write is not a promise of delivery to both.
- The object's audience selects identities, and each identity's binding selects its receiving conversation.
- The label on a comment attributes its writer, not the receiver of future replies. To find the receiver, match the fingerprint against current `GET /wake/threads` bindings, not a prior comment.
- Without a binding, the cold-wake rules still apply. A comment alone does not guarantee a birth, and pinned sibling rows cannot birth a holder.

A configured streaming client receives the same plane conversation UUID on create and on resume. Its mapping to a native runtime session stays in the client's environment, not the registry. The rail waits for that client's input endpoint before writing. It does not wait for inference, and it does not post the client's final text: the agent replies with `sc-say`. Runtime recovery and compression belong to the client. Existing adapter bindings need a deliberate migration before a seat switches to this mode. The client setup is in [Rail Clients](Rail%20Clients.md).

## Surface kinds

`POST /wake/surfaces` accepts `interactive`, `stream`, `tmux` and `oneshot` (`_SURFACE_KINDS`). Only `interactive` is registered by hand: the rail creates the others, so the plane knows them by construction. A surface is **never asked to prove it is listening**. It holds what it wrote on until it says `{"ended": true}`; refreshing it updates `last_seen_at` for observability and gates nothing.

## The verbs

What the rail's `dispatch()` answers with, plus the sweep's (`DISPATCH_VERBS`):

| verb | state it came from | what happens |
|---|---|---|
| `wake` | no instance | birth, bind, deliver |
| `continue` | a live instance holds the thread | deliver into it |
| `closing` | the batch carries the object's close, or the plane says the thread is done | deliver the wrap-up; the instance ends itself |
| `resume` | the process is gone, the work is open | reconnect to its own log, same conversation id |
| `oneshot` | the process is gone and the work is resolved | answer once, exit, no resident |
| `idle` | nothing to deliver, or a birth the plane refused | nothing |
| `closed` | *(sweep)* the process is gone | the binding is closed. Observed, never enforced |

For a runtime that does not stream, `wake`, `continue` and `resume` collapse into one act. There is no resident to keep, so each is one adapter turn on the runtime's own conversation. That is not a lesser lifecycle: it is what happens to any holder whose process has ended while its work is still open.

Known informational adapter batches (a completion, a priority or a tag change) are kept as context without a turn. A pure close marks an idle binding `closed`, or a running binding `closing` until its last turn exits. The runtime conversation stays resumable. Mixed batches and work-changing events take the ordinary dispatch path, and the next substantive prompt carries the kept facts.

One-shots on a closed thread are bounded by `RAIL_ONESHOT_ATTEMPTS` (default 3). A doc-rooted thread is the exception: it has no open work, so every message on it is a one-off and the cap does not apply. Ping-pong there is stopped by the former-holder rule in [Rail Behaviour Map](Rail%20Behaviour%20Map.md#the-rails-rule-per-holder-state-any-kind).

## State × event

Rows are the binding's state; `—` means no binding exists.

| | a message arrives | the object closes | the process ends | the surface says `ended` |
|---|---|---|---|---|
| **—** | `wake`: a cold wake. A live conversation of the identity that is not on the thread is never handed it | `idle`: a close for a thread with no instance opens nothing | n/a | n/a |
| **open** (holder) | `continue` | `closing` | → `closed` (sweep) | n/a |
| **open** (conversation, registered surface) | delivered to that conversation's feed; **no holder is born** | delivered | n/a | → `closed`, by the plane |
| **open** (conversation, no registered surface) | holds nothing: treated as **—** | as **—** | n/a | n/a |
| **closing** | `continue`, carrying the wrap-up again | already closing | → `closed` | n/a |
| **closed**, work open | `resume`, same conversation id | — | n/a | n/a |
| **closed**, work resolved | `oneshot`, bounded by `RAIL_ONESHOT_ATTEMPTS` (uncapped on a doc-rooted thread) | — | n/a | n/a |

## What is never true

- **No timers.** Nothing expires, nothing is reaped on a clock, no backlog is "late". The 15-second SSE keepalive is transport liveness, not a lifecycle event.
- **No proof of life.** Neither a surface nor a binding is asked to show that anyone is reading. An unread message is not an outage.
- **No inference of a conversation.** Never from a process id, a username, or the lone live holder. A write carries `X-Session-Id`, or it is the identity's own.
- **No stray thread.** A create never starts a thread except a conversation's first; everything else joins one.
- **No replacement.** A headless holder is never born to stand in for a conversation that went quiet. That is the twin, and it is the failure the rest of this page exists to prevent.

## Related

- [Agent Rail](Agent%20Rail.md): the model in plain words, the pens, delivery and receipts.
- [Rail Behaviour Map](Rail%20Behaviour%20Map.md): every event, who receives it, and what the rail does with it.
- [Rail Clients](Rail%20Clients.md): Claude, Codex and Hermes, and the `wake.env` keys.
- [Rail Operations](Rail%20Operations.md): limits, caps and running it.
- The served skill, [`registry/skills/samcloud-agent/SKILL.md`](../registry/skills/samcloud-agent/SKILL.md): the same model, written for the agent.

Next: [Rail Behaviour Map](Rail%20Behaviour%20Map.md)
