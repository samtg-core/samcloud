# Changelog

What each release changed.

## 2026-11-24 — A service acts as itself for compute and as its caller for everything else

- Services had become users on the plane. A service token couldn't lease, so each service that needed a GPU got a plane user; to write results where callers could read them, those users got sink groups and read on whole groups, and they posted as if they were people (doc 752, ticket 1157). Sam's call, 24 Nov: a service has no reach of its own into the plane's content, and what it does for a caller it does with the caller's access.
- **A service token leases its own device's compute, as itself.** It sees and leases the resources of the device its service runs on; the lease's holder and `service_id` are the service. It sees no other device, and naming another service is `403`.
- **Caller grants.** `POST /grants {service_id, group_scope, ttl_s}` lets one service upload into one group the caller edits, until it expires. The service sends `grant=` with its own token on `POST /files`; the file is the caller's (`owner`), and `uploaded_by` names the service. Only a hash is kept. A grant is refused for another bearer, after expiry or revocation, or once its caller no longer edits the group. `GET /grants` and `DELETE /grants/{id}` list and end them.
- `tools.md` (skill 3.21.0): "Find and call a service" says how a caller gets results and mints a grant; "Register a service" says how a service leases, uses a grant, and returns results through its own API. `queue.md` (3.13.2) points to both. Files, Identity and Access, and Compute Resources and Leases say the same. The API map lists `/grants`.
- `tools.md` (skill 3.21.1): a service that leases through compute-jobs sets `service` to its service id, or compute-jobs refuses its own lease (found by wafer-unity running scene-render with a grant, task 1450).

## 2026-11-24 — The skill covers enrolment and identities; the API index lists every route

