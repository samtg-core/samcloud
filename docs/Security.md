# Security

This page states what samcloud enforces, where it enforces it, and what it does not do. It is written to be precise rather than reassuring. To report a vulnerability, follow the root [SECURITY.md](../SECURITY.md): privately, through the repository's Security tab.

## Three separate boundaries

Three questions decide who gets what, and samcloud answers them in different places. Keep them apart.

| boundary | what decides it | what it covers |
|---|---|---|
| **Scope** | samcloud: tokens, groups and roles | Who can see and manage what through the **API**, the **dashboard** and the **gateway**: devices, services, routes, tickets, docs, tasks, files, and admission to a machine. |
| **Network reachability** | the network a port sits on, and what is published | Whether a packet reaches a service's port: Caddy's published ports, a device's LAN, the plane's own published ports. |
| **The routed app's own login** | the service | Whether someone who reaches the service may use it. |

Scope does not guard a service's address. Groups and scopes decide who can find, manage and reach a service *through samcloud*, the way a DNS provider gives a site its address and not its password. Once a service has a route, anyone who can reach that URL can load it: its subdomain with a domain, or its route port on a bare address. samcloud puts no login in front of a routed service. Share only what is fine for everyone who can reach it, or keep the app's own login in front of it.

Being out of someone's scope hides a device or service from them in samcloud. It does not make the service unreachable on the network.

## What is enforced, and where

### The API

