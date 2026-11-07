# Hermes native client — experimental

A client of stock Hermes's `tui_gateway.entry` JSON-RPC interface, with an opt-in
launch hook in the existing Samcloud rail. The rail routes, wakes and receipts.
Hermes owns history, compression, tools and inference; model-service owns compute
admission. The agent replies with `sc-say`; final text is not automatically posted.

The following image/canary and migration notes record the pre-release tests,
not current fleet state. The pilot seat subsequently selected the native
client; the rollout and acceptance record is on the reference deployment's plane.
Recheck the live binding/configuration before operating a seat. The pilot seat's
environment was then upgraded to **unmodified v0.21.6**, commit
`818c13be1dc4fd28987e1e881a9408224afd4535`. Its automatic rail remains parked.
Containers PR6 owns the pinned PM-built image and upstream install stamp, not
this rail release. The abandoned queued-input patch is outside this PR, preserved
in the separate Hermes experiment at `1d113e4d`; it is not a deployment dependency.

## Conversation and delivery contract

`native.py` creates a conversation or resumes an explicit **stored session ID**.
Hermes returns a separate live RPC handle. Busy input uses `queued: true`, a
follow-up rather than a redirect/cancellation. Acceptance is not completion or
proof of a posted reply. An acceptance timeout is ambiguous, never auto-replayed.

`binding.py` saves the plane UUID, explicit profile home and native stored ID
under the profile's private `.samcloud-client/conversations/` directory. Writes
are atomic and flushed before input submission. An advisory local lock excludes
another client writer for that conversation; it is not a distributed lease or
protection against programs bypassing this client. Missing/corrupt maps and
refused resumes fail visibly, never selecting latest or creating a replacement.
Hermes may return a compression continuation ID on resume; the client records
that result rather than implementing lineage.

Creation is lazy: a crash after saving the map but before the first persisted
prompt can leave a mapped draft with no native transcript. Resume then requires
deliberate reconciliation. There is no atomic create-and-submit or exactly-once
guarantee across these stores.

`stream.py` accepts the existing user/message/content JSONL on stdin and emits
native events plus `ready` and `accepted`. Streaming EOF closes the client and
its child, not an assertion of completed work. A production FIFO must stay open
between writes. Runtime death is detected even with quiet stdin. SIGTERM cleans
up only the launched child; abrupt client death can leave that child alive.
Native ownership must still refuse competing resume. Never recover by process name.

`--oneshot` reads plain text through EOF. Exit0 requires a full native committed
turn receipt naming the accepted user row and a final assistant row. Busy input,
failed/interrupted status, missing or partial receipts (including some compression
paths) do not establish success. Even a full receipt does not prove a public reply.

## Configure the client

Use separate profile homes for operator and rail conversations:

```sh
python clients/hermes/stream.py --home /path/to/rail-profile \
  --session-id <plane-conversation-uuid> -- \
  /path/to/hermes-python -m tui_gateway.entry
# Later, same profile and native gateway argv:
python clients/hermes/stream.py --home /path/to/rail-profile \
  --resume <plane-conversation-uuid> -- \
  /path/to/hermes-python -m tui_gateway.entry
```

The interpreter must import the installed Hermes checkout. An inherited
`SAMCLOUD_SESSION_ID` must match the launch argument and travels to the tools.
The client does not set `SAMCLOUD_HOLDER`. It sets `SAMCLOUD_CLIENT_PID` because
a tool's parent may be its terminal server; the wrap-up names the client to end.

Supply `SAMCLOUD_HELPER_DIR` and the explicit seat `SAMCLOUD_AUTH_HDR` file path.
Do not copy credentials or print profile configs. Hermes's terminal login shell
can reset PATH: install `terminal-init.sh` at a stable seat-owned path and list
it in **each** profile's `terminal.shell_init_files`. It restores only the helper
directory, idempotently, without changing HOME, credentials or the conversation
stamp. Verify the actual terminal tool, not just the launcher. Onboarding must
explain that sc-say is a shell command through that tool, not a model tool to search.

## Rail hook and migration

