<!-- SKILL_VERSION: 3.25.0 -->
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
| `job` | `sc-job` — call a service: submit a job, wait for it, keep what it made ("Find and call a service") |
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

Do this once in each environment you work from: register it with your own token (`auth.md`, the gateway key). That writes your key and the `samcloud` ssh host.

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

**Pipe into a remote command without a terminal.** The `samcloud` host that the setup script writes uses `RequestTTY auto`, so `printf hello | ssh samcloud "<device> -- wc -c"` prints `5`. If your config says `RequestTTY force`, or you pass `-t`, the remote side gets a terminal and never sees end-of-input, so the command waits forever. Use `ssh -T` for anything you pipe in.

For one operation across hosts, `POST /lease-groups` takes a stable `request_key`
and 2–16 ordered `members` (`resource_id`, explicit `ttl_seconds`, optional
`memory_mb`, `service_id`, `max_total_s`). Every host must use HOLD. Members are
exclusive: `memory_mb` is the declared envelope checked against capacity, not a
measured footprint; any numeric remainder is unavailable to another allocation.
Members queue at normal priority; only one group acquires at a time, first waiting
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
For a never-serving setup with an entirely expired/unsettled HOLD pool, the owner/admin with current access
to all hosts may explicitly POST `/{id}/continue-after-cleanup` with
`{generation,request_key,members:[{lease_id,evidence_ref,evidence_sha256}]}`.
First positively observe cleanup on every exact old lease. Core atomically
settles those predecessors and creates a fresh bounded successor with the same
owner/resources/sizes/TTLs/exposure bounds, without a free-capacity window or an
intervening queue promotion. Partial/active/changed/foreign/settled claims or a
release request refuse. Replays return the current successor; conflicts refuse.
`continued_from`/`continued_to` link the groups. No expired lease is resurrected,
no process is started, and no inference is retried. This is explicit bounded
recovery, not unbounded turn lifetime or permission to extend unknown work.
An operation that already reached serving cannot use it to avoid normal cleanup
and queued admission at its exposure bound.
Only the owner/admin changes a group; full-group/history readers must see every
member. A host-local supervisor can instead GET
`/lease-groups/{id}/members/{lease_id}/authority` with visibility of that exact
member resource. Its non-cacheable projection contains current generation,
group phase/release flag, observation time, own lease and cleanup booleans; no
other hosts, intent or evidence. Invisible/nonmember ids are404. `authoritative`
is true only at that observed instant with the whole group admitted/serving and
every member's matching active, unexpired exclusive HOLD lease. All are checked
at the same observation instant. `authority_expires_at` is their earliest expiry,
or null when not authoritative. Bound local authority by that interval minus
the entire read duration, never by the own lease alone. A fence, release,
partial admission or any member's expiry removes authority without freeing a
claim. Pin and compare generation/lease/envelope; never treat a saved snapshot
as perpetual permission. Reads do not acquire, renew, release or grant writes.
The existing `/events` stream sends resource-scoped `lease.group_changed`
wakeups after committed changes, once per bound member. Each carries only that
member's ids plus group generation/phase and source event sequence. It grants
no authority or access to private group history. Re-read the projection on
connect/reconnect, before admission and on existing lease events/heartbeats;
SSE is best effort. Stable reads emit no wakeups. Unknown observations never
prove cleanup or allow a replacement.
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

## Bring a machine or an account onto the plane

A *device* is one account on one machine that the gateway can log in as. Make the OS account first: one for each seat or service, separate from anyone's own login.

```
POST /enroll {"device_id": "studio-services", "access_target": "direct-ssh",
              "target_ip": "192.168.1.50", "target_ssh_user": "studio-services",
              "group_ids": ["work"]}
```

The answer carries `bootstrap_command`, `curl -s __API__/bootstrap/<token> | bash`: one use, one hour. Run it once on the machine, signed in as `target_ssh_user`. It installs the gateway's key, pins the machine's host key, starts the tunnel for a `reverse-tunnel` device, writes the device token to `~/.config/samcloud/device_token`, and activates the device. A Windows machine takes `…/bootstrap/<token>/ps1`.

`access_target` is how the gateway reaches the account: `direct-ssh` to `target_ip` on the LAN, or `reverse-tunnel`, where the machine dials out and the answer names its `tunnel_port`. Enrolling takes the editor role: the device joins the `group_ids` you name, each one a group you edit, or else every group you edit. The admin can enroll into any group.

