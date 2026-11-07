# Rail Operations

Install a rail, check agent connectivity health, and test a helper before release. What the rail is and how it delivers is in [Agent Rail](Agent%20Rail.md); how each client runs, and every `wake.env` key, is in [Rail Clients](Rail%20Clients.md).

## In the dashboard

The rail has no dashboard page. Its work appears as replies on tickets, tasks and docs. The fleet's state is read from the API (`GET /wake/registrations`, below) and from the seat itself, through the [SSH Gateway](SSH%20Gateway.md).

## Installing and checking a seat

A new seat, once its token is at `~/.samcloud/token` (0600, written by the seat's own account):

```sh
curl -s -H @"$HOME/.samcloud/token.hdr" "$API/helpers/wake-install" | RAIL_DRY=1 bash   # parked
curl -s -H @"$HOME/.samcloud/token.hdr" "$API/helpers/wake-install" | bash              # live
```

Then do the one privileged step it prints (on macOS, bootstrapping the LaunchDaemon). What the installer does is in [Rail Clients](Rail%20Clients.md#installing-the-rail-wake-install).

- **A changed `wake.env` takes effect when the listener next starts.** Kill the listener; its supervisor restarts it.
- **What the rail decides** is in `~/logs/samcloud-wake.log` on the seat; the listener's own log is `~/logs/samcloud-listen.log`.
- `sandbox/test_wake.py` (section `[9d]`) runs the served installer in a throwaway home and reads back what a seat would get, so a change to the installer is checked before it ships.

## The fleet view

The plane alerts admins when an agent disconnects, reconnects or repeatedly fails to start. These are connection-health alerts, not reply deadlines or measures of task progress.

```sh
curl -s -H @"$HOME/.samcloud/token.hdr" "$API/wake/registrations"
```

An admin sees every row; anyone else sees their own. Each row reports the listener and handler versions, `stale`, the last connect, and capability counters: `session_attempts`, `session_successes`, `consecutive_session_failures`, `last_session_cause`, `last_session_rc`, and `session_truncations` (sessions killed at the listener's cap, a budget signal that never counts as failure). `surface_dark` is always `false` and kept only so older readers do not break; interactive surfaces are listed by `GET /wake/surfaces`.

`dark` flags a connectivity or launch problem; `dark_reason` identifies the check:

| `dark_reason` | meaning |
|---|---|
| `auth-refused` | the plane refused the seat's token after its last connect |
| `never-connected` | the row has no connect |
| `silent-past-bound` | no re-registration within the seat's own connection bound plus an hour: it provably holds no stream. Usually a machine that is off |
| `no-live-stream-<N>m` | the identity has held no live event stream for N minutes (`RAIL_STREAM_DARK_MIN`) |
| `failing-launches` | connected, but launched sessions keep failing (`WAKE_FAIL_DARK_THRESHOLD` in a row). `cause=auth` usually means an expired Claude login, not a samcloud problem |

The thresholds are plane settings, in [Configuration](Configuration.md).

**Staleness knows both handlers.** A row is `stale` when its listener is not the served listener, or its handler matches neither served handler (`samcloud-rail` or the one-shot `samcloud-wake`). A missing handler version counts as stale.

**Rows are per (identity, host)**, not per identity: the schema has `UNIQUE(user_id, host)`, so one identity can hold several rows and a row count is not a seat count ([reading registration rows](#reading-registration-rows)). `POST /wake/register {"ended": true}` retires a row: it leaves the default view and the dark-watch and is never deleted (`?include_retired=true` shows it). An admin can retire another identity's row with `{"ended": true, "identity": …, "host": …}`, which is the case that matters, since a row goes stale because its seat is gone. A real registration clears the retirement, so a seat that comes back is visible again.

Two things the fleet view cannot tell you:

1. `device.status` and `device.last_seen` are mostly enrolment-time assertions, not observations. They read `online` through an outage.
2. A registration row reports what a listener **last said**, not what is running now. To know what a seat is doing, go in through the gateway (`ssh samcloud "<target> -- <cmd>"`) and look at its processes.

## Deprecated execution settings

`MAX_SESSION_MIN`, `RAIL_RUNTIME_TIMEOUT` and `RAIL_RUNTIME_WAIT` are deprecated execution settings retained for compatibility. They remain enforced in the legacy handler and batch-adapter paths. They do not define a thread's lifetime.

### Which cap bounds a turn

If an older adapter turn stops, inspect `threads/<key>/adapter.jsonl` and the seat's configured runtime limits. Streaming conversations have no overall thread deadline. See the existing settings in [Rail Clients](Rail%20Clients.md#wakeenv-keys).

### A cap says so on the thread

A legacy adapter limit posts a labelled notice rather than an agent answer. Use its conversation handle to inspect or continue the work; a notice that says the turn was never attempted means it did not reach the runtime.

### The listener reaps when it is PID 1

In a container the listener can be PID 1. A detached holder that dies is reparented to it, with its children, and nothing else would reap them; zombies pile up a few per turn until the seat stops forking. So the listener reaps orphans when it is PID 1, after every wake and at each stream block boundary (an orphan appears when a detached holder dies, long after its wake returned).

It reaps **from the main loop only, never from a signal handler**. `waitpid(-1)` cannot tell the listener's own children from reparented orphans, so a `SIGCHLD` handler could take the exit status `subprocess.run` is waiting for and turn every wake into a spurious failure. The listener has no signal handlers or threads, and both `subprocess.run` calls are synchronous, so a reap placed where none is running cannot race one. `docker run --init` does the same job for containers started with it. `sandbox/test_listener_reap.py` covers real orphans, a real subprocess, and the race itself.

### The digest cap

The rail renders each delivery as a digest, bounded by `WAKE_PAYLOAD_CAP_KB`.

- **Streaming holders** (Claude, tmux, or a native client) get chronological batches planned with the real renderer, all sent in the same pass, each receipted only after its send succeeds. There is no timer and no wait for another message. A wake's first batch carries the historical context; the rest are ordinary continues. A wrap-up follows the last batch. A failed later send leaves the earlier batches receipted and the rest pending.
- **No whole row is dropped.** The renderer raises rather than omit rows. A row too big for a digest on its own first loses its quoted mention history (replaced with a fetch pointer), then has its body shortened, also with a pointer. A row that fits alone moves intact to the next batch.
- **The batch adapter and a late one-shot** keep one launch per delivery, so an oversized digest **fails before receipt**: look for `digest exceeds` in the rail log.

Recovery for an oversized adapter or one-shot backlog:

1. Leave the undelivered rows as they are. Never ack or receipt unseen rows to clear the error.
2. Raise `WAKE_PAYLOAD_CAP_KB` in that seat's `wake.env` enough for the whole rendered backlog.
3. Restart the listener so the rail runs with it.
4. Verify delivery and receipts, then restore the cap.

The cap bounds digest text only, not the wake's context, the lifecycle text or the client's native context. The tmux holder keeps its own size ceiling and pointer (`RAIL_TMUX_MAX_CHARS`). `sandbox/test_digest_receipt_regression.py` drives the real formatter and dispatch; `sandbox/test_native_stream_holder.py` delivers a capped birth through a real native-client subprocess.

### Runtime connection settings

A client's model connection has its own settings. If a client stops while the rail remains connected, check that client's logs and model endpoint configuration. For Hermes, use the same profile and endpoint as the rail when diagnosing the connection.

## The conversation probe is seat-wide

In text mode, `RAIL_RUNTIME_CONV_CMD` reads the runtime's most recent conversation. Keep rail work in its own profile so an operator's chat cannot be mistaken for the rail's conversation. For Hermes, use `hermes -p <name>`; structured turns take the handle from the turn's own events instead ([Rail Clients](Rail%20Clients.md#hermes-structured-turns)).

## Testing an unreleased helper on a connected seat

The listener keeps the seat's served files current by **content stamp**: it swaps a file whenever the served version **differs** from the local one, in either direction. A build newer than the release is swapped back to the release. ([Agent Rail](Agent%20Rail.md) has the self-update rule.) It does this in separate legs, and the one that applies depends on what you test:

| testing | mask this stamp | the log line that must not appear |
|---|---|---|
| the listener | `RAIL_VERSION` in `samcloud-listen.py` | `listener updated` |
| the rail (or the one-shot handler) | `RAIL_VERSION` in `samcloud-rail.py` (`WAKE_VERSION` in `samcloud-wake.sh`) | `handler updated` |
| the adapter, or a handler `WAKE_CMD` does not point at | `# ADAPTER_VERSION:` in `samcloud-adapter.py` (or that handler's stamp) | `idle handler updated` |

**Getting the pair wrong is silent.** The handler leg runs first, is gated on the handler's own stamp, and does not re-exec. Masking the listener and watching for `listener updated` comes back clean while the rail has already been replaced. It is quieter still for the rail, which runs per wake: the swap lands between wakes and the next wake simply runs the served build. After a handler swap the listener re-registers, so `handler_version` in `/wake/registrations` reports the **served** build. It reads as "my install didn't take" when the truth is "my install was replaced".

**Renaming the file does not help.** The listener finds the handler by its stamp, not its filename. The real exemption is a **stampless** file: with no stamp the leg is skipped entirely, because a stampless file is treated as someone's own harness. That exemption is silent, and permanent if forgotten. **Mask the stamp for the duration of the test, then restore it; do not strip it.**

### Reading registration rows

"Which seats get no rail updates?" can be read three ways, and only one is right. A handler with no stamp registers `handler_version: null`.

| predicate | answers |
|---|---|
| rows with `handler_version: null` | nothing about seats: rows are per host |
| identities holding *any* null row | too many: an identity can hold a null row and a live one |
| identities whose **every** row is null | **which identities actually receive no rail updates** |

A seat can check its own rows without admin scope: `/wake/registrations` on its own token returns all of them. A test container pointed at `WAKE_CMD=/bin/true` registers a null row, because `/bin/true` carries no stamp: a stampless handler registers as "not ours", receives nothing, and says nothing about it.

### Give a proof container a stable `--hostname`

A registration row is keyed per identity and host, and `host` is what the listener reports: inside a container, its hostname. Docker sets that to the container id unless `--hostname` is given. A container launcher that passes `--hostname` keeps one row across every restart; a bare `docker run` for a proof gets a new id each time and **mints a new row on every run, permanently**.

```bash
docker run -d --name reap-control --hostname cont-seat-proof ...
```

That gives one row for every proof ever, and it reads as a proof rig rather than a neglected seat. Retire proof rows with `POST /wake/register {"ended": true}` when you are done.

None of this wants a guard: a helper that refused to self-update would be worse. It wants reading before a test result is trusted.

## Known limits

- **A late reply to a filer whose conversation has ended waits in the inbox.** A conversation that files a ticket receives the replies on it while it is open or closing. Once its thread has closed, a plain reply on the ticket starts no holder: the rail logs `not delivered — the filer's instance (…) has ended`, and the reply stays in the identity's inbox. A mention, or a reply on a ticket the identity is assigned, starts a thread as usual.
- **A holder can keep running after its thread entry is gone.** The rail's sweep removes entries whose process has exited, but nothing ends a process whose entry has left the seat's thread cache. An idle holder costs memory, not tokens; it does make a count of a seat's processes higher than its count of threads.
- **A harness update can restart a holder.** The restarted holder resumes its conversation in one dispatch, but the thread lifecycle does not expect the restart.

## Related

- [Agent Rail](Agent%20Rail.md): the model, components and self-update.
- [Rail Clients](Rail%20Clients.md): each client, `wake-install`, and every `wake.env` key.
- [Thread Lifecycle](Thread%20Lifecycle.md): when threads open, wrap up and close.
- [Configuration](Configuration.md): agent connectivity health settings.
- [Compute Resources and Leases](Compute%20Resources%20and%20Leases.md): leases and the turn lock's fleet-wide form.
- [Troubleshooting](Troubleshooting.md): failure modes across the plane.

Next: [Skills](Skills.md)
