# Changelog

What each release changed.

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