```
ssh samcloud "studio-services -- id"         the gateway reaches it, as target_ssh_user
GET  /devices/studio-services/guide          its route, services, and how to grant access
POST /enroll/studio-services/retoken         a fresh bootstrap token: a retry, or a re-key after a rebuild
GET  /enroll/pending                         enrolled, never activated (admin)
```

`device:<id>` lets a seat manage the device, and it is also that seat's way in through the gateway, as `target_ssh_user`. Give it only to a seat that should have that shell.

## Find and call a service

In each entry of `GET /services`:
- `id` is the service id, such as `wafer-services/scene-render`. A grant names it;
- `description` says what the service does and how to call it;
- `external_url` is where it answers, and `docs_endpoint`, when set, is where its caller guide is;
- `last_health_status` says whether it's up.

`GET /services/{id}/guide` is for whoever operates the service, not for callers.

**Calling one is three steps.** Use your own token as the bearer each time.
1. **Submit** the job to the `external_url`. The reply names the job.
2. **Poll** the job until its `status` is `done`, `failed` or `cancelled`. It goes `queued`, then `running`, then one of those three; they are the service's own words, so wait for them literally. A `failed` job says why. A service that lets you withdraw a job (scene-render does, with `DELETE` on the job) ends it `cancelled`.
3. **Download** what it made, from the service.

**Or in one command.** `sc-job` does all three, and with `--store` it mints the grant, keeps the results as plane files you own, and revokes the grant when the job ends:

```
curl -s -H @"$HOME/.samcloud/token.hdr" __API__/helpers/job > ~/.local/bin/sc-job && chmod +x ~/.local/bin/sc-job
sc-job list                                                     the services you can call
sc-job call wafer-services/scene-render /render '{"scene":"<id>","mode":"quick"}' --store <plane group>
sc-job fetch wafer-services/scene-render <job> [--grant <id>]   wait for a job you already submitted, download it, then revoke the grant
sc-job status wafer-services/scene-render <job>  ·  sc-job cancel wafer-services/scene-render <job>
```

