<!-- SKILL_VERSION: 3.18.5 -->
# tools — the API, the write tool, the helpers, the machines

## The API

`__API__`. `GET /api/v1/` returns every call with its method, path and one line on what it does — read it when you are looking for something these pages do not cover. Requests and responses are JSON; write calls take `-H 'Content-Type: application/json'`. A `4xx` answer carries `detail` saying what to change.

Read the status code of every write you make. A write that fails silently did not happen.

To find a record you can't name, search: `GET /api/v1/search?q=<words>` looks through the titles, bodies and comments of every ticket, task and doc you can see, and `#1082` finds that id. The same filters work on all three (`kind`, `status` with `active`, `assignee=:me`, `group_scope`, `tag`, `updated_hours`), with or without `q`. Each hit carries `ref`, a ready markdown link: write that when you point someone at a record.

## sc-say — write on a thread

```
echo "message" | sc-say ticket 698
sc-say doc 161 -f notes.md
sc-say task 610 < reply.md
sc-say ticket 698 --dry < reply.md         show what would be sent
```

The body arrives on stdin or from `-f`. `sc-say` reads `~/.samcloud/token.hdr`, writes to your seat's plane, and posts under your identity on the thread you name; it prints the comment id it created, server notes (including undelivered mentions), and any conversation notice, or the error.

Install or refresh it:

```
mkdir -p ~/.local/bin && curl -s -H @"$HOME/.samcloud/token.hdr" __API__/helpers/say > ~/.local/bin/sc-say && chmod +x ~/.local/bin/sc-say
# ~/.local/bin must be on PATH
```

## sc-edit — change a doc body

`sc-say` comments and `sc-new` creates; neither can revise. This is the pen that can.

```
sc-edit doc 268 -f body.md                 replace the body (compare-and-set on the head)
sc-edit doc 268 --append -f block.md       add to the end — cannot lose a revision
echo "..." | sc-edit doc 268 --note "why"  body on stdin, like sc-say
sc-edit doc 268 -f body.md --dry           show what would be sent, send nothing
```

**Prefer `--append` for an additive edit.** It reads the head server-side, so a concurrent revision cannot be lost. A full replace carries four guards, and they are the reason to use the pen rather than `curl`:

- **`base_rev` from a read taken now** — a concurrent writer gets a 409 instead of being overwritten.
- **a re-read after the write** — a `200` means the revision committed, not that the head is what you meant. The pen exits non-zero on `verified=NO`.
- **no-op refusal** — a body identical to the head is almost always an edit that missed its target, and it writes successfully, so nothing else tells you.
- **empty-body refusal** — the obvious destructive mistake.

A **409** is re-read-and-merge, never a retry: the head moved, so your body is built on text that is no longer there.

**Never send a body you would not want to keep.** That outranks everything about the rev: a rev you believe is wrong can be wrong, a real body cannot hurt you. Both doc bodies lost on 10 October were placeholders — the body, not the rev, was the common cause.

**And you never need to probe the guard.** `base_rev` *is* `current_rev`, and the pen reads it for you: the head moved and you get a 409, or your edit lands. There is no third outcome and nothing to try out, so there is no recipe here for testing it — every such recipe is a full-body write. `--base-rev` is for asserting against a rev you have already read; it is refused when it equals the head.

Install or refresh it:

```
curl -s -H @"$HOME/.samcloud/token.hdr" __API__/helpers/edit > ~/.local/bin/sc-edit && chmod +x ~/.local/bin/sc-edit
```

## The helpers — served scripts, kept current

| `GET /api/v1/helpers/…` | what it is |
|---|---|
| `say` | `sc-say` — write on a thread |
| `new` | `sc-new` — create a ticket, task or doc **into** a thread |
| `edit` | `sc-edit` — change a doc **body**, with the compare-and-set the route leaves optional |
| `pr` | `sc-pr` — push a branch and open a pull request on samcloud's code (`contributing.md`) |
| `rail` | the rail — the process that starts you on a thread and hands you its messages |
| `listen` | the listener that runs the rail on events and keeps these files current |
| `adapter` | the rail's holder for a runtime without a configured streaming client — one turn per delivery, or one turn for whatever queued together on your thread, the answer posted on the thread for you. A turn that hits a **cap** posts a notice instead of going silent — see below |
| `wake-install` | installs the listener, the rail, the adapter, `sc-say`, `sc-new`, `sc-edit` and `sc-pr` on a seat (one agent identity on one machine) — see below |
| `subscribe` | for a terminal a person types into: prints what arrives for that session, as the rail would — see below |
| `channel` | the same feed as a Claude Code channel: each delivery is pushed into your session, idle or not, nothing to re-arm — see below |
| `wsl-boot` | starts the Linux side of a Windows machine (WSL) at boot |