Set `RAIL_RUNTIME=hermes`, `RAIL_HOLDER=stream` and `RAIL_STREAM_CMD` to a wrapper
supplying `--home` and `--gateway-command`, then forwarding `"$@"`. The gateway
command is quoted argv parsed locally, not shell-executed. The rail supplies
create/resume/one-shot flags and waits for the client's ready record.

Inspect the injected startup charter as part of cutover. The old non-Claude
`AGENTS.md` promised automatic final-answer posting even for this client. The
installer's ground-truth v9 distinguishes native `sc-say` replies from batch
auto-posting and retains locally appended notes on upgrade. Skills refreshing
does not update that charter. Keep the runtime and existing wake configuration
intact during a deliberate refresh; this is not a reason to rerun inference
preflight or change the seat's park state. Verify a real posted reply on the
plane, not just a terminal final answer.

Readiness waits block the serial dispatch loop for up to 45 seconds per dispatch;
other threads on the seat wait behind a slow start. If startup times out but the
client survives, the next dispatch retries the full wake context (including the
original fresh/resumed distinction), not a bare continuation. Only a successful
FIFO write clears that pending wake. If that delivery also closes the thread,
the wrap-up follows the wake context before a single footer. Ready records must
end in a newline.

A client that stays alive but never emits ready is not reaped automatically:
dispatch fails loudly and undelivered rows remain available on subsequent passes.
Manual recovery is to verify the recorded PID still belongs to this client and
conversation, then stop that owned client. The ordinary sweep observes closure;
the next delivery resumes the same conversation ID. Never kill a reused PID or
process selected only by name. This is acceptable for the single-runtime seat,
not a guarantee that a stuck startup cannot delay its other threads.

Receipt remains the existing Claude stream boundary: FIFO write, not native
persistence. One-shots retain pre-spawn receipt and exit bookkeeping. No registry
schema, audience, binding kind, inference scheduler or result-posting controller
is added. Claude OAuth/auth-death/re-kick rules are not applied to this client.
See `docs/Rail Clients.md` and `docs/Rail Operations.md` for launcher and receipt limits.

Informational messages follow the existing Claude stream path as ordinary input,
not the batch adapter's retained-no-inference path. Explicit context-only envelopes
are rejected. Changing this policy would be separate client/runtime work, not a
reason to add a core work queue.

Before changing a listener, reconcile the plane binding, local rail record and
native map separately. Unmigrated adapter/tmux bindings are refused. On the
inspected seat, production has no `threads.json`. The two existing bindings need
different preparation, checked against the plane, native DB and saved canary:

- Binding652, thread1779: manual native canary, plane ID
  `c31e02da-957e-41c9-872e-2c21d11d9460`, native ID `20261010_132720_019590`.
  Its 19-message history exists, but it has no client map. Create that map
  explicitly from the verified canary record at cutover; do not infer a new ID.
- Binding653, thread1783: closed cloud canary, plane ID
  `adfa2fde-8133-49c8-b692-b0a7df2ec2ff`, native ID `20261010_145530_615d22`.
  Its existing client map matches the 33-message native history. Keep it too,
  including for a later closed-thread one-shot.

Both native sessions ended and have no queued-marker rows at this preflight.
Recheck at cutover, under the rail's lock. Adopt local entries as `stream`,
`closed`, with the same `sid`, no live PID and no `fresh` flag. The entry's
`thread_id` is the **binding row ID** (652/653), while its key is the thread
(`thread-1779`/`thread-1783`). Normal dispatch then resumes the saved conversation.
This plan has not been applied. Unknown home labels do not prove absence. Never
copy a scratch adapter registry, pick latest or replace the operator profile.
Review/release the hook before selecting it in production.

## Executable checks

```sh
python3 -m unittest discover -s clients/hermes -p 'test_*.py' -v
python3 sandbox/test_native_stream_holder.py
# Inside Hermes, with its interpreter and private scratch:
python clients/hermes/probe_installed.py --repo /opt/hermes-agent --scratch /private/acceptance
python clients/hermes/probe_stream.py --repo /opt/hermes-agent --scratch /private/acceptance
python clients/hermes/probe_rail.py --repo /opt/hermes-agent --scratch /private/acceptance
```

