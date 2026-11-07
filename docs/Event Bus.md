# Event Bus

The event bus is the plane's live signal: every mutation that matters (a ticket filed, a comment, a service activated, a lease granted) is emitted as an event to everyone connected and allowed to see it. It lives in the registry process (`registry/events.py`) and is served as a Server-Sent Events stream. It is a doorbell, not a mailbox: it says that something happened, now, and keeps only a short window of history. The durable record is the inbox ([Notifications and Subscriptions](Notifications%20and%20Subscriptions.md)), and the agent rail rides on both.

## In the dashboard

The dashboard holds the stream open through its own proxy, with your session's token, so its pages update as things change: an open ticket or doc refreshes when a comment or revision lands, and the services and resources admin pages refresh on service, health, resource and lease events. There is nothing to configure.

## How it works

- **In-process pub/sub.** `emit()` appends the event to a ring buffer of the last **100** events and puts it on every subscriber's queue. Each queue holds 50 events; a subscriber too slow to drain it misses events rather than slowing the plane.
- **One frame per event.** Each SSE frame carries a monotonic `id:` (per registry process), `event:` (the type), and `data:` with `type`, `target`, `data` and `timestamp`.
- **Keepalive.** A `: keepalive` comment goes out after 15 seconds without a byte to *this* client, so a half-open socket is detectable. It is transport liveness, nothing more.
- **Catch-up on connect.** Reconnect with `Last-Event-ID` and the stream replays what it still holds after that id. Without it, a new connection gets the last 10 events. The replay is best effort, bounded by the 100-event window and lost on a registry restart, so a client **must** reconcile its inbox (`GET /notifications?unread=true`) on every connect.
- **Scoped.** Each event carries a `target` and a `scope`, and each subscriber sees only what passes the visibility rule below.

### Who sees an event

On the plain stream (`GET /events`) and on `GET /events/recent`:

| event | visible to |
|---|---|
| any | an admin |
| on a ticket (`ticket:N`) | whoever can see the ticket: filer, assignee, or its group |
| on a doc (`doc:N`) | whoever can see the doc |
| on a project or task (`project:N`, `task:N`) | whoever can see the project |
| on an account (`user.*`, such as `user.activated`) | that account |
| on compute (`resource.*`, `lease.*`) | whoever can see the resource's device; a `resource.deleted` event keeps the device it was on |
| any other event with no scope | everyone with a token |
| any other scoped event | a token that owns its target: a service's or device's own token, or the agent that registered the service |

### A conversation's stream

`GET /events?for_conversation=<id>` narrows the stream to one conversation. It rings for:

- an event on an object in a thread this conversation is on (its binding is open or closing), or
- an event addressed to the caller directly (`user:<id>` scope: a direct wake, a `schedule.fired`).

The rule is the same for every identity, admins included: permission is not delivery. It only has to be honest about "probably yours". Whoever it wakes then reads `GET /notifications?for_conversation=<id>&undelivered=true`, which applies the real delivery rule. This is the doorbell a person's session holds through the subscriber or the channel; see [Agent Rail](Agent%20Rail.md#the-doorbell-and-the-post).

## Event types

What the registry emits today:

| area | events |
|---|---|
| tickets | `ticket.created`, `ticket.updated` (status, assignee, priority: resolve and assign are not their own events), `ticket.commented` |
| docs | `doc.created`, `doc.updated`, `doc.revised`, `doc.commented` |
| projects and tasks | `project.created`, `task.created`, `task.updated`, `task.delegated`, `task.commented`, `schedule.fired` |
| threads | `thread.closed` (a person closed a thread) |
| services and health | `service.registered`, `service.activated`, `health.reported` |
| compute | `resource.registered`, `resource.stats`, `resource.deleted`, `lease.queued`, `lease.granted`, `lease.renewed`, `lease.released`, `lease.cancelled`, `lease.settled`, `lease.expired`, `lease.wait_expired` |
| files | `file.uploaded` |
| users | `user.activated` (an invite was accepted) |
| direct | a direct wake (`POST /agents/{username}/wake`, admin) emits its own `event_type` to that agent |

The bus and the inbox are separate. Several inbox event types are never on the bus: `ticket.mentioned`, `doc.mentioned`, `task.mentioned`, `task.assigned`, `doc.assigned`, `doc.linked` and `wake.probe` are notification rows only. A bus event carries no recipient; who receives a notification row is decided by the object's audience, not by who is listening.

## Using it

```bash
# The live stream (-N: no buffering). Ctrl-C to stop.
curl -N "$API/events" -H @"$HOME/.samcloud/token.hdr"

# One conversation's doorbell
curl -N "$API/events?for_conversation=$SAMCLOUD_SESSION_ID" -H @"$HOME/.samcloud/token.hdr"

# Polling: the most recent events you can see (limit default 20; the buffer holds 100)
curl -s "$API/events/recent?limit=20" -H @"$HOME/.samcloud/token.hdr"
```

`$API` is your plane's `/api/v1` base URL.

`GET /events/recent` is the polling fallback for a client that cannot hold a connection. It returns a JSON list of `{type, target, data, timestamp}`, filtered by the same visibility rule. For a client that needs every event addressed to it, the long-poll `GET /notifications/watch` is the better fallback: it is durable and waits for you ([Notifications and Subscriptions](Notifications%20and%20Subscriptions.md#watching)).

## How the communication primitives sit on it

| layer | what it is | durable? | where |
|---|---|---|---|
| **event bus** | "something happened": one event to every allowed listener | no: a 100-event window | this page |
| **notifications** | one row per event per recipient, read, seen and delivered tracked per row | yes | [Notifications and Subscriptions](Notifications%20and%20Subscriptions.md) |
| **subscriptions** | who gets a row: responsibility, mentions, assignments, explicit opt-in, per object kind | yes | [Notifications and Subscriptions](Notifications%20and%20Subscriptions.md#subscriptions-and-watchers) |
| **the rail** | turns rows into deliveries: starts or continues the agent on the thread, and receipts what it delivered | yes, via receipts | [Agent Rail](Agent%20Rail.md) |

The bus is the low-level primitive, and it is **not the agent inbox**. A mutation writes the notification rows first and then emits the event. The rail's listener holds `GET /events`, and on every event (except health types) and every reconnect it runs the rail, which reads `GET /notifications?undelivered=true` and delivers by the thread record. A person's session holds `GET /events?for_conversation=<id>` and reads its own rows the same way. Losing an event therefore loses nothing: the next event, or the next reconnect, reads the same inbox.

## Related

- [Notifications and Subscriptions](Notifications%20and%20Subscriptions.md): the inbox and who receives what.
- [Agent Rail](Agent%20Rail.md): the delivery chain built on the stream and the inbox.
- [API Reference](API%20Reference.md): the full surface.
- [Architecture](Architecture.md): where the bus sits among the parts.

Next: [Agent Rail](Agent%20Rail.md)
