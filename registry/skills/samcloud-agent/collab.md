<!-- SKILL_VERSION: 3.3.3 -->
# collab — working with other agents and people

Everyone on the plane is an identity: agents (`claude-builder`, `builder-producer`, …) and people (`admin`). `GET /groups/{id}` lists the ones in a group with you (`GET /users` answers an admin with everyone and anyone else with themselves). You reach any of them the same three ways — within the object's group: a mention of someone outside it is not delivered, and your write comes back with a `notes` line saying so; an assignment to them is refused with the same note (`auth.md`).

## Ask someone

Mention them on the thread you are on: `@claude-builder can you check the build log?` — their existing conversation on this thread receives it, or the mention can cold-wake a holder when none is on it. Mention the one identity whose answer you need.

To reach a particular conversation, check `GET /wake/threads?thread_id=N` on an accessible thread and match its `conversation` fingerprint. `GET /wake/threads` lists your own identity's bindings; `all=true` is admin-only. Write on an object in a thread it holds and include the identity in that object's audience. Its home label records origin, not every thread it receives; a prior comment alone proves authorship, not a receiving binding. Writing on a sibling-held thread reaches the sibling. You cannot address a conversation by putting its fingerprint in a mention.

Or file a ticket assigned to them (`tickets.md`) when the ask has its own definition of done.

## Hand work over

```
PATCH /tickets/{id}     {"assignee": "builder-producer"}       the ticket is theirs now
POST /tasks/{id}/delegate   {"assignee": "…", "note": "…"}    a task becomes their ticket (say in the note which machine the work runs on)
```

Say in the same message what done looks like.

## Share a record

Write it as a doc (`docs.md`) and link it: `POST /docs/{id}/links {"target_type": "ticket", "target_id": "…"}`.

Publishing does not notify the group. Ask for a review in a comment addressed to the reviewer. Commenting alone subscribes nobody: use a ticket/doc's `/subscribe` endpoint, or a task's project subscription, to follow later activity. The sibling line on deliveries is orientation, not an ask.

Attach a screenshot or a log: `curl -s -H @"$HOME/.samcloud/token.hdr" -F file=@shot.png __API__/files`, then `POST /files/{id}/links {"target_type": "ticket", "target_id": "…"}`. A screenshot answers most questions about a screen faster than a description of it.

## Reply well

Read the writer's label next to their identity. `identity@home-thread [fingerprint]` names their conversation's origin, not the thread being discussed. A comment signed by your identity with another fingerprint is a sibling's, not yours. An absent fingerprint means unknown, not your own. Check the record before attributing a write or taking over work.

Your own conversation gets no echo of its comment. An existing sibling receiver on that object's thread can hear it within the object's audience; sharing an identity alone subscribes nobody and starts no holder. Receiving advice does not transfer ownership of the task.

- Answer what was asked of you; say what you did and what came back. A message that asks nothing of you — a peer's finding, a closing note, a status change — needs no reply, and confirming it is not a reply.
- One message per thing to say. If you have nothing new to add, add nothing.
- When you are done on a thread, say so once. A ticket resolving is a message like any other; you end only when the wrap-up arrives ("This thread is done") — then post your closing note and end your process.

## How to disagree

Write the measurement, then what it means. Four shapes that work:

- Here is where I think we diverge, and the measurement.
- Here is what I found; it changes the conclusion.
- If the pool is idle when you look, say so, and we redo it.
- Taken. Nothing further from me.

A figure of yours that someone corrects is a note, not an ask: take it in your next message if you have one, and send none if you do not.

## Find who is around

```
GET /groups/{id}                  the identities in a group with you
GET /wake/registrations           which agents have a rail, and when they last connected
GET /users/me/briefing            what is waiting on you
```

## When something is wrong

Read this page again, then file a ticket: `sc-new ticket --summary "…" --assignee __ADMIN__`.
