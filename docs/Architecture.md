# samcloud Architecture

How the parts fit: topology, components, the state each part owns, data flows, containers, networking, volumes, ports, the stack, the design decisions and the file map. Each part's own page holds its detail; this page links to it rather than repeating it.

> **The registry describes itself.** `GET /api/v1/` is a curated index of the API and names the running release; `registry/main.py` is the source. Devices, services, users and routes are the registry's own data, read through the API. Where this page and a live answer disagree, the live answer wins: this page explains *what the parts are and why*, not how many of anything there are today. `python3 sandbox/docs_coverage.py` checks this page against the code.

---

## The map on one screen

```
people and agents ──HTTPS──▶ Caddy ──▶ console (dashboard)      ──▶ registry (API, SQLite)
                     │                 /api/v1/* ───────────────▶ registry
                     └─SSH──▶ sshd + gateway ──▶ devices, by LAN or reverse tunnel
devices ──reverse tunnel──▶ sshd                 services ◀── Caddy ◀── routes
registry ──SSE /events──▶ each seat's listener ──▶ rail ──▶ holder (Claude, Codex, Hermes) ──sc-say──▶ registry
```

| part | what it is | its page |
|---|---|---|
| registry | the API and the record: identity, devices, services, routes, tickets, docs, tasks, files, leases, notifications, the event bus | [API Reference](API%20Reference.md) |
| console | the dashboard | [Dashboard](Dashboard.md) |
| gateway | the SSH entry point: identity, device routing, access control | [SSH Gateway](SSH%20Gateway.md) |
| Caddy, sslh | TLS, the reverse proxy for every route, SSH and HTTPS on one port | [DNS and TLS](DNS%20and%20TLS.md) |
| the rail | delivery of messages to agents while they run | [Agent Rail](Agent%20Rail.md) |
| brokers | short-lived logins to GitHub, AWS, Google and adb | [Credential Brokers](Credential%20Brokers.md) |

### The planes

The registry's collaboration records are planes: kinds of object with their own rules, all scoped by group and all delivering through the same notifications.

| plane | what it is | write-once | emits | page |
|---|---|---|---|---|
| tickets | a request needing someone else | `summary`, `body` | `ticket.created`, `ticket.updated` (status, assignee and priority changes: resolve and assign are not events of their own), `ticket.commented`, `ticket.mentioned` | [Tickets](Tickets.md) |
| docs | a canonical record with revisions | nothing: a body changes by a new revision against `base_rev`, and PATCH is metadata only | `doc.updated`, `doc.revised`, `doc.commented`, `doc.mentioned`, `doc.published`, `doc.linked` | [Docs](Docs.md) |
| tasks | an actionable item under a project | — | `task.created`, `task.assigned`, `task.delegated`, `task.commented`, `task.mentioned`, `task.updated` | [Tasks and Schedules](Tasks%20and%20Schedules.md), [Projects](Projects.md) |
| notifications | one row per (event, recipient) | — | `wake.probe`, `schedule.fired` | [Notifications and Subscriptions](Notifications%20and%20Subscriptions.md) |
| files | evidence attached to threads; `/files/{id}/raw` takes a bearer | — | — | [Files](Files.md) |

Who receives a row about an object (its conversation audience, update subscribers, mentions and assignment) is set out in [Notifications and Subscriptions](Notifications%20and%20Subscriptions.md). Recipients are scope-filtered on every plane: an identity that can't open an object receives no row about it. List routes are scope-narrowed and are not a census; a by-path `GET` is the only existence test.

---

## Network Topology

```
                          Internet
                             |
                    :443 / :80 / :2222
                             |
     ┌───────────────────────┴────────────────────────┐
     │               CONTROL-PLANE HOST                │
     │   Docker: native on Linux, Colima on macOS      │
     │                                                 │
     │   compose stack                                 │
     │     sslh  :443  → SSLH_PORT  (demux SSH/HTTPS)  │
     │     caddy :443  → HTTPS_PORT                    │
     │     caddy :80   → HTTP_PORT                     │
     │     sshd  :22   → SSH_PORT   (gateway binary    │
     │                               as ForceCommand)  │
     │     registry :7777 → REGISTRY_PORT              │
     │     console  :3000 → CONSOLE_PORT               │
     │                                                 │
     │   Host processes (optional):                    │
     │   credential brokers :7781/:7782/:7783          │
     │   deploy receiver :9911                         │
     └──┬─────────────────────┬────────────────────────┘
        │                     │
   ┌────┴────────────┐  ┌─────┴─────────────────────┐
   │ LAN devices     │  │   Reverse SSH Tunnels     │
   │ (direct-ssh)    │  │  terminate INSIDE the     │
   │                 │  │  sshd container           │
   │ box-1           │  │                           │
   │ mediahost       │  │  gpu-1, gpu-2 (behind NAT)│
   │ agent seats     │  │                           │
   │ IoT (HTTP/OTA)  │  │  tunnel range   10001-…   │
   └─────────────────┘  │  forward range  18001-…   │
                        │                           │
                        │  Live map is canonical:   │
                        │  GET /api/v1/ports        │
                        └───────────────────────────┘
```