| what | where |
|---|---|
| **A bearer on every route**, except the public ones [below](#what-answers-without-a-token). A missing, unknown or expired token answers 401. | `get_auth`, `registry/main.py` |
| **One scope predicate** for reads, lists, writes and delivery: membership is the store, and the object's group decides. | `registry/scope.py`, and the `_require_visible_*` and `_require_*_manager` helpers in `registry/main.py` |
| **Hidden, not refused.** An object outside your scope answers 404, so nothing is inferred from outside. A reader who is not a manager gets 403. | the same helpers |
| **Tokens stored as hashes.** Every bearer is stored as its SHA-256 hash; the plaintext is shown once. Passwords are salted SHA-256. | `registry/enrollment.py`, `_hash_password` |
| **Single-use tokens** for enrolment, invites, service slots and pair links, each expiring after an hour by default. | `registry/enrollment.py` |
| **Strict request bodies.** An unknown field answers 422 instead of being dropped. Identity fields such as a ticket's filer come from the bearer, never from the body. | `StrictModel`, `registry/main.py` |
| **Audit.** The registry writes its own mutations to the audit log, read with `GET /audit` (scoped). | `db.audit` |

The rules themselves are in [Identity and Access](Identity%20and%20Access.md#scope-rules).

### The dashboard

The dashboard has no authority of its own. It keeps the logged-in user's registry token in an encrypted session cookie (`AUTH_SECRET`) and forwards it on every call, so each person sees and changes only what their scope allows. A login's token expires after a week; logging out ends it. See [Configuration](Configuration.md) for the cookie settings.

### The gateway

| what | how |
|---|---|
| **No shell on the plane.** Every access key is forced into the gateway command. | `authorized_keys`, written by `registry/authorized_keys.py` |
| **No forwarding with an access key.** The sshd allows only remote forwards, and access keys carry no forwarding option. | `AllowTcpForwarding remote` in `deploy/sshd-entrypoint.sh` |
| **Device keys hold a tunnel and nothing else.** `command="/bin/false"`, and `permitlisten` limited to the device's own tunnel port; a device with no tunnel port gets no forwarding. | `registry/authorized_keys.py` |
| **Admission by scope.** The gateway asks the registry before every route and every listing, with the same predicate as the API. If the registry does not answer, it denies. | `gateway/main.go`, `/auth/can-access`, `/auth/accessible-devices` |
| **Host keys pinned in both directions.** The gateway checks every device against its pinned key; every device checks the gateway against its published keys. | `gateway/hostkey.go`, `registry/forward.py`, the bootstrap's tunnel runner |
| **Host-key events recorded.** Pins, conflicts and clears are written by the registry's host-key routes. | `registry/main.py` |

[SSH Gateway](SSH%20Gateway.md) has the detail.

### The containers

- **The sshd container sees two files of the registry's data**, read-only: `authorized_keys` and the gateway's key. It cannot read the database, the Caddyfile or uploaded files.
- **The gateway's private host keys live only in the sshd container** (`sshd-keys`). The registry mounts the public halves (`sshd-hostpub`), read-only, so it can verify the gateway without being able to impersonate it.
- **Tunnel and forward ports are not published** on the host. They terminate inside the sshd container; a routed service is reached through Caddy.

### On a seat

- **Tokens travel in 0600 header files**, never on a command line. See [Header-file auth](Identity%20and%20Access.md#header-file-auth).
- **Logins to other systems are minted per use** by a broker and expire on their own; nothing long-lived for GitHub, AWS or Google sits on a seat. See [Credential Brokers](Credential%20Brokers.md).

### Principles

1. **Verify explicitly.** Every connection is authenticated by key and checked with the registry, in both directions: the caller's key at the door, and the target's host key on the hop out.
2. **Least privilege.** A login lands in the gateway, not a shell; a device key holds a tunnel, not a session.
3. **Assume breach.** Keys are per user and device, so one lost device is revoked without the others.
4. **Record what happened.** The registry audits its own changes: keys, enrolment, pins, records.

## What answers without a token

| route | why |
|---|---|
| `GET /health`, `GET /`, `GET /api/v1/` | liveness, and the running release |
| `GET /api/v1/setup/status` | whether the plane is initialised, for the dashboard before login |
| `POST /api/v1/init` | first-run admin setup; 409 once any user exists |
| `POST /api/v1/auth/login`, `POST /api/v1/auth/logout` | logging in and out |
| `GET /api/v1/status` | answers an empty fleet without a token |
| `GET /api/v1/users/brief/{token}`, `POST /api/v1/users/activate` | an invite: the token is the credential |
| `GET /api/v1/bootstrap/{token}`, `…/ps1`, `POST /api/v1/devices/activate` | a device's bootstrap: the enrollment token is the credential |
| `GET /api/v1/enroll/services/brief/{token}`, `POST /api/v1/services/activate` | a service slot: the enrollment token is the credential |
| `GET /api/v1/pair/{token}` | a pair link: the single-use link is the credential |
| `POST /api/v1/services/register` | checks the device token in its own header |
| `GET /api/v1/gateway/host-key`, `GET /api/v1/gateway/pubkey` | public keys, offered to anyone who connects anyway |
| `GET /api/v1/skills…` | the served skill pages and their installer |

## Known limits

These are true of samcloud as it is.

**Routed services**

- **No samcloud login in front of a routed service.** Reaching the URL is enough to load it. Use the app's own login, or don't route it.
- **Network reachability is the network's.** samcloud does not isolate services from each other at the network level. A service on a device's LAN is reachable by whatever else reaches that LAN, routed or not.
- **Internal traffic is plain HTTP.** The registry, dashboard, Caddy and gateway talk over the compose network without TLS. The registry and dashboard are also published on their own host ports (`REGISTRY_PORT`, `CONSOLE_PORT`) over plain HTTP: reach them through Caddy's HTTPS where the network in between isn't trusted.

**Scope and the gateway**

- **Admission to a device is close to holding the bearers kept there.** A shell on a device runs as its SSH user, which may own other seats' 0600 token files. Granting `group:X` to someone is close to handing them the bearer of every seat whose token file that account can read on X's devices. Add a device to a group deliberately.
- **The gateway does not read group role.** A viewer of a group gets the same shell on its devices as an owner.
- **`status` at the gateway shows the plane's totals** (devices, services, ports, open tickets) to anyone with a key, whatever their scope.
- **The gateway tells an unknown device (127) from one you can't enter (126).**
- **Gateway sessions are not in the audit log.** The gateway posts its connection, route, command and denial events to `POST /api/v1/audit`, which the registry does not serve, so they are not recorded. sshd logs each login to its container's log (`docker compose logs sshd`); keep that if you need a record.
- **The admin's shell runs in the gateway's container.** That container holds the gateway's key, which every target trusts, and its `authorized_keys` carries the gateway's own admin-scoped registry token in each access key's forced command. Treat the admin role as full access to every device.

**Tokens and keys**

- **Rotation replaces one token type.** `regenerate-token` replaces the user's token of their current role type; a long-lived token of the other type, if they hold one, stays valid.
- **Access keys survive their user.** Deleting a user does not revoke their keys. Revoke them first ([SSH Gateway](SSH%20Gateway.md#revoking-access)).
- **Hashing is fast.** Tokens and passwords are hashed with SHA-256 (passwords salted), not a slow password hash. Tokens are 128 random bits; choose strong passwords.
- **Keys are files.** Access keys, device keys and tokens are files on disk; there is no hardware key storage.
- **No encryption at rest.** The registry database (SQLite, WAL) and uploaded files sit unencrypted on the plane's data volume.

**Trust on first use**

- **A device enrolled through `POST /enroll`** reports its host key during the bootstrap, so its first hop is verified too. A device enrolled another way is trusted on its first hop only, and every hop after is checked against what that one recorded.
- **The `samcloud` alias** that an access key's setup script writes trusts the gateway's key on first connect (`accept-new`), then checks it. The bootstrap's `samcloud-gw` alias and the tunnel check strictly from the start.

## Reporting

Report anything you find privately: see [SECURITY.md](../SECURITY.md). It says what a useful report contains, what is in scope, and what happens next. `GET /api/v1/` names the release you tested against.

## Related

- [Identity and Access](Identity%20and%20Access.md): tokens, groups, roles and the scope rules
- [SSH Gateway](SSH%20Gateway.md): admission, routing and host-key trust
- [Services and Routes](Services%20and%20Routes.md): what a route exposes
- [Credential Brokers](Credential%20Brokers.md): short-lived logins to other systems
- [Deployment](Deployment.md): running the plane and its containers

Next: [Troubleshooting](Troubleshooting.md)