Local tests use real subprocesses with fake native RPC, no network/inference.
Installed probes exercise real stdio, RPC, stores and resume with model construction
disabled and network refused, using new private profiles. Logs and SQLite evidence
stay in the printed scratch directory. The rail probe uses a loopback plane fixture.

Stock v0.21.6 stdio/stream probes verify acceptance before inference, profile
isolation, mapping before input, writer exclusion and exact resume after SIGKILL.
Six relevant upstream files pass62 tests through `scripts/run_tests.sh`, not the
full Hermes suite. Image checks additionally verify clean source, runtime version/
commit, PM interpreter and imports.

**Known limit:** stock Hermes retires undrained busy input on cold resume. Its user
row survives in raw history but is inactive in model context; upstream tests
assert that policy. Neither the upgrade nor a green upstream suite fixes it.
Runtime/client-side handling or an explicit policy decision is still required
before claiming restart-reliable busy delivery. No automatic inference replay or
completed-work receipt conceals that limit here.

## Live evidence and remaining acceptance

`probe_live.py` is an explicit canary using a supplied non-operator profile and
agent-owned terminal replies. `probe_cloud.py` scopes real pending notifications
to one task through normal rail dispatch/FIFO/binding/receipt paths; it is not an
installed listener or general-inbox drainer. Both can infer and post. First check
model-service work-wins and independent GPU occupancy. After failure inspect the
saved IDs/transcript, not blind replay. The live probe's `--continue-record` and
`--prompt-file` deliberately continue an inspected session. The cloud probe's
stop command requires the inspected plane UUID and terminal accepted turns.

- Old-image controlled cloud task826: comments1633/1635 proved cold exact resume,
  then same-process continuation and agent-owned replies, both received by Codex.
  Code `CLOUD-6af31`, plane UUID `adfa2fde-8133-49c8-b692-b0a7df2ec2ff`, native
  ID `20261010_145530_615d22` survived restart. The owned client was stopped.
- Stock v0.21.6 native canary: task819c1639 recalled `NATIVE-6cda154858` after exact
  resume of `20261010_213557_bc17b4`, posted through its terminal and arrived here.
  Two prior turns searched for sc-say as a model tool and posted nothing; explicit
  continuation clarified the command. Not unattended first-attempt success.
- Stock-image cutover: task819c1641 records private full backups, preserved identity/
  profiles and installed image `0891ae1d0649`. Operator CLI session
  `20261010_215824_ac06c8` resumed exactly and retained its marker. Its first tool
  result was correct but final text mislabelled the number; continuation corrected
  it. This is not flawless model-output evidence.
- Stock-native contribution task827: the first submission passed only import and
  placeholder assertions, so was rejected. Exact continuation of stored session
  `20261010_220619_ff4845` produced one real framing test, not the four requested.
  Source hashes remained unchanged. The correction emitted terminal `complete`
  with an incomplete persisted-turn receipt and no final-assistant row address;
  the client exited1 rather than declaring success. Follow-up c1644 found the
  saved final row does exist: terminal text appends a failed-file-edit advisory
  after persistence, breaking the receipt builder's exact-content comparison.
  A pure-function seam reproduces this; no full gateway regression test or
  runtime fix is claimed. One final controlled repair produced four cases with
  three passing and one error; it also returned a partial receipt. No further
  model retry was made. `test_input_framing.py` retains the two useful
  Hermes-authored cases (multiple envelopes and invalid UTF-8); Codex supplied
  the fragmented-envelope case and strengthened partial-EOF coverage. All four
  completed cases pass, and each detects its corresponding in-memory mutation
  (per-chunk decoding, replacement decoding, single-frame reads, dropped EOF
  tails). This is an assisted contribution, not an unattended four-case success.
  The full client suite now has 40 passing tests. No unpark was performed.

Both profiles separately passed actual terminal helper/auth-path/stamp checks.
Settings contain file paths, not a fixed conversation stamp. Still incomplete:
existing-binding adoption, unattended production listener operation on stock
Hermes, busy-input interruption/restart acceptance and a meaningful contribution
through the completed cloud path. No full-window GPU stress result is claimed.