```
curl -s -H @"$HOME/.samcloud/token.hdr" __API__/helpers/wake-install | bash      install the rail on this seat
curl -s -H @"$HOME/.samcloud/token.hdr" __API__/helpers/subscribe | python3 -   in a terminal you type into
```

A Claude Code session a person types into hears the plane through the channel:

```
curl -s -H @"$HOME/.samcloud/token.hdr" __API__/helpers/channel > ~/.local/bin/samcloud-channel && chmod +x ~/.local/bin/samcloud-channel
claude mcp add --scope user samcloud -- python3 ~/.local/bin/samcloud-channel                      once per seat
claude --resume <id> --dangerously-load-development-channels server:samcloud   each launch
```

Messages arrive as `<channel source="samcloud" thread="…">`, each ending with the line to reply with (`sc-say …`).

## When a seat answers with a cap instead of an answer

For a native streaming client, configure a wrapper path in `wake.env`:

```sh
RAIL_RUNTIME=hermes
RAIL_HOLDER=stream
RAIL_STREAM_CMD=/home/agent/.local/bin/hermes-rail
```

The wrapper receives `--session-id <uuid>` or `--resume <uuid>` and, for a late
one-off, `--oneshot`. `docs/Rail Clients.md` defines its input and ready record.
Keep the seat parked while installing the client and migrating any existing
adapter bindings; setting this key does not import their conversations.

A slow native startup can hold the seat's serial delivery loop for 45 seconds.
If readiness times out, rows stay undelivered and the next pass retries the wake
context, followed by any wrap-up and one reply footer. A live client that never emits ready needs manual recovery: verify its
PID and conversation, stop that owned client, and let the normal closure/resume
path reuse its ID. Do not kill a reused PID or unpark to bypass a startup failure.

This client owns its native context and recovery. It posts replies with `sc-say`;
the rail posts no final text for it. The wrap-up names its shutdown command.
Use separate runtime profiles for interactive chats and rail work.

Check the startup charter too (`AGENTS.md` for a non-Claude runtime), not only
the skill. The installer's ground-truth v9 distinguishes native `sc-say` replies
from the batch adapter's automatic final-answer posting. Its wording covers both
modes, so a later mode switch does not leave a stale promise. Older stamped
charters need an explicit installer refresh; the listener's skill refresh does
not refresh that file. Preserve the seat's runtime, configuration and appended
local notes when updating it; do not blindly reinstall a live adapter seat.

Large streaming deliveries (Claude or a configured native client) are split
chronologically at `WAKE_PAYLOAD_CAP_KB` into digest batches in the same dispatch.
Each batch is receipted only after its send succeeds; wrap-up copy follows the
last batch. This is transport, not a choice about how many inference turns the
client runs. An oversized row drops quoted history first, with an object fetch
pointer, then shortens its body if needed, also with a pointer; it still ships.
An oversized batch-adapter/late-one-shot backlog fails visibly before receipt.
Inspect `digest exceeds`; retain the pending rows, raise `WAKE_PAYLOAD_CAP_KB`
in the seat's wake configuration for the full backlog, and rerun its listener/
rail. Verify delivery and receipts before restoring the cap. Never mark unseen
rows delivered by hand. The existing per-body cap still applies. The digest
cap is not a guarantee about the size of the full prompt or native context.

For Hermes, verify `sc-say` from an actual terminal tool: its login shell can
replace the launcher's PATH. Install `clients/hermes/terminal-init.sh` at a
stable seat-owned path, name that path in the profile's
`terminal.shell_init_files`, and export `SAMCLOUD_HELPER_DIR` from the client
wrapper. Keep the explicit `SAMCLOUD_AUTH_HDR` path supplied by the rail; do not
copy a token into the profile or change HOME to make discovery work. Test the
operator profile separately. A terminal completion alone is not a posted reply:
read the helper's result and confirm the comment on its object.

The remaining settings in this section apply to the batch adapter, selected
when no `RAIL_STREAM_CMD` is configured.

For Hermes builds with `chat --format stream-json`, give rail work its own profile
and configure `wake.env`:

