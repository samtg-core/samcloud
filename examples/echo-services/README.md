# echo-services: the reference services for the service standard

Two small services that meet the plane's service standard (doc 793, and "The standard" in `tools.md`). They exist for three things:

- **a template** a new service copies;
- **`sc-job check`'s self-test** (`sandbox/test_echo_services.py` runs it against both);
- **the cold read:** a seat with ordinary credentials calls each one starting from `GET /services` alone.

They are test fixtures, for testing and copying, not for real work.

| | kind | does | guide |
|---|---|---|---|
| `echo_job.py` | `compute-job` | leases 64 MB as its own id, waits, writes `echo.json`, stores it on the caller's grant, releases | `guide-echo-job.md` |
| `echo_api.py` | `no-compute` | `POST /echo` answers with the JSON body | `guide-echo-api.md` |

Both are one file, standard library only (Python 3.9+), with no state on disk: a restart drops the jobs in flight, and their leases expire on the plane's TTL (120 s).

## What to copy

- **The bearer, on every call** (`caller()`): `GET /auth/verify` with the caller's header, cached a minute by its hash. The token itself is never kept or logged. Only `/health` and `/service-docs` answer without one. A refused token is `401` with `WWW-Authenticate`; a plane that can't be reached is `503`, never a pass.
- **Its own id for compute** (`take_lease`, `release`): the lease is taken with the service token, so the plane names the service as holder. It's released in a `finally`, retried through a 5xx or a dropped connection for 5 minutes, and counted gone only on a 2xx or the plane's own `404` for that lease (`queue.md`).
- **Bounded, so callers can't spend the plane through it** (`worker`, the caps in `submit`): one worker runs the jobs in order, so however many come in, the service holds one lease and one long-poll on the plane at a time. A wait that fails backs off, 2 s doubling to 60. At most 4 jobs wait or run at once, and 2 per caller; beyond that is `429` with `Retry-After`. Each waiting `GET /leases/{id}/wait` holds one of the plane's request workers, so a service that opens one per job can stall the plane.
- **The lease back on shutdown** (`shutdown`): SIGTERM withdraws what's waiting and releases the lease before the process exits.
- **Results to the caller's grant** (`check_grant`, `upload`): `POST /grants/check` before the job is queued, refusing a grant someone else made; `POST /files` with the grant after the work and before `done`, because `sc-job` revokes the grant when it sees `done`.
- **The `sc-job` shape:** `job`, `poll`, `status`, `error`, `results` under `/jobs/<id>/files/`, `plane_files` or `plane_error`, `DELETE` to withdraw, and `{}` refused with `422` and a JSON `error`.
- **Nothing reflected:** no header comes back, and the `grant` field and any `sc_grant_…` secret are stripped from what's echoed or written.

## Running them on lagoon

They run beside the plane on lagoon, walled off from it (`compose.yaml`). Each is a `python:3.13-alpine` container, pinned by digest, with:

- uid 64100, which has no account on the host, a read-only root, no capabilities and `no-new-privileges`;
- 128 MB of memory with no swap, a quarter of a CPU, 128 processes, and logs capped at 3 MB;
- its own network, and only three read-only mounts: its script, its guide and its own token header file. None of the plane's volumes, database or secrets;
- 20 s on `docker compose down` to give its lease back.

They reach the plane the way any caller does, at `https://cloud.samtg.xyz`. Each listens on lagoon's LAN address (`LAN_IP`, default `192.168.1.31`), which is where the plane's route for a direct-ssh device goes.

**1. The slot `test-echo-job` leases.** Lagoon has no compute of its own to lease, so it gets a small one that exists for this:

```
curl -s -H @"$HOME/.samcloud/token.hdr" -H 'Content-Type: application/json' -X POST "$API/resources" -d '{
  "name": "test-slot", "device_id": "lagoon", "type": "memory", "provider": "samcloud", "allocation_mode": "shared",
  "specs": {"ram_mb": 256, "notes": "For test-echo-job, the reference compute job (doc 793). Nothing else uses it."}}'
```

Its `expiry_policy` is `free`, the default: a holder that dies stops counting at once.

**2. Register both.** The reply carries the service's token, so it goes to a `0600` file outside this checkout, never into it:

```
umask 077; mkdir -p ~/.config/samcloud/test-echo
curl -s -H @"$HOME/.samcloud/token.hdr" -H 'Content-Type: application/json' -X POST "$API/services" -d '{
  "name": "test-echo-job", "device_id": "lagoon", "port": 8811, "subdomain": "test-echo-job",
  "health_endpoint": "/health", "docs_endpoint": "/service-docs", "version": "1.0.0",
  "capabilities": ["compute-job", "echo", "test-fixture"],
  "description": "Reference service for the plane'"'"'s service standard (doc 793). For testing and copying, not for real work."}' \
  > ~/.config/samcloud/test-echo/test-echo-job.reply.json
python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["route"], indent=1))' ~/.config/samcloud/test-echo/test-echo-job.reply.json
```

`test-echo-api` is the same with its name, port `8812`, subdomain `test-echo-api` and `["no-compute", "echo", "test-fixture"]`. Read each step of the `route` it prints.

**3. The token header files,** one per service, readable only by uid 64100, in `/etc/samcloud-test-echo/` (`TOKENS`). Each container mounts only its own:

```
sudo install -d -m 0700 -o 64100 -g 64100 /etc/samcloud-test-echo
python3 -c 'import json,sys; print("Authorization: Bearer " + json.load(open(sys.argv[1]))["service_token"])' \
  ~/.config/samcloud/test-echo/test-echo-job.reply.json \
  | sudo install -m 0400 -o 64100 -g 64100 /dev/stdin /etc/samcloud-test-echo/test-echo-job.hdr
rm ~/.config/samcloud/test-echo/test-echo-job.reply.json
```

The same for `test-echo-api`. The token is never in `compose.yaml`, on a command line or in this directory, and each container sees only its own script, its guide and its own header.

**4. Start them:** `docker compose -p samcloud-test-echo up -d` in this directory. Then run one job, so the plane has a lease to show for `own id`, and check both:

```
sc-job call lagoon/test-echo-job /jobs '{"text": "hello"}' --store <a group you edit>
sc-job check lagoon/test-echo-job --submit /jobs
sc-job check lagoon/test-echo-api
```

## Copying one elsewhere

Settings are environment variables; each file's docstring lists them. On a Mac, launchd runs it with the same variables in a plist (`ProgramArguments` `python3 -I <file>`, `KeepAlive`). `HOST` is where the plane's route reaches the device: `127.0.0.1` on a reverse-tunnel device, the LAN address (`outbound_ip` in `GET /devices/{id}`) on a direct one. Lease a resource on the device the service runs on, with its own service token.
