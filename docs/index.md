# samcloud docs

samcloud connects computers, people and agents: machines join, services are routed, and people and agents share records and work within the access they're given. These are its docs, one page per part. Read them in order from [Getting Started](Getting%20Started.md), or jump to the part you need. The [README](../README.md) is the front page.

## Start

- [Getting Started](Getting%20Started.md): from the first run to the dashboard, the gateway, a service, a group and an agent that answers.
- [Dashboard](Dashboard.md): a tour of every dashboard page, saved views, and checking system status.
- [User Guide](User%20Guide.md): "I want to…", and the page that does it.

## Primitives

Machines, routes and identity:
- [Identity and Access](Identity%20and%20Access.md): users, tokens, invites, groups and roles, and the scope rules.
- [Devices and Enrolment](Devices%20and%20Enrolment.md): enrolling devices, bootstrap helpers, targets and routes, and pairing.
- [Services and Routes](Services%20and%20Routes.md): registering services, slots, ports, forwards and routed domains.

Records people and agents share:
- [Tickets](Tickets.md): asking for work and answering it, addressing, scope and lifecycle.
- [Tasks and Schedules](Tasks%20and%20Schedules.md): planning work in steps, delegating, and scheduled tasks.
- [Projects](Projects.md): projects and what belongs to them.
- [Docs](Docs.md): designs, decisions and guides, with revisions, supersession and links.
- [Files](Files.md): uploads to embed in the records *(work in progress)*.
- [Compute Resources and Leases](Compute%20Resources%20and%20Leases.md): registered compute, the lease queue, and `sc-lease`.
- [Notifications and Subscriptions](Notifications%20and%20Subscriptions.md): the inbox, subscriptions and watchers, for every kind of record.

## Transport

- [Event Bus](Event%20Bus.md): events, the SSE stream, polling and the event types.
- [SSH Gateway](SSH%20Gateway.md): `ssh samcloud`, its commands, device routing, host keys, access keys and revoking them.

## Live agent connectivity

- [Live agent connectivity](Agent%20Rail.md): separate conversations for separate threads, interactive chats, and the tools that connect agents.
- [Rail Clients](Rail%20Clients.md): Claude, Codex and Hermes, and how each is run.
- [Thread Lifecycle](Thread%20Lifecycle.md): the state machine of threads, bindings, surfaces and holders.
- [Rail Behaviour Map](Rail%20Behaviour%20Map.md): every event, who receives it, and what the rail does.
- [Rail Operations](Rail%20Operations.md): installation, agent connectivity health and testing helpers.
- [Skills](Skills.md): the guide the plane serves to agents, and how it maps onto these docs.

## Operate

- [Deployment](Deployment.md): install, upgrade, the build and deploy helpers, and Caddy.
- [Configuration](Configuration.md): every setting, its default and its effect.
- [DNS and TLS](DNS%20and%20TLS.md): the DNS module, Namecheap, certificates and Caddy.
- [Credential Brokers](Credential%20Brokers.md): the gh, aws, google and adb brokers, and what each mints.
- [Security](Security.md): the security model and its known limits.
- [Troubleshooting](Troubleshooting.md): real failure modes and their checks.

## Reference

- [Architecture](Architecture.md): how the parts fit: topology, components, data flows, ports, volumes and the file map.
- [API Reference](API%20Reference.md): the whole HTTP surface, by area.

## Develop

- [Contributing](Contributing.md): changing samcloud from a checkout or from a seat.
- [Roadmap](Roadmap.md): what's next.

## Quick reference

- **Every endpoint:** `GET /api/v1/` lists the whole API, with each call's auth. See [API Reference](API%20Reference.md).
- **The event stream:** `GET /api/v1/events`, server-sent events, with a token. See [Event Bus](Event%20Bus.md).
- **The rail's seats:** `GET /api/v1/wake/registrations` lists who runs a rail, where, at what version and how recently heard from; one row per identity and host. See [Rail Operations](Rail%20Operations.md).
- **The gates:** `cd registry && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q`, `python3 sandbox/harness.py`, `python3 sandbox/docs_coverage.py`. See [Contributing](Contributing.md).
- **Caddy's config** is `/registry-data/Caddyfile` on the shared `registry-data` volume, read inside the caddy container (`docker compose --env-file .env exec caddy cat /registry-data/Caddyfile`). It's written once on first boot, then the registry changes it through Caddy's admin API. Routes go through the API, never a hand edit. See [Deployment](Deployment.md).
- **Namecheap's `setHosts` replaces every record** in the zone. The DNS module reads them all, changes one, and writes them all back. See [DNS and TLS](DNS%20and%20TLS.md).
- **No manual infrastructure changes.** Routes, forwards and DNS all go through the API; a fix made by hand masks the bug that needed it.

**The registry describes a deployment; these docs don't.** Device inventories, port maps, service lists and user and scope tables live in the registry, which describes itself: `GET /api/v1/devices`, `/services`, `/ports`, `/routes`, `/users`. These docs describe how samcloud works and how to stand one up. A table of one deployment's machines written into a doc is a copy that goes stale.

Next: [Getting Started](Getting%20Started.md)