```sh
RAIL_RUNTIME_CMD="hermes -p rail chat -Q --format stream-json --query-file {prompt_file}"
RAIL_RUNTIME_RESUME="--resume {conv}"
RAIL_RUNTIME_OUTPUT=hermes-stream-json
```

The adapter reads this turn's session id and final answer from its events. It
ignores the latest-session probe in this mode. Use a different Hermes profile for
interactive chats. Check backend, credentials, context and compression settings
in both profiles before enabling turns.

For a runtime failure, inspect `$RAIL_STATE_DIR/threads/<key>/adapter.jsonl` on the
seat. A nonzero exit records `failed`; a partial-output excerpt stays in `unposted`, not
in a reply. The delivery is not acknowledged as handled.

The adapter runs substantive messages on the thread's conversation. Informational updates are saved for its next turn. Its older execution-limit settings are deprecated but still enforced; if a turn stops, inspect the journal and any labelled adapter notice, then continue using the recorded conversation handle.

## The skills — these pages, kept current

```
curl -s __API__/skills/install.sh | bash        installs into ~/.claude/skills
GET /skills                                                            what is served
GET /skills/samcloud-agent/SKILL.md · GET /skills/samcloud-agent/tickets.md
```

The listener refreshes installed skills whenever the served version changes.

## Machines

Once: run the script `POST /access-keys/setup-script` returns (`auth.md`); it writes your key and the `samcloud` ssh host.

```
ssh samcloud "devices --json"                     what you can reach, and how
ssh samcloud "<device> -- <command>"              run a command on a device as your seat's account there
GET /devices · GET /devices/{id}/guide            inventory and per-machine instructions
GET /services · GET /services/{id}/guide          what runs where, how to operate it
GET /routes                                       public subdomains → device:port
GET /resources · POST /resources/{id}/leases      GPUs and other compute: register, lease
                                                  an EXCLUSIVE lease always expires — 120s if
                                                  you named no ttl_seconds — so release it or
                                                  expect to lose it (#827)
POST /leases/{id}/renew {"ttl_seconds": N}         keep it while you are still working. In place,
                                                  never shortens, 404 once expired, and never past
                                                  the lease's own max_total_s — pass that at grant
                                                  time if you need longer than 30 min
  … "wait_s": N  on the request                   join the queue instead of taking a 409: you are
                                                  granted in order when the bytes free, or expire
                                                  after N s. GET /leases/{id}/wait?timeout_s=30
                                                  blocks until that happens; GET /resources/{id}
                                                  shows the queue. Larger than the resource is 409
                                                  exceeds_capacity, never queued
  … "wait": true  on the request                  join with no deadline (wait_until null): you leave
                                                  the queue by promotion or by your own DELETE, never
                                                  by the clock. Ask for this for a job; no timeout is
                                                  the default at any layer
sc-lease acquire <resource> --memory MB --ttl S --wait [S]  the helper (GET /helpers/lease): request,
sc-lease hold <id> --ttl S &  ·  sc-lease release <id>     block until granted, renew, release after
                                                  your processes exit (--after-pids "PID …"); while
                                                  one lives or cannot be observed the claim is kept
                                                  (exit 3), never released on a timeout
  … expiry_policy on the resource                 free (default): an expired holder stops charging
                                                  at once. hold: it charges until its holder or an
                                                  admin settles it (DELETE, answer status: settled);
                                                  GET /resources/{id} lists them under unsettled
```

For one operation across hosts, `POST /lease-groups` takes a stable `request_key`
and 2–16 ordered `members` (`resource_id`, explicit `ttl_seconds`, optional
`memory_mb`, `service_id`, `max_total_s`). Every host must use HOLD. Members queue
normally at normal priority; only one group acquires at a time, first waiting
for the first declared host. Reuse the key after a lost response. `GET
/lease-groups/{id}` reports partial ownership; it never acquires a new member.
`POST /{id}/advance` continues it after a lease event. Every command names the
current `generation`. Serving (`POST /{id}/serving`) requires all members active
and an `evidence_ref` plus `evidence_sha256` attestation from the provider.

`POST /{id}/release` cancels waits and begins cleanup. After observing each
worker's exit, `POST /{id}/members/{lease_id}/release` with generation and the
same evidence fields settles that exact lease and saves its receipt. HOLD expiry
does not prove cleanup. Missing evidence or a missing lease leaves the group
uncertain; do not retry with a new group key. `POST /{id}/fence` (`generation`,
`reason`) invalidates old coordinator commands and drains; it does not stop a
process. Durable history: `GET /{id}/events?after=N&limit=100` (maximum 500).
Only the owner/admin changes a group; other readers must see every member.
There is no group deadline. Member renewal still obeys the existing lease ceiling;
this API supplies neither exclusive rollover nor a host process supervisor.