- **No skill page named how a machine or an account joins the plane, how a service is published, or how an identity is added** (ticket #1048). Bringing Obelisk on, four seats improvised a device-scope grant, a hand-made `POST /devices` and a gateway key pasted in through sudo, and the grant would have given a shell on Sam's login. Of 186 routes, 98 were named in a skill page and 121 in `GET /api/v1/`; 48 were in neither, `/enroll` and `/bootstrap/{token}` among them.
- **`tools.md` 3.20.0:** *Bring a machine or an account onto the plane* (`POST /enroll`, the bootstrap command run as the target account, the gateway check, retoken, pending, and what `device:<id>` grants; enrolling takes the editor role). *Register a service*, from 3.19.0, gains what this pass had written as *Publish a service*: the id is `<device_id>/<name>`, `guide_url`, the check from outside before sharing the URL, and health, update and delete. It also corrects 3.19.0's "a route only through a tunnel": a `direct-ssh` device is routed to its `outbound_ip`, and only a device with neither a tunnel nor an address gets no working route (`register_service`'s Caddy target).
- **`auth.md` 3.3.0:** *Add an identity* (invite, activate, the briefing, who may invite with what, an identity with no scopes, ending a token). Its invite example names the group in `scopes`, as `group_roles` requires.
- **`SKILL.md` 3.14.0:** the table routes "bring a machine or an account onto the plane, register a service" to `tools.md`, and "add an identity" to `auth.md`.
- **`GET /api/v1/` lists every route a seat calls:** enrolment and bootstrap, device helpers, gateway admin, ports, skills, `helpers/{name}`, audit, views, group member and device edits, `regenerate-token`, doc watchers, `POST /services/register` and `POST /probe`. `POST /services` describes its answer, including the `service_token`. `users` had the key `briefing` twice, so `GET /users/brief/{token}` was never listed; it is `invite_briefing` now.
- **`sandbox/docs_coverage.py` fails on a route that is neither in `GET /api/v1/` nor in `INTERNAL_ROUTES`** (the rail's claims, presence, beacon and `notifications/seen`, each with its reason), and on an internal entry that names no served route.

## 2026-11-24 — The skill says a seat can register its own environment and use services

- Hermes seats had gateway reach that their scopes allowed but didn't use it: nothing in the skill's front page said that the environment a seat runs in can be registered with its own token (ticket 1143).
  - The front page now says so, and its table points to the steps.
  - `auth.md` gives the full `setup-script` call and what `username` and `device_name` are, and says the script needs an OpenSSH client and leaves an existing `Host samcloud` block alone.
- `tools.md` has a short "Find and call a service" section, just before "Register a service": which `GET /services` fields to read, and that `GET /services/{id}/guide` is written for the service's operator.
- `tools.md` and SSH Gateway now say that piped input to a one-off command needs no terminal: with `RequestTTY force` or `ssh -t` the command never sees end-of-input. Use `ssh -T`.
- Skills: agent 3.13.1, auth 3.2.6, tools 3.19.1.

## 2026-11-24 — The skill says how to register a service

- No served page said how to register a service. Today wafer-unity registered on a device with no tunnel, got no route, and needed the admin to find out why (task 1433, ticket 1146, the Papercuts register, the 5 Nov skills audit in ticket 1048). `tools.md` (skill 3.19.0) has a new "Register a service" section. It covers who can register (an editor of a group that holds the device), why a public route needs a tunnelled device, that `subdomain` wires the forward, Caddy and DNS in the same call, the `route` steps to check, the service token for health, and how to move a service. `queue.md` (3.13.1) points to it from "Build a service", and the API map's line for `POST /services` says the same.

## 2026-11-24 — The queue has its own skill page

- The skill listed the lease calls in `tools.md`, but no served page explained the queue itself, and it was one of the things Sam had to explain most often in the last fortnight (ticket 1146). The new `queue.md` (skill 3.13.0) covers it: one queue per resource; ask for your real need and wait with no deadline; a service queues for you; take a lease with `sc-lease` and release it on every path; what a service owes the queue. `SKILL.md` lists it.

## 2026-11-24 — An upload answers with its embed, /files is in the index, and /tasks pages

- A seat that uploaded a file had to build its markdown embed by hand, and seats posted host paths instead, which nobody else can open. That cost Sam about 13 round trips in a fortnight (ticket 1146). Every file record now carries `markdown`: `![name](/api/v1/files/{id}/raw)` for an image and `[name](…)` for anything else, the same form the console's Attach button writes. The tools skill page (3.18.9) says to paste it.
- `GET /api/v1/` didn't list `/files`. It does now, with upload, list, read, raw, re-scope, delete, link and unlink.
- `GET /tasks` stopped at the 500 lowest ids and ignored `offset`. It now pages with `limit` (up to 500) and `offset` over the tasks you can see, and answers with `total` and `next_offset`.

## 2026-11-24 — Lists and thread reads read the caller once

- The plane was slow for everyone on 24 Nov (ticket 1146). Ticket lists took up to 88 s for a seat, and even single reads were slow. A list, a search or a thread read that checked each row re-read the caller's username and group memberships for every row. In tests that was 1,546 queries for one page of tickets as a seat, 2,155 for one search, and 189 for one thread read. The rails read threads about 3.5 times a second, which kept the registry near six cores busy.
- A GET or HEAD request now reads the caller's name and groups once and keeps them for that request. A request that changes something never keeps them, and each request starts fresh, so a membership change is seen on the next request. The same ticket page takes 9 queries, the search 13, and the thread read 105, one per member row.

## 2026-11-24 — google-broker records what Google granted at consent

- A Workspace consent screen has a box per scope. When Drive was left unticked, Google granted only email, but the broker recorded the scopes the consent link asked for (ticket 1154). So `/mint` let a full-URL `drive.file` through to Google (a bare `invalid_scope`), or handed back a token without Drive that Drive then refused on the first upload.
- The callback now records the scopes in Google's token response. Its page says which were dropped and to open the link again; the mint log carries `requested_scopes` and `dropped_scopes`.
- `/mint` refuses a token Google returns narrower than asked for, and Google's `invalid_scope` on refresh, as `scopes not granted at consent`. That covers accounts enrolled before this change.
- New short names: `drive`, `drive.file`, `drive.readonly`. Tests in `google-broker/test_granted_scopes.py`.

## 2026-11-24 — Doc reads answer quickly again

- Since the docs tree release, a doc read, the docs list and doc search could take seconds or more for a caller who isn't admin, and they held workers under concurrent reads (ticket 1146). Working out the tree checked each doc's visibility on its own, and each check re-read the caller's group memberships: 2,124 queries for one read over 302 docs in the test. The caller's groups are now read once per request, under the same rule (admin, author or owner, or a member of the doc's group). The same read takes 25 queries, and a list page takes 15.

## 2026-11-24 — The docs are a tree you can walk

- Every doc made with `sc-new` landed at the top level, because the pen had no parent option, and `GET /docs` returned the newest 200 whatever you asked for (ticket 1146). 346 of 644 docs sit at the top level.
- `sc-new doc --parent N` files a doc under doc N, and `--parent root` says the top level is right. With neither, the doc still lands at the top level and `sc-new` lists the biggest sections (sc-new 1.2.0).
- `GET /docs` honours `parent` (or `parent_id`), `root=true`, `sort` (`updated`, `created`, `title`, `id`, `children`), `order`, `limit` (up to 500) and `offset`, and answers with `total` and `next_offset`. Each row carries `path` and `child_count`. With no parameters it returns what it always did.
- A doc read carries `path`, `path_text` and `siblings` (the previous and next doc under the same parent). A doc hit in `/search` carries `path` and `path_text`, and `/search?sort=updated` orders by date. An ancestor you can't read appears as a bare id.
- The docs skill page (3.4.0) teaches choosing a place, searching, and walking the tree.

## 2026-11-19 — A file attached on a ticket, task or doc is linked to it

- A file attached through the console's "Attach files" landed in the uploader's default group and was never linked to the object it was attached on, so the object's readers got "File not found" for the embed (ticket 1103; files 2390 and 2784). The button posted the link to `/api/v1/files/{id}/links` from the browser. The generic `/api/v1` proxy forwards only a caller's `Authorization` header, and a browser sends a session cookie, so the registry refused every link and the button's best-effort catch hid it.
- The link now goes through `/api/files/{id}/links`, which forwards the session's own registry token, as `/api/files/{id}/raw` does. The registry's existing check still applies: the linker must be able to read both the file and the target. If linking fails, the button keeps the embed and says the file isn't linked.

## 2026-11-19 — The bootstrap runs in Termux

- A Quest 3 running Termux couldn't enrol as an access target (ticket 1110). Termux reports Linux, so the bootstrap took the Linux path and stopped at `mktemp /tmp/...`: Termux has no `/tmp`. Past that it would have needed sudo, apt-get, `service` and systemd, and Termux has none of them.
- The bootstrap now detects Termux and runs as its user, with no root. It keeps its temp copy in `$TMPDIR`, installs `openssh` and `python` with `pkg`, starts `sshd` and reads its port (8022), and starts the tunnel from a Termux:Boot script with a wake lock. Other platforms are unchanged.

## 2026-11-19 — An access target's QR opens without a sign-in

- `/b/<token>` was missing from the console's public paths, so the QR from the last release opened the login page on the device instead of the command (ticket 1110). It's public now, like `/p/<code>`.

## 2026-11-19 — An access target's bootstrap has a QR

- Enrolling an access target (or both) shows a QR beside the bootstrap commands. Scanned on the device, it opens `/b/<token>`, which shows `curl -s …/api/v1/bootstrap/<token> | bash` to copy, with no sign-in, as `/p/<code>` does for a route's pair link. For a headset or phone where typing the URL is the hard part (ticket 1110). The token is the same one-hour, single-use enrolment token the page already showed.

## 2026-11-19 — Continuation uses the ordinary lease ledger

- Check competing ownership with the same `occupying` predicate as normal admission. The initial continuation query incorrectly counted expired queued waits and old non-HOLD rows as charged capacity, so a retained pool could refuse despite having no competing hold.
- Keep every historical row unchanged. Active leases and real expired/unsettled TTL holds still block the transfer. HTTP/SQLite regressions cover both noncharging history shapes and a competing real HOLD.

## 2026-11-19 — Atomically continue a cleaned-up expired HOLD pool

- An owner/admin with current access to every host can explicitly attest cleanup of every exact expired/unsettled member of a never-serving setup and transfer the same already-charged pool to a fresh bounded successor. Settlement, coordinator fencing and replacement leases commit together; ordinary waiters see no free-capacity window or intervening promotion.
- Owner, resources, sizes, TTLs and effective exposure bounds are unchanged. The original intent and expired leases remain history, linked to the successor. No worker is started, expired lease resurrected or inference retried; unbounded active-session lifetime remains a separate contract.
- Missing, changed, foreign, active, partial or already-settled claims, another charged allocation, revoked access, prior serving and a release request refuse atomically. Exact replays return the successor's current state; conflicting intent never allocates again.
- HTTP/SQLite controls cover ownership, same-pool transfer, independent fresh authority, current-state replay, repeated expiry, queue order, concurrency, partial-write/commit rollback and post-commit publication.

## 2026-11-14 — Hosts can observe their own group-member authority (#18)

- A host-local service can read its exact member's current group generation and lease authority without visibility of the other hosts or a copied coordinator credential. The projection hides other members, intent and evidence and is not cacheable.
- The existing lease event stream wakes each affected host after a committed group change, without exposing private group history or creating a second polling service. Notices are reconciliation hints; stable reads emit none.
- Partial admission, a fence/release, any member's expiry, mismatched authority or recorded cleanup cannot authorize work. Reads use normal expiry reconciliation but never acquire, renew or release a group member; full-group reads and owner/admin writes keep their existing boundaries.
- All required members are checked at one observation instant, including expiry after reconciliation. The projection's authority ends at the earliest required member expiry, so a longer local lease cannot extend group authority.
- HTTP/SQLite controls cover scope isolation, generation loss with HOLD retained, partial acquisition, missing/mismatched links, revoked access and unchanged cleanup permissions. This adds no host supervisor or provider deployment by itself.

## 2026-11-14 — Durable groups across ordinary host lease queues (#16)

- A stable `request_key` now binds one multi-host operation to its member leases and durable event history. Group intent and member admission commit together; response retries cannot create a second allocation.
- Groups serialize acquisition FIFO, first waiting for their declared first host, then requesting the others through normal host queues. Partial ownership is visible; serving requires every member active. There is no group deadline or priority boost.
- HOLD expiry remains uncertain until cleanup is attested and the exact lease's settlement receipt is saved. Generation fencing drains rather than retries. Scoped reads require every host; owner cleanup remains possible after access revocation.
- This is the registry record/API, not a provider rollout or process-death detector. Existing member renewal ceilings still apply. HTTP/SQLite tests cover concurrent coordinators, normal queue order, lost responses, rollback, HOLD settlement, stale generations, access and schema upgrades.

## 2026-11-14 — google-broker holds more than one Google Cloud project (#1246)

- An OAuth client has a name. `default` is the existing `client_secret.json`, and others live in `clients/<name>.json`. `GET /oauth/start?client=<name>` asks for consent through that client, and the account it enrols records it and refreshes through it on every mint. Accounts enrolled before this have no client recorded and keep using `default`. A mint whose client is missing answers `503` and names it.
- `POST /oauth/client?name=<name>` stores a named client. Its body can be `{"client_id", "secret_from"}`: the secret a person pasted into a one-time secret link moves into the client record and leaves the secret store, so it passes through nobody's hands. `GET /health` lists the clients and each account's client.
- So keys for Sam's professional Google account come from his own project, apart from the family one. Credential Brokers describes it, and `google-broker/test_named_clients.py` tests it.

## 2026-11-14 — The thread pill's drop-down opens again (#1083)

- The thread pill in the top bar opens its list again, so you can start a new thread or resume another one. The 13 November sticky header (#1082) gave the bar a blurred backdrop, and a backdrop filter makes the bar the containing block of the pill's drop-down, so the bar's `overflow-hidden` cut the list off. The bar is now opaque and still sticky.

## 2026-11-13 — Search across tickets, tasks and docs (#1082)

- `GET /search?q=` finds tickets, tasks and docs you can see by every word of the query in their title, body or comments, or by id (`#1082`). One filter vocabulary for all three: `kind`, `status` (with `active` for work still moving), `assignee` (`:me`), `group_scope`, `tag`, `updated_hours`. Ranked id, title, body, comment; paged; each hit has a snippet and a ready `ref` link. Until now only `GET /docs?q=` searched, and only titles.
- The console has a Search page, opened from a magnifier at the start of the top bar, from **≡**, or with ⌘K / `/` anywhere. Matches are highlighted; a comment match says so; the search is kept in the address.
- Dashboard and the API Reference describe both; the tools skill (3.18.4) tells agents to search, and to write a hit's `ref` link when pointing someone at a record.

## 2026-11-13 — The inbox is a feed with read and star (#1082)

- `GET /inbox/feed`: one card per ticket, task or doc, newest activity first, each with what happened last, its comment count and your read and star state. `?filter=mine` (the default: addressed to you by a notification row, assigned to you, or yours), `unread`, `starred` or `all`; `?kind`, `?window_hours`, `?group_scope`, and paging by card. It reads delivery as it is: no one's notifications change, and nothing reaches the rail.
- `POST /inbox/marks` marks cards read or unread and starred or not; reading one also marks your notification rows for it read. `POST /inbox/read-all` marks the whole feed read and keeps stars. New tables `inbox_marks` and `inbox_state`.
- The console's inbox leads with the feed: *For me · Unread · Starred · Everything*, *tickets · tasks · docs*, a read/unread toggle and a star per card. A read card stays, dimmed. One busy thread is one card, so it no longer uses up a section's 30 rows. **Mark all handled** is **Mark all read**, and the notification views say *read*.
- Dashboard, Notifications and Subscriptions and the API Reference describe the feed.

## 2026-11-13 — Rows wrap instead of being cut off on a phone (#1082)

- The previous release stopped the page panning sideways, which cut off any row that was wider than the screen instead of letting you pan to it. Those rows now wrap: a ticket card's priority, type, filer and assignee line; the New ticket form's selects (two per row on a phone, on the Tickets page and in the ≡ pop-up); the upload group picker on Files; a service's header on Admin › Services.
- An inbox card's expanded updates show up to three lines each under their header, and a tap shows the whole update. They had been cut to one line.
- The tables on Admin, Admin › Devices and a group's admin page scroll sideways inside their own box.

## 2026-11-13 — The console's paper cuts (#1082)

- The Tickets list loads the next 100 as you scroll and says how many of the total are loaded. It used to stop at the newest 100: the registry pages the list and the page asked only for the first. A search, filter or sort other than newest first loads every ticket, so it no longer searches one page. The console proxy passes `X-Total-Count` through.
- The header stays at the top as you scroll.
- Nothing widens the page sideways on a phone: `html` and `body` clip horizontal overflow, markdown tables scroll in their own box, long words and URLs wrap, a ticket's assignee and scope pickers wrap onto their own line, and an inbox card's expanded rows truncate as intended.
- Plane references in markdown (`ticket 1082`, `#1082`, `doc 470`, `task 869`) are links that open in place and show the record's title on hover.
- Dashboard describes the paging, the sticky header and the reference links.

## 2026-11-10 — A pair link has a QR and a short code

- `POST /access-keys/pair` also answers `code` (ten characters, like `K7QM2-XHD9P`), `code_url` and `code_command`. `GET /pair/{token}` accepts the code in any case, with or without its dash. Running either form spends both; minting again replaces both (#1068).
- The console's `/pair` page shows `code_url` as a QR. The new device scans it, or types the address, and `/p/<code>` shows the pair command to copy. The page needs no session, and opening it spends nothing.
- The served auth skill (3.2.4), Devices and Enrolment, Dashboard and API Reference describe the code.
## 2026-11-09 — Outside pull requests are welcome

- CONTRIBUTING.md, the README and docs/Contributing.md invite pull requests from outside contributors. Each is reviewed and merged by a person, never automatically; for anything larger than a small fix, an issue comes first.

## 2026-11-07 — The docs state the access rules as they are

- The enrolment, device-manager, invite, route-activation and event-visibility rules read the same in the docs, the served skills (auth 3.2.3) and the `GET /api/v1/` index.
- The examples use a neutral "App release" project (tasks skill 3.2.4; the tools skill, 3.18.3), and the dashboard screenshots are retaken with neutral demo data.
- The rail's known limits are in Rail Operations; the Roadmap lists only what comes next.
- Live agent connectivity leads with threads and interactive chats, with separate Claude, Codex and Hermes sections. Connection-health guidance and legacy execution notes are shorter; the roadmap includes choosing a plane name at setup.

## 2026-11-07 — Enforce invitation, enrollment and identity boundaries

- Only admins can invite admins. Invite scope and group-role checks run before user or token writes, so rejected grants leave no partial account.
- The dashboard submits admin invites with wildcard scope and an empty group-role map; user and agent invites retain the selected group roles.
- Non-admin enrollment requires an editor role and joins only editable groups, including when the group list is omitted. Group viewers cannot reconfigure devices or their resources.
- Device IDs cannot occupy a user/service namespace or collide with usernames, in either creation order. Existing ambiguous device credentials are refused; password-backed human sessions and legacy agent identities remain supported.
- Route activation checks the service manager before changing forwarding, Caddy or DNS; routes without a service require admin.
- Account and compute events are filtered by the account or resource's device. Resource-deletion events retain the device scope after the row is removed.

## 2026-11-07 — The docs are one page per part

The docs had grown by accretion: one long User Guide, the rail spread over five design pages, and history mixed into the current description. They are now a contents page (`docs/index.md`) over one page per part, in the order a newcomer meets them, each ending with a link to the next. Each page shows the dashboard view first, then the API and helpers.

- **Start:** Getting Started, a Dashboard tour with screenshots from a demo instance, and the User Guide as a task index.
- **Primitives, at equal depth:** Identity and Access, Devices and Enrolment, Services and Routes, Tickets, Tasks and Schedules, Projects, Docs, Files, Compute Resources and Leases, Notifications and Subscriptions.
- **Transport and the rail:** Event Bus, SSH Gateway, Agent Rail, Rail Clients (Claude, Codex, Hermes), Thread Lifecycle, Rail Behaviour Map, Rail Operations, Skills.
- **Operate and reference:** Configuration, Deployment, DNS and TLS, Credential Brokers, Security, Troubleshooting, Architecture, API Reference (every route), Contributing, Roadmap.
- The pages were checked against the code. Where the old docs disagreed with it, they now say what the code does: commenting does not subscribe you, and the ticket examples no longer send `source`.
- **Skills (agent 3.12.2, tasks 3.2.3, collab 3.3.3, tools 3.18.1, auth 3.2.2, docs 3.3.2, tickets 3.2.3):**
  - delegating takes no `device_id`;
  - the admin's unnamed writes land in `samcloud-contributors`;
  - bringing a device into a group needs the editor role there;
  - `samadb` ships with the adb broker;
  - superseding is a new doc with `supersedes`;
  - editors of a ticket's group can change it.
- The replaced design pages and `registry/API.md` moved to `docs/archive/`.

## 2026-11-07 — A holder commits on a PR branch

The pre-commit guard refused every commit from a rail holder, so a held seat could not open a pull request at all (ticket #1010). It now allows a holder to commit on a branch in a linked worktree, which is how a PR is made. It still refuses in the primary checkout (the deployment checkout is one), on `main` or `staging`, and on a detached HEAD.

## 2026-11-07 — `workcloud` is `tunnel`; `SLICE_*` settings are `PLANE_*`

`workcloud` was the early label for a device reached by reverse tunnel, and one path still keyed behaviour on it: a service registered through `POST /services` got a host forward port only if its device was labelled `workcloud`, while the slot path keyed on the tunnel itself (ticket #1010).

- Both paths now allocate the forward port when the device's `outbound_method` is `reverse-tunnel`.
- The label is `tunnel`. Existing `workcloud` rows become `tunnel` at startup, and `workcloud` is still accepted on enroll, register and `?context=`, stored as `tunnel`.
- The old label alone, with no `outbound_method`, still means a reverse tunnel: a register that names no method gets `reverse-tunnel`, and so do existing `workcloud` rows that named none. An explicit `direct-ssh` or `http` is kept (Codex's re-review).
- The dashboard badge follows the new label.
- `SLICE_HOST`, `SLICE_SSH_PORT` and `SLICE_SSH_USER` are now `PLANE_HOST`, `PLANE_SSH_PORT` and `PLANE_SSH_USER`, named for the plane rather than one machine. The old names are still read when the new ones are unset, so existing env files keep working; compose passes both.

## 2026-10-31 — The served helpers take their plane from the plane (rail 3.18.0, adapter 1.5.0, subscribe 2.13.0)

The last helpers that named one instance (C6 on doc 490, ticket #1010; the helpers are core):

- **The adapter** defaulted to that instance's API when `wake.env` set none. It now defaults to the plane that served it (`__API__`, rendered).
- **subscribe** fell back to that API when run from an unrendered copy. It now exits and says to set `SAMCLOUD_API`.
- **The rail** named its plane `stg` or `prod` by matching that instance's staging hostname, and exported the guess to every holder as `SAMCLOUD_PLANE`. It now looks its API up in the planes the serving plane names (`SAMCLOUD_PLANES`, rendered into the served copy as JSON), falls back to the serving plane's `SAMCLOUD_ENV`, and takes `PLANE` from `wake.env` over either. On a plane configured as before, the rendered rail gives the names it gave.
- `wsl-keepalive.ps1`'s comments, and the pens' `--plane` help, no longer name an employer or one instance's plane names.
- **Releasing:** these version bumps are what listeners act on. Once this is deployed, each seat's listener installs the new copies at its next check, so the merge waits for review and an idle boundary.

## 2026-10-31 — A login doesn't end the others

Each password login replaced the user's one token: `generate_device_token` keeps a fixed id per owner and deletes it first, against its own docstring. So a dashboard login revoked the init token an admin keeps in a header file, and a second browser's login ended the first's session (C7 on doc 490, found running the first-run walkthrough, ticket #1010).

- `POST /auth/login` makes a session token with its own id, valid for a week (the console cookie's life), and says so in `expires_in`. Expired session tokens are cleared at the next login.
- `POST /auth/logout` ends the login whose token it carries; the console's Log out calls it. It refuses a long-lived token, which `regenerate-token` replaces.
- Tokens from earlier logins keep working: they have no expiry, and nothing revokes them now except `regenerate-token`.
- Rotating a user's token on purpose (`regenerate-token`, or `rotate_token` on `PATCH /users/{id}`) ends every login of theirs too. A login keeps the scopes it was made with, so a demoted admin's open login would have stayed admin (Codex's review of the candidate).

## 2026-10-31 — Getting started, tested; the User Guide describes any plane

Following the guide after the README didn't complete the journey (finding C5, ticket #1010): its commands used port 7777 (the plane publishes 7778), it listed dashboard pages that moved to the archive, its service example returned 422 for a missing `description`, and its agent invite gave no group role, so the agent couldn't file a ticket (422).

- **`docs/Getting Started.md`**, new: from the README's first run to the dashboard, `ssh samcloud` through the gateway, a service reached by its URL, a group with an editor agent, a question and its answer, and an outside agent refused. Each command ran, in order, on a fresh no-domain plane in a VM, and the answers shown are what it returned.
- **`docs/User Guide.md`** uses `$PLANE` and the header file in every command. The service example has `description`, the invite gives a group role, and what needs a token is stated. Sections about one instance (its staging host, sim device names, deploy host, launchd sslh, `/var/lib/caddy`) give way to how any plane does it: compose, the Caddyfile on the data volume, two planes on one machine, upgrading.

## 2026-10-31 — A plane with no domain works end to end

A first run on a LAN address had an API but no coherent rest: the env example's `PUBLIC_URL` is the registry's port, which answers JSON; the console's login cookie was Secure on every request, so over plain HTTP it never stuck; and a service under the address got a name like `hello.192.0.2.10`, which resolves nowhere (finding C2, ticket #1010).

- **A service under an IP address gets its own port.** With no `SAMCLOUD_DOMAIN` and an IP in `PUBLIC_URL`, each route takes a port from a new route range (`ROUTE_RANGE_START`/`END`, 18101–18110), and Caddy serves it plain HTTP: `http://192.0.2.10:18101`. Compose publishes the range on Caddy. Removing the route frees the port. A plane with a domain is unchanged.
- **The login cookie is Secure when the request came over HTTPS**, which behind the bundled Caddy is every request. Reached directly on `CONSOLE_PORT` over HTTP, it isn't. `SESSION_COOKIE_SECURE=true` forces it on.
- **The gateway's SSH default agrees with itself.** Compose published sshd on `SSH_PORT` (default 2200) and told the registry 2222, so guides named a port nothing listened on. Both default to 2200; prod and staging set `SSH_PORT` and are unchanged.
- **The bundled Caddy's default site answers.** With `CONSOLE_DOMAIN=:443`, the default, Caddy had no name to get a certificate for and failed every TLS handshake (curl exit 35, by address and by name). A site with no host now gets `tls internal` with `on_demand`, and `default_sni` covers clients that send no name. A plane with a domain is unchanged.
- README's first run says where each part is on a LAN plane: API, dashboard, gateway SSH, services.

## 2026-10-31 — The export gates check the right tree

- **The export check scans the commit being made** (ticket #1010; Codex's review, doc 514, finding 4). `sandbox/test_export_public.py` scanned `HEAD`, so in pre-commit it checked the previous commit. It now scans the index (`git write-tree`), and a check stages a fake token in a throwaway repo to show it is caught there and missed at `HEAD`. `scripts/export-public.py --staged` does the same from the command line.
- **The public tree's own hook runs** (finding 3). The exporter check moves from gate 4 to a gate 5 that runs only where `scripts/export-public.py` exists, since the public tree carries neither the exporter nor its manifest and baseline.
- `CONTRIBUTING.md` says how to run every gate: `npm ci` in `console/`, Go, and `bash .githooks/pre-commit`. A fresh tree had no console packages, and the hook's type check failed before any test ran.

## 2026-10-30 — The export's instance rules live on the host

- **`public.denylist` leaves the repo** (ticket #1010). It held one instance's own details: its domains, public address, email domains, device names and a family name. `scripts/export-public.py` now reads those rules from `SAMCLOUD_EXPORT_DENYLIST`, else `~/.config/samcloud/export.denylist`, in the same format.
- Without a denylist, a scan uses the built-in rules and says so. `--out`, which writes a tree for release, refuses unless `--no-denylist` is given.
- `sandbox/test_export_public.py` checks that the repo carries no denylist, that the env setting names the rules (with an example rule, matched case-insensitively), and that `--out` refuses without one. It no longer names a real domain.
- Two sandbox tests still carried a family name in variable names (`…_id`, `…_keys`); they now use the fixtures' example name. The rule had used `\b`, which treats `_` as part of a word, so `name_id` slipped past. The host's rules now bound names with `(?<![a-z0-9])`…`(?![a-z0-9])`.

## 2026-10-30 — Comments name no one's machines

- **Code comments describe what happened without naming one instance's devices, seats or employer** (ticket #1010), in `registry/main.py`, `registry/bootstrap.sh`, `registry/bootstrap.ps1`, `registry/dns.py`, `aws-broker/main.py` and the adb-broker README. What each comment records is unchanged. The served helpers' comments change with the next rail release.

## 2026-10-30 — Brokers, scripts and the registry take their instance from config

- **The brokers serve no instance by default** (ticket #1010). gh-broker 0.6.0, aws-broker 0.2.0, google-broker 0.2.0 and adb-broker 0.2.0 exit at start without `SAMCLOUD_API`, and the gh and google brokers without their `*_BROKER_PUBLIC_URL`. They used to fall back to one plane. The GitHub App manifest's homepage is the broker's public URL.
- **`plists/install-broker.py gh|aws|google`** renders a broker's LaunchAgent from `~/.config/samcloud/<name>-broker.env`, with every key there in the agent's environment. It writes and reloads the agent when it changed, waiting for launchd to drop the old job first; `--dry` prints it. `sandbox/test_install_broker.py` gates it. The three hand-written LaunchAgents move to `archive/plists/`.
- **`OPERATOR_TZ` defaults to `UTC`,** and compose now passes it. It used to default to one instance's time zone, and compose did not pass it, so no env file could change it.
- **The enroll answer names the tunnel endpoint.** `POST /api/v1/enroll` for a reverse-tunnel target returns `tunnel: {host, port, user}`, as bootstrap fills it in. The console's manual tunnel command uses it instead of a fixed host and port.
- **Scripts ask instead of assuming:**
  - `scripts/ddns-update.sh` updates the zone `SAMCLOUD_DOMAIN` names, from its environment or `namecheap.env`, and stops without it.
  - `scripts/new-seat.sh` requires `--device-ip`, and takes `--api` for the commands it prints.
  - `clients/hermes/probe_cloud.py` requires `--api`.
  - The console's watchdog reader falls back to the process's home directory, not a fixed one.
  - `aws-setup.sh` and `bootstrap-ca.sh` name the leaf, role and trust anchor after the host (`HOST`, else the short hostname), and the AWS profile defaults to `default`.
- `scripts/bootstrap.sh`, the v1 manual-enrollment script that wrote `devices/*.yaml`, moves to `archive/scripts/bootstrap-v1.sh`. `registry/bootstrap.sh` is the enrollment script.

## 2026-10-30 — The deploy hook ships, with no instance in it

- **The deploy receiver names no instance** (ticket #1010), and ships in the public tree. Receiver 2.1.0:
  - `DEPLOY_HOOK_REPO` is required. The receiver and `install.sh` refuse to start without it, instead of falling back to one repository.
  - `DEPLOY_HOOK_PLANE` is optional. Unset, a release comment leaves out the `GET /api/v1/` line rather than naming a fixed plane.
  - New `DEPLOY_HOOK_PUBLIC_URL`, optional: where the receiver is reachable. A failure comment points at its `/health`, else at "the receiver's /health".
- **The systemd unit is a template,** `samcloud-deploy-hook.service.in`. `install.sh` renders it for the user who runs it: their home, and `DEPLOY_HOOK_REPO_DIR` from `deploy-hook.env` (else `~/samcloud`). It stops when `deploy-hook.env` or `DEPLOY_HOOK_REPO` is missing.
- **`registry/seed_production.py` moves to `archive/registry/`.** It seeded one instance's devices in the v1 schema, and its only caller, the `seeded_db` test fixture, skips before calling it. The fixture still skips; its 9 tests wait to be migrated to API-built state.

## 2026-10-30 — Instance trees move to the archive

- **The repo holds no instance's devices, notes or host jobs** (ticket #1010). These move to `archive/`, where they are kept and not maintained:
  - `devices/`, `incidents/`, `maintenance/`, `data/`, `srv/`, `config/` and `systemd/`;
  - the remote-workstation bootstrap scripts the console served from `console/public/` (no request reached them in 14 days of prod logs);
  - the LaunchAgents other than the brokers' (`archive/plists/`);
  - the device and host scripts: the remote-workstation, NAS, GPU and colima scripts, the `samcloud` CLI and its inventory, the watchdog and tunnel check, and the exo placement guard and `restart-when-idle.sh`, with their two tests (`archive/sandbox/`).
- **`bots/blinds` is no longer a submodule.** It pointed at a local path on one machine. `archive/bots/blinds.md` records the pinned commit.
- **The registry no longer copies the live Caddyfile into the repo.** `caddy.sync_repo_copy()` wrote to `config/Caddyfile` beside the source; in the container that path is `/config`, which does not exist, so it never ran. The docs drop the "reference copy".
- `scripts/seat-audit.sh` labels each plane by its env file name instead of matching one instance's hostname.

## 2026-10-30 — Every setting is in one reference

- **`docs/Configuration.md` lists every setting the control plane reads** (ticket #1010): the registry, gateway, console, compose file, deploy hook, brokers and device-side scripts, 113 in all. Each row gives its default and what it does. Settings the code reads but compose doesn't pass are marked.
- **`sandbox/test_config_reference.py` keeps the list complete**, and runs in the pre-commit gates. It derives the env vars the code reads, and fails on one the reference lacks, on a reference row nothing reads, and on a `deploy/.env.example` key the reference lacks.
- **`AUTH_SECRET` is required.** Compose used to fall back to a fixed, published secret. Now it refuses to start without one. Generate one with `openssl rand -hex 32`.

## 2026-10-30 — Test fixtures use example names

- **Tests, the sandbox and the staging stack name no real instance** (ticket #1010). Device, seat and resource names in fixtures are example names (`gpu-1`, `mediahost`, `homehub`, `studio`, `claude-studio`, `pi-1`), domains are `example.org`/`example.com`, SSH users are `you`, and LAN addresses are RFC 5737 documentation addresses. What each test asserts is unchanged.
- The `staging/sim-network/` devices are `sim-media`, `sim-homehub`, `sim-gpu-1` and `sim-gpu-2`, in the compose file, `setup.sh` and the User Guide's sim table.
- The staging registry sets `SAMCLOUD_DOMAIN=example.org`, so `run_tests.py`'s external-URL check has a domain to expect. The sandbox enrollment test now expects the harness's own domain, and passes again (70/70).

## 2026-10-30 — Skill pages carry the plane that serves them

- **The served skill pages name the instance that serves them** (ticket #1010). `registry/skills/samcloud-agent/` holds placeholders: `__API__`, `__GH_BROKER__`, `__GOOGLE_BROKER__`, `__AWS_BROKER__` and `__REPO__`. `GET /api/v1/skills/<name>/<file>` fills them from `PUBLIC_URL`, the broker URLs (else `ghb.`, `gglb.` and `awsb.` under the service domain) and the new `SAMCLOUD_REPO` (else `OWNER/samcloud`). Compose passes `GOOGLE_BROKER_URL`, `AWS_BROKER_URL` and `SAMCLOUD_REPO`.
- The pages' example seats are `claude-builder`, `builder-producer` and `box-1`. `contributing.md` drops the note about the repo's old name, and names the accepted mergers by their setting (`DEPLOY_HOOK_MERGERS`). Stamps: auth 3.2.1, collab 3.3.2, contributing 2.4.1, tasks 3.2.2, tickets 3.2.2.
- `registry/skills/install.sh`, run from a checkout, asks for `SAMCLOUD_API` instead of falling back to a fixed plane.
- **The served installer works with no `SAMCLOUD_API` set,** and installs every page of a skill, not only `SKILL.md`. The registry fills in its URL and each skill's file list when it serves the script. The script's check for an unrendered copy is a pattern (`__*__`); written as the placeholder itself, the registry's replace rewrote it too, and the served copy always exited 2 (Codex's review, doc 514, finding 1). `registry/test_skill_render.py` runs the exact served bytes with no API set and an empty destination.
- Upgrading: set `SAMCLOUD_REPO` (and `SAMCLOUD_DOMAIN`, if services publish under a parent domain), so the pages name your repo and brokers. Test: `registry/test_skill_render.py`.

## 2026-10-30 — The repo's front is its own

- **`README.md`, `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `SECURITY.md`, `LICENSE` and `deploy/.env.example` are the repo's own files** (ticket #1010). They used to be public-only copies under `public/`, which the export put in place of private ones. The overlay is gone: the export ships each file as it is in the repo, and the gate checks the bytes.
  - `CLAUDE.md` keeps what is about the code: the skills to load, where the truth is, the conversation model, how a change ships, and the working rules. It gains one more rule: nothing about one instance goes in the repo.
  - The previous `README.md`, `AGENTS.md` (GitNexus instructions) and `CHANGELOG.md` move to `archive/`. The history up to here is `archive/CHANGELOG.md`.
- **The project skills under `.claude/skills/` ship.** `samcloud-administering` reads its plane from `~/.config/samcloud/wake.env` instead of a fixed URL (2.0.1). `samcloud-contributing` names no host (2.3.1).
- `scripts/export-public.py` drops overlay support. A manifest is `+` and `-` lines, and a `>` line is refused.