> Tunnel and forward port assignments are **not** listed here on purpose: they are allocated at enrolment and any hardcoded list rots. `GET /api/v1/ports` is the only correct answer.

### Connection Types

| Type | Devices | How It Works |
|------|---------|-------------|
| LAN direct | servers and agent seats on the control plane's LAN (`box-1`, `mediahost`), IoT devices | The control-plane host reaches them by LAN IP (`outbound_method=direct-ssh`) |
| Reverse SSH tunnel | devices behind NAT (`gpu-1`, `gpu-2`) | The device opens a tunnel to the gateway's SSH port (e.g. `tunnel.example.org:2222`); it terminates inside the sshd container |
| sslh multiplex | any seat behind a network that blocks the SSH port | SSH ridden in over :443 and demuxed by sslh |

Windows hosts are device rows of their own. The `lifeline_port` and `monitor_port` columns and their ranges still exist in `registry/ports.py`, but nothing allocates in either: in practice `GET /api/v1/ports` returns `tunnel`, `forward` and `route` rows. [Devices and Enrolment](Devices%20and%20Enrolment.md) covers how a device joins, and targets versus routes.

---

## Component Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                CONTROL PLANE (control-plane host)               │
│              one compose stack: deploy/docker-compose.yml        │
│                                                                 │
│  ┌───────────┐  ┌───────────┐  ┌───────────┐                  │
│  │  Registry  │  │  Console  │  │  Gateway   │                  │
│  │  (FastAPI) │  │ (Next.js) │  │   (Go)    │                  │
│  │   :7777    │  │   :3000   │  │ ForceCmd  │                  │
│  └──┬──┬──┬──┘  └─────┬─────┘  └──┬────┬───┘                  │
│     │  │  │            │           │    │                       │
│     │  │  │      ┌─────┴─────┐     │    │                      │
│     │  │  │      │ Caddy     │     │    │                      │
│     │  │  │      │ :443/:80  │     │    │                      │
│     │  │  │      └───────────┘     │    │                      │
│     │  │  │                        │    │                      │
│  ┌──┴──┴──┴────────────────────────┴────┴──┐                   │
│  │           SQLite (WAL mode)              │                   │
│  │   /data/registry.db (registry-data vol)  │                   │
│  └──────────────────────────────────────────┘                   │
│                                                                 │
│  ┌──────────────────────────────────────────┐                   │
│  │           Registry Modules               │                   │
│  │                                          │                   │
│  │  main.py ─── the API (/api/v1/)          │                   │
│  │  enrollment.py ─── token lifecycle       │                   │
│  │  events.py ─── in-process event bus      │                   │
│  │  dns.py ─── Namecheap A records          │                   │
│  │  forward.py ─── SSH -L automation        │                   │
│  │  caddy.py ─── config gen + reload        │                   │
│  │  ports.py ─── port allocator             │                   │
│  │  authorized_keys.py ─── key sync         │                   │
│  └──────────────────────────────────────────┘                   │
│                                                                 │
│  Host processes, outside the stack, all optional:               │
│    deploy receiver (systemd) :9911                              │
│    gh/aws/google brokers :7781/:7782/:7783 (LaunchAgents on     │
│      macOS), adb-broker :7791 (by hand)                         │
│  On each seat: the rail's listener (com.samcloud.listen.<user>  │
│    on macOS)                                                    │
└─────────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Language | Purpose |
|-----------|----------|---------|
| Registry | Python (FastAPI) | CRUD for every entity, enrolment, auth, events, DNS, port forwarding, Caddy integration, the docs, tasks and notifications planes, the rail's fleet state |
| Console | TypeScript (Next.js) | The web dashboard and pair page. Apps an instance adds (a media library, home automation) run as services on its devices and register with the registry; they aren't part of the console |
| Gateway | Go (stdlib only) | SSH ForceCommand shell: user identity, device routing, access control |
| Caddy | Go (binary) | TLS termination and reverse proxy; certificates by ACME DNS-01 over Namecheap when the module's credentials are set, else as [DNS and TLS](DNS%20and%20TLS.md#tls) describes |
| sslh | C (binary) | Port 443 multiplexer (SSH vs HTTPS), for networks that only let 443 out |

---

## Auth

Identity and scope have their own page, [Identity and Access](Identity%20and%20Access.md). In short:

- **Tokens are prefix-typed** and stored as SHA-256 hashes in the `tokens` table: `sc_agent_` (people and agents alike; an admin is an `sc_agent_` token with scopes `["*"]`), `sc_device_`, `sc_service_`, and the single-use `sc_invite_`, `sc_enroll_` and `sc_pair_`. A dashboard or API login gets its own `sc_device_` token that expires after a week. There is no `sc_admin_`.
- **A bearer is presented from a 0600 header file** (`curl -H @"$HOME/.samcloud/token.hdr"`), never on a command line, where `ps` shows it to every account on a shared box. Header files are plane-scoped.
- **Scope is the group.** Tokens carry scopes (`*`, `group:<id>`, `device:<id>`, `service:self` and others), and each group gives its members a role: viewer, editor or owner. Everything a caller can see and do through the API, the dashboard and the gateway follows from them.
- **Group scope reaches further than it looks.** Gateway admission granted by group membership lands the caller as the target seat's own Unix account, which owns that seat's 0600 token file. Group-scoped access to a seat's device is therefore equivalent to holding that seat's registry bearer.
- **The gateway admits in five stages:** SSH lands in the gateway process, not a shell; the key's comment identifies the user and device; the registry answers what that user may reach (groups, roles, scopes); the gateway does the plumbing (direct SSH or reverse tunnel); the target environment depends on the identity.

Ticket scoping and the other per-object rules are on each primitive's page.

---

## State, and who owns it

| state | where | owner | lifetime |
|---|---|---|---|
| devices, services, routes, users, groups, tokens | registry tables | plane | until deleted |
| tickets, docs, tasks, projects, files | registry tables | plane | until deleted (docs and projects prefer `archived`) |
| the conversation ↔ object ↔ identity binding | registry `threads` (`GET /api/v1/wake/threads`); each seat's `threads.json` is its local cache | plane | open → closing → closed |
| liveness lease on a thread | registry `thread_claims` (`session_id`, `claimed_until`) | plane | minutes; refreshed by writes and watches |
| what a listener last said about itself | registry `wake_registrations` | plane | until the next connect |
| pids, FIFOs, generation files | the seat's `~/.samcloud/rail-v3-<plane>/threads/<key>/` | seat | the process |
| **delivered** (the rail wrote the row into a live process) | notification row `delivered_at` / `delivered_to` | rail | permanent |
| **read** / acked | notification row | the reader | the reader's choice |
| the live Caddyfile | `registry-data` volume | registry (after first boot) | until removed |

Delivered and read are different facts with different owners. A delivered row is never offered again, to anyone; a new instance reads its thread from the plane. [Agent Rail](Agent%20Rail.md) and [Thread Lifecycle](Thread%20Lifecycle.md) cover the rail's side.

### Where to look

| question | look at |
|---|---|
| what did this seat's rail do | `~/logs/samcloud-wake.log` (verbs) and `~/logs/samcloud-listen.log` (connects, updates) on the seat |
| what did a holder receive and answer, and at what cost | the thread's `in.jsonl` and `out-*.jsonl` under the seat's rail directory. Cost is **tokens per turn**: the `usage` block on each `result` line |
| who is holding what | `GET /api/v1/wake/claims` (lease), `GET /api/v1/wake/threads` (binding) |
| is a seat current, or dark | `GET /api/v1/wake/registrations`, which reports what the listener said at connect, and `GET /api/v1/wake/health` |
| what is blocked on me | `GET /api/v1/users/me/briefing` |
| what just happened | `GET /api/v1/events/recent` |

---

## Database Schema

SQLite in WAL mode, with foreign keys enforced. `registry/schema.sql` is the full definition. Core inventory:

| Table | Purpose | Key Columns | Page |
|-------|---------|-------------|------|
| `devices` | Network inventory | id, owner_id, inbound/outbound methods, tunnel_port, enrolled, status | [Devices and Enrolment](Devices%20and%20Enrolment.md) |
| `services` | Running services | id (device/name), port, slice_port, subdomain, health_endpoint, created_by, status | [Services and Routes](Services%20and%20Routes.md) |
| `ports` | System-wide port uniqueness | port, type (tunnel/lifeline/forward/route/local/monitor), owner_id | [Services and Routes](Services%20and%20Routes.md) |
| `routes` | Caddy reverse proxy routes | subdomain, domain, target, service_id, caddy_extra | [Services and Routes](Services%20and%20Routes.md) |
| `users` | Human and agent accounts | username, role (admin/user/agent), scopes | [Identity and Access](Identity%20and%20Access.md) |
| `groups`, `group_services`, `group_members`, `group_devices` | Grouping and membership | group_id, service/user/device refs, role | [Identity and Access](Identity%20and%20Access.md) |
| `tokens` | All auth tokens (hashed) | token_hash, type, owner_id, scopes, expires_at, last_used | [Identity and Access](Identity%20and%20Access.md) |
| `tickets`, `ticket_comments`, `ticket_watchers` | Requests, their comments and subscriptions | source, type, priority, status, assignee, group_scope | [Tickets](Tickets.md) |
| `audit_log` | All mutations | actor, action, target, detail (JSON) | [API Reference](API%20Reference.md#system) |

| Table group | Purpose | Page |
|---|---|---|
| `docs`, `doc_comments`, `doc_links`, `doc_revisions`, `doc_watchers` | Shared canonical records with revisions and subscriptions | [Docs](Docs.md) |
| `files`, `file_links` | Files attached to threads | [Files](Files.md) |
| `projects`, `project_watchers`, `tasks`, `task_comments` | Projects and tasks, with computed progress | [Projects](Projects.md), [Tasks and Schedules](Tasks%20and%20Schedules.md) |
| `resources`, `leases`, `lease_rollovers` | Compute inventory and time-boxed leases. A resource's `allocation_mode` decides exclusivity, not the caller's flag: on an `exclusive` resource every lease is exclusive and at most one is active, guaranteed by a partial unique index rather than by a check the next request can step over | [Compute Resources and Leases](Compute%20Resources%20and%20Leases.md) |
| `views` | Saved cross-entity queries | [Dashboard](Dashboard.md) |
| `notifications` | Per-identity durable inbox (seen, delivered and ack state) | [Notifications and Subscriptions](Notifications%20and%20Subscriptions.md) |
| `threads`, `work_threads`, `conversation_threads`, `conversation_origins`, `surfaces`, `thread_claims`, `thread_mutes` | Threads, the conversations bound to them, which surface holds them, and mutes | [Thread Lifecycle](Thread%20Lifecycle.md) |
| `wake_registrations`, `wake_dark_state`, `wake_session_stats`, `wake_auth_refusals` | The rail's fleet state | [Rail Operations](Rail%20Operations.md) |

---

## Registry modules

| module | what it does | page |
|---|---|---|
| the API (`main.py`) | one FastAPI app under `/api/v1/` | [API Reference](API%20Reference.md) |
| event bus (`events.py`) | in-process pub/sub, delivered as an SSE stream (`GET /api/v1/events`) with a polling fallback (`GET /api/v1/events/recent`). It is the low-level primitive, not the agent inbox | [Event Bus](Event%20Bus.md) |
| DNS (`dns.py`) | the optional `dns-namecheap` module: one A record per service subdomain, read-modify-write because Namecheap's `setHosts` replaces every record | [DNS and TLS](DNS%20and%20TLS.md) |
| forward (`forward.py`) | an SSH local forward inside the sshd container, from a forward-range port to the service's port through the device's reverse tunnel, so Caddy can reach a tunnelled service | [Services and Routes](Services%20and%20Routes.md) |
| Caddy (`caddy.py`) | appends and removes route blocks in the live Caddyfile and reloads Caddy through its admin API | [DNS and TLS](DNS%20and%20TLS.md#the-caddyfile) |
| ports (`ports.py`) | the port allocator: ranges, the ledger, atomic allocation | [Services and Routes](Services%20and%20Routes.md) |
| enrolment (`enrollment.py`) | token lifecycle: generate, validate, verify | [Identity and Access](Identity%20and%20Access.md) |
| authorized_keys (`authorized_keys.py`) | writes the managed `authorized_keys` the gateway's sshd reads | [SSH Gateway](SSH%20Gateway.md) |

---

## The rail

Agents are not reached by polling. The **rail** delivers messages on threads to agents while they run: a listener on each seat holds the registry's event stream, and on each event runs the rail, which births a holder for a thread, writes later messages into that same live process, resumes it after a death or reboot, and closes it when the thread's work is done. Nothing closes a holder on a timer. [Agent Rail](Agent%20Rail.md) is the model; [Rail Clients](Rail%20Clients.md) covers Claude, Codex and Hermes.

Registry surface: `POST /wake/{register,beacon,session,threads,surfaces}`, `POST /wake/claims/{refresh,release}`, `GET /wake/{claims,registrations,threads,surfaces,health}`, `/threads`, and `POST /agents/{username}/wake` ([API Reference](API%20Reference.md#the-rail)).

## Served helpers and skills, rendered from config

The registry serves the rail and the agent tools from `registry/helpers/` (`GET /api/v1/helpers/{name}`: `rail`, `listen`, `wake`, `adapter`, `wake-install`, `say`, `new`, `edit`, `pr`, `lease`, `subscribe`, `channel`, `scope-check`, `wsl-boot`) and the agent skill from `registry/skills/` (`GET /api/v1/skills/…`). Seats keep them current by content stamp: the listener swaps a file whose stamp differs from the plane's. The repo holds placeholders, so it names no plane and no seat; the plane fills them in when it serves a file:

| placeholder | filled with | in |
|---|---|---|
| `__API__` | `PUBLIC_URL` + `/api/v1` | helpers; skill pages `SKILL.md`, `auth.md`, `collab.md`, `contributing.md`, `docs.md`, `tickets.md`, `tools.md` |
| `__ADMIN__` | `SAMCLOUD_ADMIN_SEAT`, else the plane's first admin user (the one `POST /init` made), else `admin`: the seat the pages tell an agent to file problems to | skill pages `SKILL.md`, `auth.md`, `collab.md`, `docs.md`, `tasks.md`, `tickets.md`, `tone.md`, `tools.md` |
| `__GH_BROKER__`, `__GOOGLE_BROKER__`, `__AWS_BROKER__` | `GH_BROKER_URL`, `GOOGLE_BROKER_URL`, `AWS_BROKER_URL`, else `ghb.`, `gglb.` and `awsb.` under the service domain | skill page `auth.md` |
| `__REPO__` | `SAMCLOUD_REPO`, else `OWNER/samcloud` | skill page `contributing.md` |
| `--plane` presets | `SAMCLOUD_PLANES`, in order; unset, only this plane under its `SAMCLOUD_ENV` name. Picking a plane picks where a write goes, nothing else | helpers |
| broker URLs | `GH_BROKER_URL` and `RAIL_TOKEN_MINT_URL`, else `ghb.` and `gglb.…/claude-token` under the service domain | helpers |
| the operator's time | `OPERATOR_TZ` | helpers |
| the header and token file names | `stg-token.hdr` and `stg-token` when `SAMCLOUD_ENV=staging`, else `token.hdr` and `token` | helpers |

The skill bundle is [`registry/skills/samcloud-agent/`](../registry/skills/samcloud-agent/SKILL.md); [Skills](Skills.md) says what each page holds and how it maps onto these docs. The served bytes differ from the repo's by design, so verify a served file from a seat, not from the repo. The rail, the adapter and the subscribe helper also carry a fixed plane URL as an unserved fallback.

## Credential brokers

FastAPI services on a host, **outside** the compose stack. Each verifies the caller's samcloud bearer against the registry, mints a short-lived login, and appends to a JSONL audit; long-lived secrets never leave its host. gh (7781), aws (7782) and google (7783) are installed as LaunchAgents by `plists/install-broker.py`; adb (7791) is run by hand. [Credential Brokers](Credential%20Brokers.md) covers each.

---

## Data Flow: Enrolment to Access

### Access Device Enrolment (phones, laptops)

```
User                    Console                  Registry               authorized_keys
 │                        │                        │                        │
 ├─ visit /pair ─────────►│                        │                        │
 │                        │                        │                        │
 ├─ enter name+device ───►│                        │                        │
 │                        ├─ POST /access-keys ───►│                        │
 │                        │                        ├─ ssh-keygen ed25519    │
 │                        │                        ├─ store pubkey in DB    │
 │                        │                        ├─ sync() ─────────────►│
 │                        │                        │                   write managed
 │                        │                        │                   lines with
 │                        │                        │                   ForceCommand
 │                        │◄─ {private_key} ───────┤                        │
 │◄─ download key ────────┤                        │                        │
 ╵                        ╵                        ╵                        ╵
```

[Devices and Enrolment](Devices%20and%20Enrolment.md) covers access keys, pair links and bootstrap.

### Service Enrolment (slot-based)

```
Admin                Registry                   Agent/Device
 │                      │                           │
 ├─ POST /services/     │                           │
 │    enroll ──────────►│                           │
 │                      ├─ create slot (pending)    │
 │                      ├─ generate sc_enroll_ token│
 │◄─ {token, slot} ────┤                           │
 │                      │                           │
 ├─ hand token ─────────┼──────────────────────────►│
 │                      │  GET /enroll/services/    │
 │                      │◄──── brief/{token} ───────┤
 │                      ├─ return briefing doc ────►│
 │                      │  POST /services/activate  │
 │                      │◄──── {token, port, ...} ──┤
 │                      ├─ validate token           │
 │                      ├─ allocate slice_port      │
 │                      ├─ create route             │
 │                      ├─ start SSH forward        │
 │                      ├─ add DNS record           │
 │                      ├─ reload Caddy             │
 │                      ├─ generate sc_service_ token│
 │                      ├─ emit service.activated   │
 │                      ├─ return guide (markdown)  │
 │                      │────────────────────────── ►│
 ╵                      ╵                           ╵
```

[Services and Routes](Services%20and%20Routes.md) covers slots, activation and routes.

### SSH Connection Through Gateway

```
User (with key)        sshd container         Gateway              Registry
 │                        │                      │                     │
 ├─ ssh samcloud ────────►│                      │                     │
 │                        ├─ match key           │                     │
 │                        ├─ ForceCommand ───────►│                     │
 │                        │                      ├─ GET /users ───────►│
 │                        │                      │◄─ {role, scopes} ───┤
 │                        │                      ├─ GET /auth/         │
 │                        │                      │   accessible-devices►│
 │◄─ interactive menu ────┤◄─────────────────────┤                     │
 │                        │                      │                     │
 ├─ "<device> -- <cmd>" ─►│─────────────────────►│                     │
 │                        │                      ├─ GET /auth/         │
 │                        │                      │   can-access ──────►│
 │                        │                      ├─ GET /devices/{id} ►│
 │                        │                      │◄─ {method, port} ───┤
 │                        │                      ├─ exec ssh via       │
 │                        │                      │   tunnel or LAN     │
 │◄═════ stdout + real exit code ═══════════════►│                     │
```

[SSH Gateway](SSH%20Gateway.md) covers connecting, commands, device routing and host-key trust.

---

## Containerized Control Plane

The control plane runs as **one Docker Compose stack**, built from `deploy/docker-compose.yml` with an env file (`deploy/.env.example` lists what an operator sets). The stack has its own `controlplane` bridge network; only external-facing ports are published to the host.

On a Linux host, Docker is a systemd service enabled at boot. On a macOS host, Colima runs it, for example as the brew service `homebrew.mxcl.colima`. Running a second instance on the same host is the operator's choice: it needs its own project name, host ports and ranges.

### Containers

| Container | Image | Internal | Published | Purpose |
|-----------|-------|----------|-----------|---------|
| registry | `Dockerfile.registry` | :7777 | `${REGISTRY_PORT}` | FastAPI + SQLite, the source of truth |
| console | `Dockerfile.console` | :3000 | `${CONSOLE_PORT}` | Next.js dashboard; proxies to the registry |
| caddy | `Dockerfile.caddy` | :443, :80, :2019 | `${HTTPS_PORT}`, `${HTTP_PORT}`, the route range | Reverse proxy, TLS. Admin API :2019 is **internal only** |
| sshd | `Dockerfile.sshd` | :22, the tunnel and forward ranges | `${SSH_PORT}` **only** | SSH gateway (Go binary as ForceCommand) and tunnel termination. The tunnel and forward ranges are **internal to the container**, never published; Caddy reaches them as `sshd:<port>` |
| sslh | `Dockerfile.sslh` | :443 | `${SSLH_PORT}` | SSH/TLS multiplexer |

Compose also has a `sim` profile, which adds containers that act as devices, and a `seed` profile, which seeds data, for testing a plane with no real machines.

**The live Caddyfile** lives on the shared `registry-data` volume. `deploy/caddy-entrypoint.sh` writes it on first boot; after that `registry/caddy.py` changes it and reloads Caddy over its admin API on `caddy:2019`. [DNS and TLS](DNS%20and%20TLS.md#the-caddyfile) covers it.

### Internal Networking (Docker bridge)

```
console  ──→  registry:7777     (API proxy via Docker DNS)
caddy    ──→  console:3000      (reverse proxy)
caddy    ──→  registry:7777     (API direct, SSE streaming)
caddy    ──→  sshd:18xxx        (forwarded services)
sslh     ──→  caddy:443         (TLS traffic)
sslh     ──→  sshd:22           (SSH traffic)
gateway  ──→  registry:7777     (auth, device lookup, via the ForceCommand env)
registry ──→  sshd:22           (sets up service forwards, as the samcloud user)
registry ──→  caddy:2019        (route changes)
```

### Persistent Volumes

| Volume | Mount | Contents |
|--------|-------|----------|
| `registry-data` | `/data` (registry, rw), `/registry-data` (caddy **rw**: the entrypoint seeds the Caddyfile there); sshd mounts only two files from it, read-only, by subpath: `authorized_keys` and `keys/gateway` | DB, keys, authorized_keys, Caddyfile, file store |
| `caddy-data` | `/data` (caddy) | TLS certificates |
| `caddy-config` | `/config` (caddy) | Caddy runtime config |
| `sshd-keys` | `/etc/ssh/keys` (sshd) | SSH host keys (survive restart) |
| `sshd-hostpub` | `/etc/ssh/hostpub` (sshd), `/sshd-hostpub` (registry, read-only) | the public halves of sshd's host keys, so the registry can verify what it connects to without being able to impersonate the gateway |

> **A subpath mount needs its file to exist, so the registry writes both on first boot.** On an empty volume the registry's startup generates `keys/gateway` and runs the `authorized_keys` sync before sshd starts (`depends_on: service_healthy`); an existing `authorized_keys` is left alone.

> **`registry-data` is shared, so uploads are capped by a reserve.** The same volume holds `registry.db`, the gateway keys, `authorized_keys`, the Caddyfile *and* `/data/files`, on the host's disk (under Colima, the VM's disk). Filling it is a control-plane outage, not a failed upload: SQLite fails every write, and because every authenticated request writes the token's `last_used`, the whole API fails with it. Container logs are capped in the compose file for the same reason.
>
> `POST /files` therefore enforces two numbers, both env-tunable on the registry container (`REGISTRY_FILE_MAX_MB`, `REGISTRY_FILE_RESERVE_MB`):
>
> | | default | meaning |
> |---|---|---|
> | size cap | 1 GB, every content type | `413` once the arriving bytes pass it |
> | free reserve | 2 GB | `507` once the bytes arriving would leave less than this free |
>
> The reserve is the one that protects the plane; the size cap just stops a single upload being absurd. A size cap alone never could: enough uploads under any cap still fill a disk. Uploads and downloads stream, so a 1 GB file costs one buffer in the registry, not its size in memory. Multi-GB media does not belong here: it wants object storage, with `/files` holding the reference.

### Deployment

Install, upgrade and release are on [Deployment](Deployment.md). You can run compose by hand. The repo's helpers (`.githooks/deploy-lib.sh`, `.githooks/post-merge`) recreate only the services whose source changed since the last release, with `--no-deps`, so a registry change doesn't drop every reverse tunnel by recreating `sshd`. They can be wired to a git hook or to the deploy receiver, which turns a merged pull request into a release.

---

## Port Map

### Production (host-published)

The container ports are fixed; the host ports are settings ([Configuration](Configuration.md#ports-and-ranges)). The third column is a plane published directly on 443, as the reference deployment is.

| Container port | Setting (compose default) | A plane on 443 | Bound by | Notes |
|------|------|------|----------|-------|
| sslh :443 | `SSLH_PORT` (9443) | 443 | sslh | SSH/HTTPS multiplexer: the public entry |
| caddy :80 | `HTTP_PORT` (9080) | 80 | caddy | ACME and redirects |
| caddy :443 | `HTTPS_PORT` (8444) | 8443 | caddy | Caddy's HTTPS, behind sslh |
| sshd :22 | `SSH_PORT` (2200) | 2222 | sshd | SSH gateway (`tunnel.example.org:2222`) |
| registry :7777 | `REGISTRY_PORT` (7778) | 7776 | registry | FastAPI |
| console :3000 | `CONSOLE_PORT` (3010) | 3002 | console | Next.js |
| caddy, the route range | `ROUTE_RANGE_START`–`END` (18101–18110) | the same | caddy | one port per route, only when services go under an IP address |

On the host, outside the stack:

| Port | Bound by | Notes |
|------|----------|-------|
| 7781 / 7782 / 7783 | gh- / aws- / google-broker | Credential brokers (LaunchAgents on macOS) |
| 7791 | adb-broker | Run by hand |
| 9911 | deploy receiver | `DEPLOY_HOOK_PORT`; Caddy proxies the webhook to it |

**Not host-published:** the tunnel range (`TUNNEL_RANGE_START`–`END`, 10001–10099 by default) and the forward range (`FORWARD_RANGE_START`–`END`, 18001–18099) are internal to the sshd container; Caddy's admin API `:2019` is internal to the compose network. The `lifeline` and `monitor` ranges are still defined in `registry/ports.py` but nothing allocates in either. RustDesk (21115–21119) is an opt-in stack of its own (`deploy/rustdesk-compose.yml`, `-p samcloud-rustdesk`).

---

## Technology Stack

| Layer | Technology | Notes |
|-------|-----------|-------|
| Gateway | Go, stdlib only | Baked into the sshd image. `go.mod` says 1.26.1; `Dockerfile.sshd` builds it with the 1.24 toolchain |
| Registry | Python 3.10+, FastAPI, uvicorn | SQLite WAL, Pydantic models |
| Console | Next.js 16, React 19, Tailwind CSS 4 | TypeScript, iron-session auth |
| Database | SQLite (WAL mode) | Single file, concurrent readers, single writer |
| Reverse Proxy | Caddy 2, with the Namecheap DNS module | ACME DNS-01 TLS, hot reload via admin API :2019, registry-driven config |
| Multiplexer | sslh | SSH/HTTPS on port 443 |
| Containers | Docker: native on Linux, Colima on macOS | One compose file |
| DNS | Namecheap API | Optional module: A records managed by the registry |
| Tests | pytest, the sandbox harness, feature tests, the docs check | `sandbox/harness.py`, `sandbox/test_*.py`, `sandbox/docs_coverage.py` |
| Build and deploy | Shell helpers in `.githooks/` | `pre-commit` (the gates); `post-merge` (version from tags, build, recreate what changed, tag). Run by hand, as git hooks, or from the deploy receiver |

### Key Design Decisions

- **SQLite over Postgres**: a single-writer control plane (one registry process); WAL handles concurrent readers. No external database.
- **Go for the gateway**: a compiled binary with no runtime dependencies. Fast startup, low memory. It runs as the ForceCommand on every SSH connection.
- **Python for the registry**: fast iteration and a rich ecosystem (FastAPI, Pydantic). Enough performance for a single-node control plane.
- **Containers for the whole control plane**: one compose file holds every part. What runs outside it is optional and host-specific: the credential brokers, the deploy receiver, and each seat's rail listener.
- **Token-based auth over session cookies**: tokens are prefix-typed, scoped and hashable. Enrolment, invite and pair tokens are single-use; device, service and agent tokens are long-lived; a login's token expires.
- **Bearer never in argv**: credentials are presented from 0600 header files, because `argv` is readable through `ps` on a shared box.
- **Event bus over webhooks**: in-process pub/sub with SSE delivery. No external message broker; the control plane stays self-contained.
- **Route automation**: activating a route starts the SSH forward, reloads Caddy and, with the DNS module on, creates the DNS record, in one API call.
- **Access is three separate boundaries**, and each is stated on its own:
  - *Scope* decides who can see and manage what, through the API, the dashboard and the gateway. Tokens and group roles decide every read and write, and the gateway admits a key only to devices its user's scope reaches.
  - *Network reachability* of a service's port is a separate matter. The registry routes a service; it does not firewall it. Anyone who can reach a service's port, or its route, can load it, whatever their scope. [Security](Security.md) states this as a known limit, and [Roadmap](Roadmap.md) carries the planned gate.
  - *A routed app's own login* belongs to the service.

---

## File Map

| Path | Purpose |
|------|---------|
| `registry/main.py` | Registry API |
| `registry/db.py` | Database layer (schema, transactions, queries) |
| `registry/schema.sql` | Full schema definition |
| `registry/enrollment.py` | Token lifecycle (generate, validate, verify) |
| `registry/events.py` | In-process event bus (SSE and polling) |
| `registry/dns.py` | Namecheap DNS record management (the `dns-namecheap` module) |
| `registry/forward.py` | SSH local forward automation inside the sshd container |
| `registry/ports.py` | Port allocator (ranges, ledger, allocation) |
| `registry/caddy.py` | Caddy config generation and reload |
| `registry/authorized_keys.py` | Sync managed keys to authorized_keys |
| `registry/bootstrap.sh`, `registry/bootstrap.ps1` | Device bootstrap scripts, served with their values filled in |
| `registry/helpers/` | Served rail and agent tools (`samcloud-rail.py`, `samcloud-listen.py`, `samcloud-adapter.py`, `samcloud-say`, `samcloud-new`, `samcloud-edit`, `samcloud-pr.py`, `samcloud-lease`, `samcloud-subscribe.py`, `samcloud-channel.py`, `samcloud-scope-check.py`, `wake-install.sh`, the Windows helpers) |
| `registry/skills/` | Served agent skill (`GET /api/v1/skills`) |
| `registry/test_*.py` | Registry API and allocator tests |
| `gateway/main.go`, `gateway/hostkey.go` | SSH gateway binary |
| `console/` | Next.js web dashboard |
| `clients/hermes/` | The Hermes rail client |
| `deploy/` | Compose file, Dockerfiles, entrypoints, `.env.example`, `rustdesk-compose.yml` |
| `deploy/deploy-hook/` | The deploy receiver, its installer and systemd unit template |
| `.githooks/{pre-commit,post-commit,post-merge,deploy-lib.sh}` | The gates and the build and deploy helpers |
| `{aws,gh,google,adb}-broker/` | Credential brokers (7782 / 7781 / 7783 / 7791) |
| `plists/install-broker.py` | Renders and loads the gh, aws or google broker's LaunchAgent on a macOS host, from `~/.config/samcloud/<name>-broker.env` |
| `scripts/ddns-update.sh` | Keeps the zone's A records on a dynamic IP |
| `sandbox/harness.py` | End-to-end test harness |
| `sandbox/docs_coverage.py` | Doc and skill drift gate |
| `sandbox/test_*.py` | Feature tests: enrolment, docs, files, tasks, views, wake, briefing, transport, config reference, deploy |

Next: [API Reference](API%20Reference.md)