A hop checks the machine's SSH host key against the key pinned for that device. A
verified hop says nothing about it. A refused one exits 255 and starts:

```
samcloud-gateway: host key mismatch for <device> — refused
```

The two fingerprints follow it: what samcloud trusts, and what the machine
offered. Treat it as a machine you have not reached, and file a ticket naming the
device and both fingerprints.

```
GET    /devices/{id}                     host_pubkey — the key samcloud trusts
DELETE /devices/{id}/host-key            accept a re-key: the admin, or that device
GET    /gateway/host-key                 the gateway's own host keys, so a device can
                                         verify the tunnel it dials (no bearer needed)
```

Clear the pin only when you know why the key changed — a device rebuilt or
re-keyed on purpose. The next hop pins what it then presents. The first hop to a
device with no pin records its key and needs nobody.

## Threads

```
GET  /threads                      the threads you hold: members, participants, open_members
GET  /threads/{id}                 one thread
GET  /threads/of/ticket/706        the thread an object belongs to (doc, task likewise)
POST /threads/{id}/members         {"kind": "doc", "id": 237}   move an object into a thread
```

**Create with `sc-new`, not bare curl.** Everything your conversation makes goes into its thread:

```
sc-new ticket --summary "…" --assignee someone     into your conversation's thread
sc-new task --project <slug> --title "…"
sc-new doc --title "…" -f draft.md
sc-new ticket --summary "…" --thread 1767          into another existing thread, on purpose
```

The plane places every create, in this order: the thread `--thread` names; a subtask's parent's; the thread you hold, if a rail started you; your conversation's own thread. Only your conversation's first create starts a thread. There is no way to start a second one — `--no-thread` is refused.

`sc-new` sends `X-Session-Id`, which is how the plane knows your conversation. A bare `curl` sends nothing: it binds nothing, and lands in your identity's own thread rather than this conversation's.

## Files

```
curl -s -H @"$HOME/.samcloud/token.hdr" -F file=@shot.png -F group_scope=projects \
     __API__/files                                                        upload
POST /files/{id}/links   {"target_type": "task", "target_id": "839"}        attach to a task, ticket or doc
PATCH /files/{id}        {"group_scope": "projects"}                                             re-scope it
GET  /files/{id}/raw                                                                             download
```

**A file is in exactly one group, like everything else.** `-F group_scope=<group>` names it;
omitted, it resolves the same way an unnamed ticket or doc does — your first editable group, which
for the admin is `samcloud-contributors` (`core` on a plane without it). **So name it when the readers are not in your default group**: a file
uploaded by the admin without `group_scope` lands in that group, and the seat you uploaded it *for*
reads 404.

**Who can read it is the union of its own group and whatever it is attached to** — a task, a ticket
or a doc. So either works: scope the file to the readers' group, or attach it to a task they can
already see. Attaching is usually the better one, because it keeps following the work if the group
changes.

**Embedding a file in a doc body does not attach it** — a markdown link is text, and only
`POST /files/{id}/links` creates the attachment the union reads. You can attach only to a doc,
ticket or task you can read; anything else answers 404.

**To change a file's group, `PATCH /files/{id}`** (owner or admin). Re-uploading makes a second
file and leaves the first where it was.

**1 GB per file, any type** — a screenshot, a 700 MB playthrough video and a
Gaussian splat capture are all held to the same ceiling. Two refusals to read
rather than retry:

- `413` — the file is over the cap. Nothing was kept, so retrying changes
  nothing. Compress it (a 1 GB `.ply` is usually a tenth of that as `.spz`), or
  put it where large media belongs and attach the reference.
- `507` — the file volume is nearly full, and its last 2 GB is reserved for the
  plane's own writes. **Not your upload's fault, and not fixable by retrying**:
  `registry.db`, the Caddyfile and the gateway keys share that volume, and the
  plane stops working before it would refuse an upload. Say so on the ticket you
  were attaching to, and name the file and its size.

Both refusals leave nothing behind, so neither needs cleaning up after.

## When something is wrong

Read this page again, then file a ticket with the pen so it joins the work:
`sc-new ticket --summary "…" --assignee __ADMIN__ < detail.md`.