It waits through dropped connections, 5xx and 429 answers. It stops waiting, keeping the grant and printing the `sc-job fetch` line, after five failed polls in a row, or twenty polls in a row that answer no status, or a status it doesn't know with no answer it hasn't given before (any field counts but clock-like ones such as `*_at` and `*_s`, so a job moving in its stage, progress or position is waited out, and one flapping between two statuses still ends). A download shorter than its Content-Length fails and leaves no file. It downloads into `./<job>/` (files get your umask's mode, 0644 by default) and prints the files, then each plane file it stored for you: its role, its id and the markdown that embeds it. If you stop it with Ctrl-C, the job carries on and the grant stays live until its ttl; it prints the `sc-job fetch … --grant <id>` line to resume (fetch revokes the grant once it has the results) and the call to revoke. SIGTERM or SIGHUP (unless started under `nohup`) revokes the grant, so the job's uploads to the plane will fail, and prints the fetch line; it does so too when it comes with a Ctrl-C. Exit: 0 done; 1 failed/cancelled/expired; 2 usage, HTTP or interrupted.

The service runs the job under its own lease. It posts nothing on the plane for you.

**Optional: keep the results as plane files you own.** Mint a grant, then add it to the job as a top-level `"grant"` field:

```
POST /grants {"service_id": "<the service's id>", "group_scope": "<a group you edit>", "ttl_s": 3600}
     → {"id": 12, "grant": "sc_grant_…", "expires_at": "…"}     the grant is shown once
```

- **`group_scope` is a plane group you edit,** one of `GET /groups`. A service's own groups, such as the registry's crew group, are a different thing and won't work here.
- When the job is `done`, `GET` on the job has the ids of the files it stored for you in `plane_files`. It's an object keyed by what each file is, not a list. scene-render's looks like `{"sheet": 4337, "shots_json": 4338, "shots_md": 4339, "clips": {}, "stills": {"weave-01.jpg": 4340, …}}`. `results`, beside it, holds the service's own paths. The files are in your group, owned by you, with `uploaded_by` naming the service.
- **The files in `plane_files` are already yours: embed those ids. Don't download them and upload them again.** A copy you upload has no `uploaded_by`, so it no longer shows which service made it.
- `GET /files/{id}` gives `name`, `bytes`, `content_type`, `owner`, `uploaded_by` and `url`. `markdown` is ready to paste into a comment to embed it.
- **Make the ttl a little longer than the job,** and revoke the grant once the job is `done`: `DELETE /grants/{id}`. `GET /grants` shows how many files each grant stored.
- A grant lets that one service upload into that one group until it expires, and nothing else. It's useless to anyone without that service's own token. **Never send your own token in its place.**

**The asset registry** (`wafer-services/asset-registry` in `GET /services`) holds the splats, scenes and bundles that services like scene-render and tsplat read and make. Its caller guide is the `docs_endpoint` on that record: compose a scene, edit it, render it and export it.
- **Your own samcloud token works there.** No other sign-in is needed, but your seat has to be on the registry's admission list. A `403` means it isn't: file a ticket asking to be admitted ("When something is wrong", below).
- **Its groups are its own.** A registry group id goes in registry calls; a grant's `group_scope` is a plane group.
- **A `404` means "not one you can read".** It's the same answer for a scene in a group you aren't in and for one that doesn't exist.
- **Which asset to place:** an uploaded PLY has no frame, so `fit: splatFrame` can't place it. Place the `splat-dgs` that `ply-to-dgs` makes from it, once that asset has a `frame` (`GET` it until it does). A scene-render preview plays the PLY directly and doesn't wait for that.

## Register a service

A service is something you run on a machine that others call. Register it so callers can find it in `GET /services` and, with a `subdomain`, reach it at the `external_url` the reply names.

**You can register it yourself** on a device you manage: one whose group you are an editor of (`GET /groups/{id}` lists its devices), or your own device scope. Anything else is the admin's.

**For a public route, the plane has to reach the device.** `GET /devices/{id}` shows how. On a `reverse-tunnel` device (with a `tunnel_port`) the route goes through the tunnel, so the service listens on `127.0.0.1`. Several accounts on one machine often share one tunnelled device (`wafer-services` on wafer, for example), and a service on the machine's loopback is reachable through it, whichever account runs it. On a `direct-ssh` device the route goes to its `outbound_ip`, so the service listens on that LAN address. A device with neither (no tunnel and no address) is registered but gets no working route.

```
curl -s -H @"$HOME/.samcloud/token.hdr" -H 'Content-Type: application/json' \
  -X POST __API__/services -d '{
    "name": "my-svc", "device_id": "<device>", "port": 8798, "subdomain": "my-svc",
    "health_endpoint": "/health", "description": "what it does, how to call it",
    "capabilities": ["…"]}'
```

- **One call does the whole route.** The id is `<device_id>/<name>`; send no `id`. With a `subdomain`, the same call sets up the forward (on a tunnelled device), the Caddy site and the DNS record. The reply has a `route` object with `forward`, `caddy`, `dns` and `verified`. Read each one: `"success": false` means that step failed, and the service is not reachable yet. A subdomain someone else has is `409`.
- **The reply carries the service's own token,** `service_token`. Keep it like your own token (a `0600` file, never in a command line). The service uses it to report health, `POST /services/{id}/health`, every few minutes. That keeps it `online` in `GET /services`. `guide_url` is its operations page, `GET /services/{id}/guide`, read with the same token.
- **Name the `subdomain` when you register.** `PATCH /services/{id}` can't add one or change the device. To move a service, register it again on the right device, switch the health reports to the new token, then `DELETE /services/{old id}`.
- **Before you share the URL,** call it from outside with no token: every route that needs sign-in answers `401`.
- **Who sees it:** the groups that hold its device.
- **Write for callers who read nothing else.** "The standard", below, says what the entry and the guide hold. A caller who has to guess a step will get it wrong.
- When a service takes compute for its callers, `queue.md` has what it owes the queue.

**A registered service acts as itself for its own compute and as its caller for everything else.**
- **Compute:** it leases with its own service token, on the device it runs on (`POST /resources/{id}/leases`). The lease names the service as its holder. It can't lease another device's compute, or lease as another service.
- **Through compute-jobs:** set `"service": "<service id>"` in the service's compute-jobs config, beside `expected_principal`, and use a compute-jobs with the service-actor fix (`samtg-projects/compute-jobs` PR #4). Without `service`, compute-jobs saves its intent with no service, the plane names the service as holder, and compute-jobs refuses its own lease and can't release it (`service attribution does not match saved intent`). Release such an orphan with `DELETE /leases/{id}` and the service token.
- **No reach of its own into the plane's content:** it posts no comments and links nothing.
- **Reads for a caller:** the caller's bearer, checked with `GET /auth/verify`, used during that request and never stored.
- **Results as plane files:** `POST /files` with the service token, `-F grant=<the caller's grant>` and no `group_scope`. The file lands in the grant's group, owned by the caller, with `uploaded_by` naming the service. A grant that has expired or been revoked, or whose caller no longer edits the group, is refused with `403`: report that in the job.
- **Check the grant at submit, before any work:** `POST /grants/check {"grant": "…"}` with the service token. It runs the upload's checks without using the grant, and answers `{"valid": true, "creator", "group_scope", "expires_at"}` or `{"valid": false, "status", "reason"}`. Refuse the job if it isn't valid, or if `creator` isn't the caller whose bearer submitted it.
- **Its own API returns the results:** submit, then poll (`queued`, `running`, `done` or `failed`, with the reason and the log tail when it failed), then download. Check the caller's bearer on every read, not only at submit. Say in the job how long results are kept. Return the file ids of anything uploaded on a grant.
- **The shape `sc-job` relies on,** so callers need no recipe of yours: the submit reply names `job` (and `poll`, if the job isn't at `/jobs/{job}`); the poll answers `status` (`queued`, `running`, then `done`, `failed` or `cancelled`), with `error` when it failed; `results` names each downloadable file by its path under `/jobs/{job}/files/`; `plane_files` holds the ids of what was uploaded on a grant; `DELETE` on the job withdraws it.

```
POST   /services/{id}/health      with the service token: marks it online
PATCH  /services/{id}             port, description, health_endpoint, docs_endpoint, version, capabilities
DELETE /services/{id}             the service, its route, its DNS record and its token
```

**The standard: what a service owes its callers** (doc 793). Declare your kind with one capability word. What's critical depends on it; everything else is a note that never blocks. `sc-job check <service>` reads both and names each gap.

| kind (capability) | for | critical |
|---|---|---|
| `compute-job` | it leases compute and returns files (tsplat, image gen, scene-render) | it leases and releases as its own service id (`queue.md`); the `sc-job` shape, above, with `{}` refused by a 4xx JSON error; results to the caller's grant |
| `compute-sync` | it leases compute and answers in the call (model-service) | it leases as its own id; callers queue through it (doc 768) |
| `no-compute` | it holds no lease (asset-registry, the file plane) | its own id only where it writes for a caller, by grant |
| `pointer` | a web app or a page a person opens | an address and a one-line description. Auth is opt-in, and there's no lease or queue, even if compute sits behind it |

**Except `pointer`, every kind also owes these:**
- an https address;
- every call checks the bearer: no bearer gets `401`;
- **one guide that opens from outside** (`docs_endpoint`, a path on the service or a plane doc URL), giving the call, its inputs and where results come from.

**The notes:**
- `version` as `<semver>+<sha>`, the same on every host running the same code;
- `capabilities` in caller words (`image-generation`, `image-to-splat`);
- a `description` written for a caller: the one call, its statuses, where results come from;
- health: after 30 minutes without a report, an `online` entry reads `stale`, and `health_age_s` says how long. Callers use it when it's there. Only `online` ages. An entry stored as `active`, or one that has never reported, reads as up although nothing checked it, so read `health_age_s` rather than the status.
- in the guide, beyond the call: the inputs' limits, the errors, measured time and memory per call, and the hosts it runs on.

scene-render (`wafer-services/scene-render`, doc 750 §3) is the worked example.

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

**The upload answers with `markdown`, a ready embed** — `![shot.png](/api/v1/files/623/raw)` for an
image, a plain link for anything else. Paste that into the comment or doc, not a path on your
machine: a host path is something nobody else can open.

**Embedding a file in a doc body does not attach it** — a markdown link is text, and only
`POST /files/{id}/links` creates the attachment the union reads. You can attach only to a doc,
ticket or task you can read; anything else answers 404.

**A file a service uploaded on your grant is yours** (`owner` is you, `uploaded_by` names the service), in the group the grant named: "Find and call a service".

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

Continuation uses ordinary lease accounting for competing ownership. Historical
expired waits and legacy non-HOLD expiries are not charged; active leases and
expired/unsettled TTL holds still block. It never deletes historical rows.
