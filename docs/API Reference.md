# API Reference

The registry's whole HTTP surface, by area. The registry is one FastAPI app (`registry/main.py`); every route below is in it. Each primitive's page explains the model and shows the dashboard view; this page is the reference for the calls.

The registry also describes itself: `GET /api/v1/` is a curated index that names the running release. It is **a map, not a route table**: some working routes (such as `/auth/can-access`, `/files/{id}/raw` and `/skills/{name}/{file}`) are absent from it, so absence from the index is not evidence that a route doesn't exist.

## Conventions

**Base URL.** `PUBLIC_URL` followed by `/api/v1`, e.g. `https://cloud.example.org/api/v1`. The bundled Caddy proxies `/api/v1/*` to the registry; `/`, `/health` and FastAPI's interactive docs (`/docs`, `/redoc`) are reached on the registry's own port (`REGISTRY_PORT`), since Caddy sends everything else to the console. The examples use:

```bash
PLANE=https://cloud.example.org/api/v1
H=(-H @"$HOME/.samcloud/token.hdr")     # Authorization: Bearer <token>, from a 0600 header file
```

**Auth.** Almost every route takes a bearer, and a missing or unknown one answers `401`. A bearer is presented from a 0600 header file, never on a command line ([Identity and Access](Identity%20and%20Access.md)). The **Auth** column says who may call:

| Auth | means |
|---|---|
| none | no bearer needed |
| any | any valid token |
| scoped | any valid token; the answer, or the object, is limited to the caller's scope |
| manager | the admin, a token with `device:<id>`, or an editor or owner of a group that holds the device |
| viewer / editor / owner | that role or higher in the object's group (the admin always passes) |
| self | acts on the caller's own rows |
| admin | a token with scope `*` |
| token | the single-use token named in the path or body |

**Out of scope is not found.** An object outside your scope answers `404`, not `403`: it does not exist from where you stand. `403` means you can see it but not do that, and usually names the role needed. List routes are scope-narrowed and are not a census.

**Bodies are strict.** A POST or PATCH body with an unknown field answers `422`. Fields the registry derives from the bearer (`source` on a ticket, `author` on a comment) are refused if sent.

**Threads.** Creates and comments accept `X-Session-Id` (the writer's conversation: replies come back to it, and it binds the writer to the object's thread) and creates accept `X-Thread-Id` (the thread to create into). The served pens `sc-say` and `sc-new` send them; a bare `curl` sends neither. [Agent Rail](Agent%20Rail.md) explains threads.

**Errors** are a status code with a JSON `detail`:

```json
{"detail": "Device gpu-1 already registered"}
```

| code | meaning |
|---|---|
| `400` | a bad value the registry checked (an unknown `service_id`, an empty comment, a bad `limit`) |
| `401` | no bearer, or one the plane doesn't know |
| `403` | you can see it, but your role or token doesn't allow this |
| `404` | not found, or outside your scope |
| `409` | a conflict: a duplicate, a port or subdomain already taken, a claim another live session holds, a lease refused |
| `413` / `415` / `507` | an upload over the size cap, of a content type not allowed, or one that would cut into the disk reserve |
| `422` | validation: a missing or unknown field, a wrong type |
| `500` | a server error |

**Paging.** `limit` is checked on every list that takes it (1 to 500, else `400`). `GET /tickets` also takes `offset` and returns the total in the `X-Total-Count` header.

---

## System

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/` | none | Service identity and release (registry port only) |
| GET | `/health` | none | Health check: database reachable, device count (registry port only) |
| GET | `/api/v1/` | none | Curated API index; `version`, `commit`, `schema`, `helpers` and `skill` name the release; `modules` says which optional modules are on |
| GET | `/api/v1/status` | scoped | Devices, services, ports, routes and open tickets in one answer, with counts. Admin: everything; scoped: what derives from your devices; no or an invalid bearer: empty |
| GET | `/api/v1/audit` | scoped | Recent audit entries (`?limit` default 50, `?action=`). Admin: all; scoped: entries you made or that target your devices and services; no bearer: empty |
| POST | `/api/v1/probe` | admin | Run every service's health endpoint now; updates `status` and `last_health` |

### GET /

```bash
curl http://localhost:7778/
```

```json
{"service": "samcloud-registry", "version": "0.13.1", "commit": "abc1234", "schema": 20,
 "helpers": {"rail": "3.18.0", "listen": "2.8.0", "…": "…"}, "skill": "3.2.1"}
```

`version` is the release the deploy host assigned at build (`dev` from a checkout) and `commit` the commit built; `schema`, `helpers` and `skill` are the schema and the served stamps. `GET /api/v1/` carries the same fields.

### GET /health

```bash
curl http://localhost:7778/health
```

```json
{"status": "healthy", "devices": 12}
```

On failure:

```json
{"status": "unhealthy", "error": "unable to open database file"}
```

### GET /api/v1/status

The dashboard's one-request view.

```bash
curl -s "${H[@]}" "$PLANE/status"
```

```json
{
  "devices": [...],
  "services": [...],
  "ports": [...],
  "routes": [...],
  "open_tickets": [...],
  "counts": {"devices": 12, "services": 25, "ports": 30, "routes": 21, "open_tickets": 3}
}
```

### GET /api/v1/audit

Every mutation is logged: device and service registration, port allocation, Caddy reloads, DNS records, tokens, group changes and more.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| limit | int | 50 | Entries to return (at most 500) |
| action | string | — | Filter by action, e.g. `device.register`, `port.allocate`, `caddy.reload` |

```bash
curl -s "${H[@]}" "$PLANE/audit?action=port.allocate&limit=10"
```

```json
[
  {
    "id": 1,
    "actor": "system:registry",
    "action": "device.register",
    "target": "gpu-1",
    "detail": "{\"hostname\": \"GPU-1\", \"context\": \"tunnel\", \"tunnel_port\": 10001}",
    "created_at": "…"
  }
]
```

Some actions: `device.register`, `device.activated`, `device.deleted`, `service.register`, `service.slot.activated`, `service.deleted`, `port.allocate`, `port.release`, `route.append`, `route.remove`, `caddy.reload`, `forward.started`, `record.added`, `record.clobbered`, `ticket.create`, `ticket.update`, `doc.revise`, `group.member.add`, `access_key.generated`, `auth.login`, `user.invite`. `grep -o 'audit([^,]*, *"[a-z_.]*"' registry/*.py` lists them all.

---

## Setup and login

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/v1/setup/status` | none | `{"initialized": bool}`: does any user exist. The only unauthenticated fact about users |
| POST | `/api/v1/init` | none | First-run admin setup `{username, password}`; works only while there are no users. Returns an admin token |
| POST | `/api/v1/auth/login` | none | `{username, password}` → `{username, role, scopes, token, expires_in}`. Each login gets its own token, valid a week; long-lived tokens are untouched |
| POST | `/api/v1/auth/logout` | that login's token | End this login. A long-lived token is not revoked here (`404`); use `regenerate-token` |
| GET | `/api/v1/auth/verify` | any | Who the token is: `owner_id`, `username`, token type, scopes, groups with roles, `default_group`, and the operator's time. `?scope=` checks one scope |

## Users

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/v1/users` | any | Admin: everyone; anyone else: themselves. A group's roster is `GET /groups/{id}` |
| GET | `/api/v1/users/{user_id}` | any | Self or admin: the whole row; a fellow member of any of your groups: name, role and the groups you share; otherwise `404` |
| POST | `/api/v1/users` | admin | Create a user |
| PATCH | `/api/v1/users/{user_id}` | admin | Change role or scopes; changing scopes rotates the token |
| DELETE | `/api/v1/users/{user_id}` | admin | Delete a user and all their tokens |
| POST | `/api/v1/users/{user_id}/regenerate-token` | admin | Issue a new long-lived token |
| POST | `/api/v1/users/invite` | admin, or editor | Create a user and a single-use invite token. A group editor or owner invites a `user` or `agent` into groups they edit, at a group role up to their own, and onto devices they manage. Only the admin invites an admin or grants `*` |
| GET | `/api/v1/users/brief/{token}` | token | Invitation briefing (markdown) |
| POST | `/api/v1/users/activate` | token | Spend the invite, get the permanent token |
| GET | `/api/v1/users/me/guide` | self | Your persistent operations guide (markdown) |
| GET | `/api/v1/users/me/briefing` | self | One-call briefing: unread inbox, open items blocked on you, decision tasks, a short pick-me menu, movement in the last `?window_hours` |

[Identity and Access](Identity%20and%20Access.md) covers users, tokens and invites.

## Groups

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/v1/groups` | any | The groups you belong to (admin: all) |
| POST | `/api/v1/groups` | any | `{id, name, description?, doc_id?}`; you become its owner |
| GET | `/api/v1/groups/{group_id}` | viewer | Purpose, working doc, members with roles, devices, services, `your_role`; `404` if you're not in it |
| PATCH | `/api/v1/groups/{group_id}` | owner | `name`, `description`, `route_path`, `doc_id` (the working doc; `0` clears) |
| GET | `/api/v1/groups/{group_id}/status` | viewer | Status filtered to the group's services |
| POST | `/api/v1/groups/{group_id}/members` | owner | `{user_id, role}`, role `viewer`, `editor` or `owner` |
| PATCH | `/api/v1/groups/{group_id}/members/{user_id}` | owner | Change a member's role |
| DELETE | `/api/v1/groups/{group_id}/members/{user_id}` | owner | Remove a member |
| GET | `/api/v1/groups/{group_id}/devices` | viewer | The group's devices |
| POST | `/api/v1/groups/{group_id}/devices` | editor | `{device_id}`: a device you manage joins the group |
| DELETE | `/api/v1/groups/{group_id}/devices/{device_id}` | owner | Remove a device from the group |
| POST | `/api/v1/groups/{group_id}/services` | editor | `{service_id}`: a service you can change joins the group |
| DELETE | `/api/v1/groups/{group_id}/services/{service_id}` | owner | Remove a service from the group |
| GET | `/api/v1/admin/scope/unscoped` | admin | Anything that still has no group; empty unless a row was written around the API |

## Devices

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/v1/devices` | scoped | Devices in your scope (admin: all). `?context=samcloud` or `tunnel` |
| GET | `/api/v1/devices/{device_id}` | scoped | One device with its `services` |
| GET | `/api/v1/devices/{device_id}/guide` | scoped | How to reach it, what runs on it (markdown) |
| POST | `/api/v1/devices` | admin | Register a device row directly. Most devices join by enrolment instead |
| PATCH | `/api/v1/devices/{device_id}` | manager | Update device fields |
| DELETE | `/api/v1/devices/{device_id}` | manager | Delete a device, with its services, routes, tickets, resources, leases, ports and tokens |
| POST | `/api/v1/devices/{device_id}/host-key` | admin, or the device's own token | `{host_pubkey}`: pin the host key the gateway checks hops against; only if none is pinned (`409` on a different key) |
| DELETE | `/api/v1/devices/{device_id}/host-key` | admin, or the device's own token | Clear the pin, so a legitimate re-key is accepted |
| GET | `/api/v1/devices/{device_id}/helpers` | scoped | Post-enrolment helper scripts that apply to this device (`wsl-keepalive`, `tunnel-debug`) |
| GET | `/api/v1/devices/{device_id}/helpers/{name}` | scoped | One helper, with the device's values filled in |

[Devices and Enrolment](Devices%20and%20Enrolment.md) covers devices, targets and routes.

### GET /api/v1/devices

```bash
curl -s "${H[@]}" "$PLANE/devices"
curl -s "${H[@]}" "$PLANE/devices?context=tunnel"     # only devices reached by reverse tunnel
```

`context` is `samcloud` or `tunnel`; the old name `workcloud` still matches `tunnel`.

### GET /api/v1/devices/{device_id}

```bash
curl -s "${H[@]}" "$PLANE/devices/gpu-1"
```

```json
{
  "id": "gpu-1",
  "name": "GPU-1",
  "hostname": "GPU-1",
  "context": "tunnel",
  "type": "server",
  "owner_id": null,
  "outbound_method": "reverse-tunnel",
  "outbound_ip": null,
  "outbound_ssh_user": "you",
  "outbound_ssh_port": 22,
  "tunnel_port": 10001,
  "transport_owner": null,
  "host_pubkey": "ssh-ed25519 AAAA…",
  "startup_method": "systemd",
  "enrolled": 1,
  "status": "online",
  "services": [
    {"id": "gpu-1/model-gateway", "name": "model-gateway", "port": 8450, "…": "…"},
    {"id": "gpu-1/render-api", "name": "render-api", "port": 8440, "…": "…"}
  ]
}
```

### POST /api/v1/devices

```bash
curl -s -X POST "${H[@]}" "$PLANE/devices" -H "Content-Type: application/json" \
  -d '{"id": "new-workstation", "name": "New WS", "type": "server",
       "outbound_method": "reverse-tunnel", "outbound_ssh_user": "you", "startup_method": "systemd"}'
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| id | string | Yes | | Unique device identifier |
| name | string | No | `id` | Display name |
| type | string | No | `device` | Freeform: `device`, `server`, `phone`, `bot` |
| owner_id | int | No | null | The user who owns it |
| inbound_method, inbound_endpoint | string | No | null | How the device reaches the plane (`ssh-tunnel`, `lan`, `browser-only`) and at what |
| outbound_method | string | No | null | How the plane reaches it: `direct-ssh`, `reverse-tunnel`, `http` |
| outbound_ip | string | No | null | LAN IP for direct access |
| outbound_ssh_user | string | No | null | SSH user on the device |
| outbound_ssh_port | int | No | 22 | SSH port on the device |
| tunnel_port | int | No | null | Allocated from the tunnel range for `reverse-tunnel` when null |
| transport_owner | string | No | null | Borrow this device's reverse tunnel instead of opening one (a second user on an enrolled machine) |
| startup_method | string | No | null | `systemd`, `launchd`, `scheduled-task`, `docker` |
| recovery_method | string | No | null | `event-driven`, `polling`, `restart-policy`, `none` |
| lifeline_port, monitor_port | int | No | null | Recorded in the port ledger when given; nothing allocates them |

The older names `hostname`, `context` (`tunnel` implies `reverse-tunnel`), `ip` and `ssh_user` are still accepted.

```json
{"status": "registered", "device_id": "new-workstation", "tunnel_port": 10008}
```

`409` if the id is already registered. Allocated ports are recorded in the `ports` ledger, and the registration is audited.

## Device enrolment

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/v1/enroll` | admin, or editor | Enrol a device as a route (`access_route`), a target (`access_target`: `direct-ssh`, `reverse-tunnel`, `http`) or both. A target gets a single-use `sc_enroll_` token and a `bootstrap_command`. The device joins the `group_ids` named, each one a group the caller edits, or else every group the caller edits. The device ID can't contain `:` or `/` or match a username |
| GET | `/api/v1/enroll/pending` | admin | Devices awaiting activation |
| POST | `/api/v1/enroll/{device_id}/retoken` | manager | A fresh enrolment token for an existing device |
| GET | `/api/v1/bootstrap/{token}` | token | The bootstrap script (bash), with the plane's values filled in |
| GET | `/api/v1/bootstrap/{token}/ps1` | token | The bootstrap script for native Windows (PowerShell) |
| POST | `/api/v1/devices/activate` | token | Called by the bootstrap script: spends the enrolment token, records the device's keys, returns its device token |
| GET | `/api/v1/gateway/host-key` | none | The gateway's SSH host keys, so a device can verify the tunnel it dials (`503` if unpublished) |

```bash
curl -s -X POST "${H[@]}" "$PLANE/enroll" -H 'Content-Type: application/json' \
  -d '{"device_id": "box-1", "name": "Box 1", "type": "server", "access_target": "reverse-tunnel", "target_ssh_user": "you"}'
```

[Devices and Enrolment](Devices%20and%20Enrolment.md) walks through it, starting from the dashboard.

## Access keys and pairing

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/v1/access-keys` | self or admin | `{username, device_name}`: generate a gateway keypair; returns the private key once. Your own username, or anyone's as admin |
| POST | `/api/v1/access-keys/setup-script` | self or admin | The same, as a bash script that installs the key and the `samcloud` SSH host |
| POST | `/api/v1/access-keys/pair` | self or admin | A single-use pair link (one hour) for a device with no samcloud token; answers a `pair_command` to run on that device |
| GET | `/api/v1/pair/{token}` | the link | Spend a pair link: mint the key and serve its setup script; `410` once used or expired |
| GET | `/api/v1/access-keys` | self or admin | Access keys (public halves only): your own, or all as admin (`?username=`) |
| DELETE | `/api/v1/access-keys/{key_id}` | self or admin | Revoke a key: your own, or any as admin |

## Gateway

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/v1/gateway/pubkey` | none | The gateway's public key, which target devices put in `authorized_keys` |
| POST | `/api/v1/gateway/keygen` | admin | Generate the gateway keypair if it doesn't exist |
| GET | `/api/v1/gateway/status` | any | `authorized_keys` and gateway key state |
| POST | `/api/v1/gateway/sync` | admin | Write `authorized_keys` from the registry now |
| GET | `/api/v1/auth/can-access` | admin | `?username=&device_id=`: can this user enter this device. The gateway's admission check, by the same predicate as the API |
| GET | `/api/v1/auth/accessible-devices` | admin | `?username=`: the devices this user can enter; what the gateway lists |

[SSH Gateway](SSH%20Gateway.md) covers connecting and revoking.

## Services

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/v1/services` | scoped | Services on devices in your scope (admin: all). `?device_id=`, `?status=` |
| GET | `/api/v1/services/{service_id}` | scoped | One service; ids are `device_id/name` |
| GET | `/api/v1/services/{service_id}/guide` | scoped | Persistent operations guide (markdown) |
| POST | `/api/v1/services` | manager | Register a service on a device you manage; wires the forward, the Caddy route and the DNS record |
| PATCH | `/api/v1/services/{service_id}` | service manager | Update metadata. The admin, the service's own token, its creator, or its device's manager |
| PUT | `/api/v1/services/{service_id}` | — | Not supported: answers `405`; use PATCH |
| POST | `/api/v1/services/{service_id}/health` | service manager | Report health (the service's own token, usually) |
| DELETE | `/api/v1/services/{service_id}` | service manager | Delete a service: stops its forward, removes its route and DNS record, frees its ports and tokens |

Service statuses: `online`, `offline`, `maintenance`, `degraded`, `active`, `pending`, `stopped`, `unknown`. [Services and Routes](Services%20and%20Routes.md) covers services, slots and routes.

### GET /api/v1/services/{service_id}

```bash
curl -s "${H[@]}" "$PLANE/services/gpu-1/model-gateway"
```

```json
{
  "id": "gpu-1/model-gateway",
  "name": "model-gateway",
  "device_id": "gpu-1",
  "port": 8450,
  "slice_port": 18008,
  "protocol": "http",
  "health_endpoint": "/health",
  "description": "Model gateway: chat and generation",
  "subdomain": "mdl",
  "domain": "example.org",
  "external_url": "https://mdl.example.org",
  "status": "active",
  "version": "0.2.0",
  "capabilities": ["chat", "generate", "model-lifecycle", "personas"],
  "created_by": "user:2"
}
```

### POST /api/v1/services

```bash
curl -s -X POST "${H[@]}" "$PLANE/services" -H "Content-Type: application/json" \
  -d '{"name": "my-api", "device_id": "gpu-2", "port": 9000, "description": "Upload and process jobs",
       "health_endpoint": "/health", "subdomain": "myapi", "version": "0.1.0", "capabilities": ["upload", "process"]}'
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| name | string | Yes | | Service name, unique per device: lowercase letters, digits and hyphens |
| device_id | string | Yes | | A device you manage |
| port | int | Yes | | Port on the device |
| description | string | Yes | | What the service does |
| slice_port | int | No | null | Forward port on the plane; allocated from the forward range for a reverse-tunnel device when null |
| protocol | string | No | `http` | `http`, `https`, `ws`, `tcp` |
| health_endpoint | string | No | null | Path for health checks, e.g. `/health` |
| docs_endpoint | string | No | null | Path to the service's own docs |
| subdomain | string | No | null | Subdomain for external access; null keeps it internal |
| domain | string | No | the service domain | `SAMCLOUD_DOMAIN`, else `PUBLIC_URL`'s host ([DNS and TLS](DNS%20and%20TLS.md#the-service-domain)) |
| version | string | No | null | Service version |
| capabilities | string[] | No | null | Capability tags |

```json
{
  "status": "registered",
  "service_id": "gpu-2/my-api",
  "port": 9000,
  "slice_port": 18011,
  "external_url": "https://myapi.example.org",
  "service_token": "sc_service_…",
  "guide_url": "https://cloud.example.org/api/v1/services/gpu-2/my-api/guide",
  "route": {"forward": {"success": true, "message": "…"}, "caddy": {"success": true, "message": "…"},
            "dns": {"success": null, "skipped": true, "message": "…"}, "verified": {"reachable": true, "…": "…"}}
}
```

`404` if the device is outside your scope; `409` if the service, the subdomain or the device port is already taken. With a subdomain, a route is recorded and pushed to Caddy, and the DNS record is added when the `dns-namecheap` module is on. The service joins the device's groups.

## Service enrolment (slots)

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/v1/services/enroll` | admin | Create a service slot and its single-use token |
| GET | `/api/v1/enroll/services/brief/{token}` | token | The slot's briefing for the agent that will activate it (markdown) |
| POST | `/api/v1/services/activate` | token | Activate the slot with runtime details; wires forward, route, DNS and Caddy and returns the guide |
| GET | `/api/v1/enroll/services/pending` | admin | Slots awaiting activation |
| POST | `/api/v1/services/register` | a device token | Register a service with the device's own token |

## Ports

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/v1/ports` | any | Every allocation (`?type=`). Not scope-filtered |
| GET | `/api/v1/ports/check/{port}` | any | Is the port in the ledger, and is anything listening on it inside the registry's container |
| POST | `/api/v1/ports/allocate` | manager | Allocate the next free port for a device you manage (`owner_id` is the device or `device/service`); any other owner is the admin's |
| DELETE | `/api/v1/ports/{port}` | admin | Release an allocation (`?type=` when the port is allocated for more than one type) |

### Port ranges

| Type | Range | Purpose |
|------|-------|---------|
| tunnel | `TUNNEL_RANGE_START`–`END` (10001–10099) | Device reverse tunnels, inside the sshd container |
| forward | `FORWARD_RANGE_START`–`END` (18001–18099) | Service forwards, inside the sshd container |
| route | `ROUTE_RANGE_START`–`END` (18101–18110) | A service's own port when services go under an IP address; published by Caddy |
| lifeline | 10013–10099 | Defined; nothing allocates in it |
| local | 3000–3099 | Defined; nothing allocates in it |
| monitor | 19001–19099 | Defined; nothing allocates in it |

### GET /api/v1/ports

```bash
curl -s "${H[@]}" "$PLANE/ports?type=forward"
```

```json
[
  {"port": 18003, "type": "forward", "owner_id": "gpu-2/inference-api", "allocated_at": "…"},
  {"port": 18004, "type": "forward", "owner_id": "gpu-2/ml-pipeline", "allocated_at": "…"}
]
```

### POST /api/v1/ports/allocate

```bash
# The next free forward port
curl -s -X POST "${H[@]}" "$PLANE/ports/allocate" -H "Content-Type: application/json" \
  -d '{"port_type": "forward", "owner_id": "gpu-2/new-service"}'

# A specific port
curl -s -X POST "${H[@]}" "$PLANE/ports/allocate" -H "Content-Type: application/json" \
  -d '{"port_type": "forward", "owner_id": "gpu-2/new-service", "preferred": 18011}'
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| port_type | string | Yes | `tunnel`, `forward`, `route`, `lifeline`, `local`, `monitor` |
| owner_id | string | Yes | The device or `device/service` that owns the port |
| preferred | int | No | A port to take if it is free and in range |

```json
{"port": 18011, "type": "forward", "owner": "gpu-2/new-service"}
```

`409` when the preferred port is taken or out of range, or the range is full. Allocation is atomic and audited.

### GET /api/v1/ports/check/{port}

```bash
curl -s "${H[@]}" "$PLANE/ports/check/18003"
```

```json
{"port": 18003, "allocated": [{"port": 18003, "type": "forward", "owner_id": "gpu-2/inference-api", "allocated_at": "…"}], "in_use_lsof": false}
```

`in_use_lsof` runs `lsof` inside the registry container, which doesn't see ports the sshd or caddy containers bind ([Troubleshooting](Troubleshooting.md#port-allocation-refused)).

## Routes

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/v1/routes` | scoped | Routes for services in your scope, plus routes with no service (admin: all) |
| POST | `/api/v1/routes` | manager of the service's device | Record a route; doesn't activate it. A route with no service is the admin's |
| POST | `/api/v1/routes/{subdomain}/activate` | service manager | Activate one route: start the forward, push the Caddy block, add the DNS record. `?domain=` defaults to the service domain. A route with no service is the admin's |
| POST | `/api/v1/routes/activate-all` | admin | Re-activate every route (forwards and Caddy, no DNS); cold-start recovery |

### GET /api/v1/routes

```json
[
  {"id": 1, "subdomain": "media", "domain": "example.org", "target": "192.0.2.20:8080", "service_id": "mediahost/media", "caddy_extra": null, "created_at": "…"},
  {"id": 2, "subdomain": "mdl", "domain": "example.org", "target": "sshd:18008", "service_id": "gpu-1/model-gateway", "caddy_extra": null, "created_at": "…"}
]
```

### POST /api/v1/routes

```bash
curl -s -X POST "${H[@]}" "$PLANE/routes" -H "Content-Type: application/json" \
  -d '{"subdomain": "myapp", "target": "sshd:18011", "service_id": "gpu-2/my-service"}'
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| subdomain | string | Yes | | e.g. `myapp` |
| domain | string | No | the service domain | |
| target | string | Yes | | Caddy proxy target, e.g. `sshd:18011` |
| service_id | string | No | null | The service it fronts |

```json
{"status": "created", "route": "myapp.example.org", "target": "sshd:18011"}
```

`409` if the route exists (same subdomain and domain).

### POST /api/v1/routes/{subdomain}/activate

```bash
curl -s -X POST "${H[@]}" "$PLANE/routes/myapp/activate?domain=example.org"
```

```json
{"status": "active",
 "forward": {"success": true, "message": "Forward sshd:18011 → tunnel:10003 → :9000"},
 "caddy": {"success": true, "message": "Route myapp.example.org → sshd:18011 added and active"},
 "dns": {"success": null, "skipped": true, "message": "dns-namecheap module is off; point the name at this host with your own DNS"}}
```

`404` if the route isn't in the registry. A Caddy reload that fails restores the previous Caddyfile, and the answer's `caddy.success` is false. [DNS and TLS](DNS%20and%20TLS.md#the-caddyfile) covers the file.

---

## Tickets

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/v1/tickets` | scoped | Tickets you can see, newest first. `?status`, `?service_id`, `?device_id`, `?assignee`, `?source`, `?tag`, `?group_scope` (exact: the ticket's own group), `?limit` (default 100, max 500), `?offset`; `X-Total-Count` header |
| GET | `/api/v1/tickets/{ticket_id}` | scoped | One ticket with its comments and claims |
| POST | `/api/v1/tickets` | editor | File a ticket into a group; the filer and the assignee are subscribed |
| PATCH | `/api/v1/tickets/{ticket_id}` | editor, the filer, or the assignee (status only) | Change status, assignee, priority, service, device, group or tags |
| POST | `/api/v1/tickets/{ticket_id}/comments` | viewer | Comment. Doesn't subscribe the writer; `subscribed` reports the existing watch state |
| POST | `/api/v1/tickets/{ticket_id}/subscribe` | viewer | Opt into comments and record changes (`updates=1`) |
| DELETE | `/api/v1/tickets/{ticket_id}/subscribe` | any | Stop watching; also mutes, so your later comments don't re-enrol you. Assignment, a mention or an explicit subscribe clears the mute |
| GET | `/api/v1/tickets/{ticket_id}/watchers` | viewer | Who watches it, and whether for updates |
| POST | `/api/v1/tickets/{ticket_id}/claim` | viewer | Claim the thread for your live session (writing already claims it); `409` when another live session of your identity holds it |
| DELETE | `/api/v1/tickets/{ticket_id}/claim` | self | Release your claim |

Statuses: `open` → `triaged` → `in_progress` → `review` → `resolved` → `closed`. [Tickets](Tickets.md) covers addressing, scoping and the lifecycle; [Notifications and Subscriptions](Notifications%20and%20Subscriptions.md) covers who hears what.

### GET /api/v1/tickets/{ticket_id}

```bash
curl -s "${H[@]}" "$PLANE/tickets/1"
```

```json
{
  "id": 1,
  "source": "alice",
  "type": "bug",
  "priority": "high",
  "status": "open",
  "summary": "Dashboard has two Flask instances running",
  "body": "PID 1535 and 124917 both running on port 5000...",
  "service_id": "gpu-2/dashboard",
  "device_id": "gpu-2",
  "assignee": null,
  "group_scope": "home",
  "tags": [],
  "resolved_at": null,
  "created_at": "…",
  "updated_at": "…",
  "ref": "…",
  "comments": [
    {"id": 1, "ticket_id": 1, "author": "builder", "body": "Killed stale PID.", "parent_comment_id": null, "created_at": "…"}
  ],
  "claims": [...]
}
```

### POST /api/v1/tickets

```bash
curl -s -X POST "${H[@]}" "$PLANE/tickets" -H "Content-Type: application/json" \
  -d '{"type": "incident", "priority": "critical", "summary": "Caddy returning 502 on all routes",
       "body": "All external URLs down. sslh is healthy, Caddy running, every backend unreachable.",
       "device_id": "box-1"}'
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| summary | string | Yes | | Short description |
| body | string | No | null | Detail. Mentions (`@name`) in it notify members of the ticket's group |
| type | string | No | `bug` | `bug`, `feature`, `question`, `incident` |
| priority | string | No | `medium` | `low`, `medium`, `high`, `critical` |
| service_id, device_id | string | No | null | What it is about; each must exist |
| assignee | string | No | null | A member of the ticket's group, else `422` with a note |
| group_scope | string | No | your default group | The group it is in; with an assignee, the first group you edit that they are in |
| tags | string[] | No | null | Tags |

`source` is the bearer's identity; sending it answers `422`.

```json
{"status": "created", "id": 12, "ticket_id": 12, "ref": "…", "thread_id": 40, "group_scope": "home",
 "subscribed": true, "claim_owned": true, "watch": "…"}
```

### PATCH /api/v1/tickets/{ticket_id}

```bash
curl -s -X PATCH "${H[@]}" "$PLANE/tickets/1" -H "Content-Type: application/json" \
  -d '{"status": "resolved"}'
```

| Field | Type | Description |
|-------|------|-------------|
| status | string | `open`, `triaged`, `in_progress`, `review`, `resolved`, `closed` |
| assignee | string | A member of the ticket's group |
| priority | string | `low`, `medium`, `high`, `critical` |
| service_id, device_id | string | Re-address it |
| group_scope | string | Move it to another group; `""` answers `422` |
| tags | string[] | Replaces the tags |

Resolving or closing sets `resolved_at`. Editing claims the ticket's thread for the writer.

### POST /api/v1/tickets/{ticket_id}/comments

```bash
curl -s -X POST "${H[@]}" "$PLANE/tickets/1/comments" -H "Content-Type: application/json" \
  -d '{"body": "Service recovered after restart. Health check passing."}'
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| body | string | Yes | Comment text; empty answers `400` |
| parent_comment_id | int | No | Reply to a top-level comment on the same ticket (one level deep) |

`author` is the bearer's identity; sending it answers `422`.

```json
{"status": "created", "comment_id": 31, "session_fp": null, "home_thread_id": null,
 "subscribed": false, "claim_owned": true, "watch": "…"}
```

A mention (`@name`) notifies that member of the ticket's group; `notes` says when a mentioned name was not notified. From 30 comments on, the answer carries a `thread_notice`.

## Docs

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/v1/docs` | scoped | Docs you can see, without bodies. `?kind`, `?status`, `?group_scope`, `?linked_ticket`, `?q`, `?parent_id` |
| POST | `/api/v1/docs` | editor | Create a doc: revision 1. `source` is the bearer's identity; `owner` defaults to it. Returns `thread_id` |
| GET | `/api/v1/docs/{ident}` | scoped | A doc by slug or id: head body and links. `?rev=N` for a past revision |
| PATCH | `/api/v1/docs/{doc_id}` | owner of the doc | Lifecycle metadata: `status`, `kind`, `group_scope`, `supersedes`, `parent_doc_id`, `owner` |
| DELETE | `/api/v1/docs/{doc_id}` | owner of the doc | Permanent, with its history; prefer PATCH `status=archived` |
| GET | `/api/v1/docs/{doc_id}/revisions` | scoped | Revision history (metadata) |
| POST | `/api/v1/docs/{doc_id}/revisions` | editor | A new immutable revision (full body), against `base_rev` |
| POST | `/api/v1/docs/{doc_id}/append` | editor | Append a block to the head without resending the body; still a revision |
| POST | `/api/v1/docs/{doc_id}/comments` | viewer | Discuss the doc, apart from its body. Doesn't subscribe the writer |
| POST | `/api/v1/docs/{doc_id}/links` | editor | Link the doc to a ticket, service, device or doc |
| DELETE | `/api/v1/docs/{doc_id}/links/{link_id}` | owner of the doc | Remove a link |
| POST | `/api/v1/docs/{doc_id}/subscribe` | viewer | Opt into comments and record changes (`updates=1`) |
| DELETE | `/api/v1/docs/{doc_id}/subscribe` | any | Stop watching; also mutes |
| GET | `/api/v1/docs/{doc_id}/watchers` | viewer | Who watches it |
| POST | `/api/v1/docs/{doc_id}/claim` | viewer | Claim the doc's thread for your live session before the first write |
| DELETE | `/api/v1/docs/{doc_id}/claim` | self | Release your claim |

[Docs](Docs.md) covers revisions, supersession, links and ownership. The served pen `sc-edit` revises a body safely.

## Projects and tasks

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/v1/projects` | scoped | Projects with their computed progress. `?status`, `?group_scope` |
| POST | `/api/v1/projects` | editor | Create a project; the owner is the bearer's identity |
| GET | `/api/v1/projects/{ident}` | scoped | A project by slug or id: rollup and the full task tree |
| PATCH | `/api/v1/projects/{ident}` | owner of the project | Lifecycle and metadata |
| DELETE | `/api/v1/projects/{ident}` | owner of the project | Permanent, with its tasks; prefer PATCH `status=archived` |
| POST | `/api/v1/projects/{ident}/subscribe` | viewer | Opt into the project's task events (`task.created`, `task.updated`, `task.delegated`, `task.commented`) |
| DELETE | `/api/v1/projects/{ident}/subscribe` | any | Stop |
| POST | `/api/v1/projects/{ident}/tasks` | editor | Add a task, or a subtask with `parent_task_id`; answers the `thread_id` it joined |
| GET | `/api/v1/tasks` | scoped | Tasks across your projects. `?project`, `?status`, `?assignee`, `?parent`, `?tag` |
| GET | `/api/v1/tasks/{task_id}` | scoped | One task |
| PATCH | `/api/v1/tasks/{task_id}` | editor | Edit; `status=done` stamps `completed_at`; a new assignee gets one inbox row |
| DELETE | `/api/v1/tasks/{task_id}` | the creator, the project's owner, or admin | Delete; subtasks are re-rooted |
| POST | `/api/v1/tasks/{task_id}/comments` | viewer | Comment. Notifies the assignee, the creator, named recipients and project watchers; doesn't subscribe the writer |
| POST | `/api/v1/tasks/{task_id}/delegate` | viewer | Hand the task to an agent: files a linked ticket in the project's group, assigned to them, and sets the task `blocked`, your waiting handle. `409` if already delegated |
| POST | `/api/v1/tasks/{task_id}/claim` | viewer | Claim the task's thread for your live session |
| DELETE | `/api/v1/tasks/{task_id}/claim` | self | Release your claim |

[Projects](Projects.md) and [Tasks and Schedules](Tasks%20and%20Schedules.md) cover them.

## Files

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/v1/files` | editor | Upload (multipart `file`, `group_scope`). `413` over the size cap, `415` for a content type not allowed, `507` when it would cut into the disk reserve |
| GET | `/api/v1/files` | scoped | Files you can see. `?kind=image` filters by content type; `?q`, `?mine` |
| GET | `/api/v1/files/{file_id}` | scoped | Metadata |
| GET | `/api/v1/files/{file_id}/raw` | scoped | The bytes, with their declared type, never sniffed |
| PATCH | `/api/v1/files/{file_id}` | owner of the file, or admin | Move it to another group |
| DELETE | `/api/v1/files/{file_id}` | owner of the file, or admin | Delete |
| POST | `/api/v1/files/{file_id}/links` | scoped | Attach it to a doc, ticket or task |
| DELETE | `/api/v1/files/{file_id}/links/{link_id}` | owner of the file, or admin | Detach |

[Files](Files.md) covers them.

## Notifications, subscriptions and presence

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/v1/notifications` | self | Your durable inbox (`?unread=true` by default; `?unread=false` includes read rows). `?ticket_id`, `?thread`, `?lane_scope`, `?undelivered=true` (rows the rail has never delivered), `?for_conversation=<id>` (only what is delivered to that conversation), `?unclaimed=true` (rows none of your live claims cover), `?delivery=true`, `?limit` (default 50). An unknown parameter answers `400`. Rows carry `read_object`, `siblings`, `pending_inbox` |
| GET | `/api/v1/notifications/watch` | self | Long-poll until something lands (`?timeout=60`, at most 300); takes the same filters |
| POST | `/api/v1/notifications/ack` | self | Mark handled: `{ids: [...]}` or `{all: true}`, with an optional `surface` |
| POST | `/api/v1/notifications/seen` | self | `{ids, ttl_seconds}` (default 1800): a soft lease on rows a live surface has shown |
| POST | `/api/v1/notifications/delivered` | self | `{ids, conversation_id}`: the rail's delivery receipt. Final: the row is never offered again |
| GET | `/api/v1/subscriptions` | self | Your watch state: the tickets, docs and projects you subscribe to, and your mutes |
| GET | `/api/v1/comments` | scoped | Comments across tickets, docs and tasks: `?author`, `?since` (a watermark), `?plane=ticket|doc|task`, `?limit`; page until `more` is false |
| POST | `/api/v1/presence` | self | Declare a live session for your identity (`ttl_seconds`, default 600; `0` clears) |
| GET | `/api/v1/presence` | self | Your presence |
| DELETE | `/api/v1/presence/{username}` | admin | Clear another identity's presence |

[Notifications and Subscriptions](Notifications%20and%20Subscriptions.md) covers who receives what.

## Events

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/v1/events` | scoped | Server-sent events, filtered to what you can see. `?for_conversation=` |
| GET | `/api/v1/events/recent` | scoped | Recent events, for clients that can't hold a stream (`?limit`) |

[Event Bus](Event%20Bus.md) lists the event types.

## Views

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/v1/views` | self | Your saved views; the first call seeds four editable defaults |
| POST | `/api/v1/views` | self | Create a view |
| PATCH | `/api/v1/views/{view_id}` | self | Change one |
| DELETE | `/api/v1/views/{view_id}` | self | Delete one |
| GET | `/api/v1/views/{view_id}/results` | self | Run it; scope always applies (`?limit`) |

[Dashboard](Dashboard.md) shows them.

## Compute resources and leases

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/v1/resources` | scoped | Resources on your devices (no bearer: empty). `?device_id`, `?type`, `?provider`, `?available`, `?min_memory_mb` |
| GET | `/api/v1/resources/dashboard` | scoped | A normalised at-a-glance view |
| GET | `/api/v1/resources/{resource_id}` | scoped | Detail with active and queued leases, the queue in grant order, `expiry_policy` and unsettled holders |
| POST | `/api/v1/resources` | manager | Register a resource on a device: `{name, device_id, type, provider, model?, allocation_mode, specs?}` |
| POST | `/api/v1/resources/{resource_id}/stats` | manager | Push live utilisation (every 15 s is the recommended rate) |
| PATCH | `/api/v1/resources/{resource_id}` | manager | `status`, `specs`, `model`, `expiry_policy` (`free` or `hold`) |
| DELETE | `/api/v1/resources/{resource_id}` | admin or its owner | Delete |
| POST | `/api/v1/resources/{resource_id}/leases` | scoped | Request a lease: granted, queued (with `wait_s` or `wait: true`), or `409` with a `detail.code` |
| GET | `/api/v1/leases` | scoped | Leases on your resources (no bearer: empty). `?resource_id`, `?service_id`, `?status` |
| GET | `/api/v1/leases/{lease_id}` | scoped | One lease, with its queue position while it waits |
| GET | `/api/v1/leases/{lease_id}/wait` | scoped | Long-poll a queued lease (`?timeout_s`, at most 120) |
| POST | `/api/v1/leases/{lease_id}/renew` | the holder, or admin | `{ttl_seconds}`: extend in place, up to the lease's ceiling |
| POST | `/api/v1/leases/{lease_id}/rollover` | the holder, or admin, and the resource's manager | `{key, ttl_seconds}`: atomically replace a finite shared allocation |
| DELETE | `/api/v1/leases/{lease_id}` | the holder, or admin | Release; on a `hold` resource, settle an expired holder. Promotes the waiters that now fit |

[Compute Resources and Leases](Compute%20Resources%20and%20Leases.md) covers the queue and `sc-lease`.

## The rail

The rail's registry surface: registrations, thread bindings, surfaces and claims. Seats' listeners and rails call most of these; [Agent Rail](Agent%20Rail.md) and [Thread Lifecycle](Thread%20Lifecycle.md) explain them.

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/v1/wake/register` | any | Register or refresh this identity's rail on a host (`wake_cmd`, `host`, versions). `{ended: true}` retires a row; an admin may retire another identity's |
| GET | `/api/v1/wake/registrations` | scoped | The fleet: versions, staleness, darkness, launch capability. Admin: all; others: their own. Rows are per (identity, host). `?include_retired=true` |
| POST | `/api/v1/wake/beacon` | any | A listener whose stream is dark reports why |
| POST | `/api/v1/wake/session` | any | Report one launch attempt and its outcome; consecutive latching failures mark a seat dark |
| GET | `/api/v1/wake/health` | any | Fleet darkness as a reading: per-seat state and `?transitions=N` of history |
| POST | `/api/v1/agents/{username}/wake` | admin | Wake one agent on a thread: `{thread_id, summary, body?, event_type?}` |
| GET | `/api/v1/wake/threads` | scoped | Thread bindings: yours by default; `?thread=ticket:N|doc:N|task:N` or `?thread_id=` for every identity on one thread; `?all=true` (admin); `?state=` |
| POST | `/api/v1/wake/threads` | self | Record or move your binding for one thread; `409` when a live surface of your identity already holds it |
| PATCH | `/api/v1/wake/threads/{thread_id}` | self or admin | Move a binding's state, or re-stamp its pid |
| POST | `/api/v1/wake/surfaces` | self | Declare a conversation that can receive: `{conversation_id, surface, host?, client?, ended?}` |
| GET | `/api/v1/wake/surfaces` | self | What is listening as you; `?all=true` (admin); `?include_ended=true` |
| GET | `/api/v1/wake/claims` | scoped | Active thread claims: yours, one thread's (`?thread=`), or all (`?all=true`, admin) |
| POST | `/api/v1/wake/claims/refresh` | self | Extend your unexpired claims |
| POST | `/api/v1/wake/claims/release` | self | Release your claims: this surface is going idle |
| GET | `/api/v1/threads` | self | The threads you are bound to, with members, participants and bindings. `?state=open|closed`, `?limit` |
| GET | `/api/v1/threads/{thread_id}` | scoped | One thread; members you can't read show as hidden refs |
| GET | `/api/v1/threads/of/{kind}/{object_id}` | scoped | The thread an object belongs to (`ticket`, `doc`, `task`) |
| PATCH | `/api/v1/threads/{thread_id}` | a person or admin | `{state, title}`; closing writes `thread.closed` to every bound instance |
| POST | `/api/v1/threads/{thread_id}/members` | scoped | `{kind, id}`: move an object into this thread |

## Helpers and skills

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/v1/helpers/{name}` | any | A served rail or agent tool, with this plane's values filled in: `rail`, `listen`, `wake`, `adapter`, `wake-install`, `say`, `new`, `edit`, `pr`, `lease`, `subscribe`, `channel`, `scope-check`, `wsl-boot` |
| GET | `/api/v1/skills` | none | The skills the registry serves, with each one's pages |
| GET | `/api/v1/skills/install.sh` | none | The skill installer, with this plane's URL in it |
| GET | `/api/v1/skills/{name}/SKILL.md` | none | A skill's entry page, rendered for this plane |
| GET | `/api/v1/skills/{name}/{filename}` | none | One of its other pages |

[Skills](Skills.md) covers the bundle; [Architecture](Architecture.md#served-helpers-and-skills-rendered-from-config) lists what the plane fills in.

## Interactive documentation

FastAPI's generated docs are on the registry's own port: `http://<host>:<REGISTRY_PORT>/docs` (Swagger UI) and `/redoc`, from `/openapi.json`.

## Related

- [Architecture](Architecture.md): how the parts behind these routes fit
- [Identity and Access](Identity%20and%20Access.md): tokens, scopes and roles
- [Configuration](Configuration.md)

Next: [Contributing](Contributing.md)
