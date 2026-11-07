# Troubleshooting

Real failure modes and the checks that tell them apart. Every command runs from the checkout's `deploy/` directory on the control-plane host, with the env file the plane was started with; `$PLANE` is the API base (`PUBLIC_URL` followed by `/api/v1`), and the bearer comes from a 0600 header file.

Two facts explain most of the checks below:

- **Tunnels and forwards live inside the `sshd` container.** A device's reverse tunnel lands on the container's loopback, and the forward that exposes a tunnelled service listens on the container's network. Neither range is published on the host, so `lsof` on the host shows nothing for them. The `sshd` image has `curl` but no `ss` or `lsof`; read `/proc/net/tcp` instead.
- **The registry is the record.** `GET /api/v1/devices/{id}` gives a device's `tunnel_port`, `GET /api/v1/services/{id}` a service's `port` and `slice_port`, `GET /api/v1/ports` every allocation.

## Service returning 502

Caddy reached nothing at the route's target.

1. Check the service on its device, through the gateway:
   ```bash
   ssh samcloud "DEVICE -- curl -s localhost:SERVICE_PORT/health"
   ssh samcloud "DEVICE -- systemctl status SERVICE_UNIT"
   ```
2. If the device is reached by reverse tunnel, check the forward answers inside `sshd`:
   ```bash
   docker compose --env-file .env exec sshd curl -s localhost:SLICE_PORT/health
   ```
3. If the service is up but the forward is gone, re-activate its route. This re-binds the forward and pushes the Caddy route again:
   ```bash
   curl -X POST "$PLANE/routes/SUBDOMAIN/activate" -H @"$HOME/.samcloud/token.hdr"
   ```
   The registry also re-establishes forwards on its own every `FORWARD_RECONCILE_INTERVAL` seconds (120 by default), since they die when `sshd` restarts.
4. If the forward is gone because the tunnel is, see [Tunnel not connecting](#tunnel-not-connecting).

## Tunnel not connecting

1. Check whether the device's tunnel port is listening inside `sshd`. The port is in hex in `/proc/net/tcp`, and state `0A` is LISTEN:
   ```bash
   docker compose --env-file .env exec sshd \
     sh -c 'grep -i ":$(printf %04X TUNNEL_PORT) " /proc/net/tcp'
   ```
2. No line means the device is offline or its tunnel unit is down. The bootstrap installs the unit as:

   | device | tunnel unit |
   |---|---|
   | Linux, WSL | systemd unit `samcloud-tunnel-DEVICE` |
   | macOS | LaunchAgent `com.samcloud.tunnel-DEVICE` |
   | Windows | scheduled task `samcloud-tunnel-DEVICE` |

   A plane whose `SAMCLOUD_ENV` isn't `prod` adds its name: `samcloud-lab-tunnel-DEVICE`, `com.samcloud.lab.tunnel-DEVICE`, `samcloud-lab-tunnel-DEVICE`.
3. Check the unit on the device, by whatever other way you reach it. If the tunnel port still answers, the gateway reaches the device through it:
   ```bash
   ssh samcloud "DEVICE -- systemctl status samcloud-tunnel-DEVICE"
   ```
4. On Windows, the device's `tunnel-debug` helper writes a verbose log of the task and one `ssh -v` attempt: `GET /api/v1/devices/DEVICE/helpers` lists it.

## Port allocation refused

The registry hands out tunnel, forward and route ports from fixed ranges ([Configuration](Configuration.md#ports-and-ranges)), and records each one in its `ports` ledger. `POST /api/v1/ports/allocate` answers `409` when:

- the preferred port is already allocated to another owner;
- the preferred port is outside the type's range;
- the range has no free port left (`No available ports in forward range [18001-18099]`).

Check the ledger:

```bash
curl -s "$PLANE/ports/check/PORT" -H @"$HOME/.samcloud/token.hdr"
curl -s "$PLANE/ports?type=forward" -H @"$HOME/.samcloud/token.hdr"
```

An allocation whose owner is gone (a deleted service, a device moved onto a shared tunnel) can be released by an admin with `DELETE /api/v1/ports/PORT` (add `?type=` if the port is allocated for more than one type). A range that is simply full needs a wider range in the env file.

The ledger's `in_use_lsof` field runs `lsof` inside the registry container, which sees nothing the `sshd` or `caddy` containers bind, so it is not evidence of what holds a tunnel or forward port.

A host port can also collide when compose starts: `Bind for 0.0.0.0:7778 failed: port is already allocated`. Another process or stack holds one of `REGISTRY_PORT`, `CONSOLE_PORT`, `HTTPS_PORT`, `HTTP_PORT`, `SSH_PORT`, `SSLH_PORT` or the route range; change the setting or stop the other process.

## Caddy won't reload

1. Validate the file:
   ```bash
   docker compose --env-file .env exec caddy caddy validate --config /registry-data/Caddyfile
   ```
2. Common issues: a missing closing brace, a duplicate site block, an invalid upstream.
3. The registry's route calls restore the previous file when a reload fails, and keep the file before each change as `Caddyfile.bak.registry`. A hand edit has no such guard.
4. The registry reloads through Caddy's admin API on `caddy:2019`; if the registry's log shows that call failing, check the `caddy` container is running.

[DNS and TLS](DNS%20and%20TLS.md#the-caddyfile) describes the file.

## DNS not resolving

Only with a domain. With no domain, services are reached by port and need no DNS.

1. Check the name: `dig +short mysubdomain.example.org`.
2. A new record can take 1 to 30 minutes to appear, and Caddy won't get a public certificate until it does.
3. Check the module is on: `GET /api/v1/` reports `modules.dns-namecheap`. With it off, the route's answer says `"dns": {"skipped": true, …}` and the record is yours to create.
4. With the module on, check the record exists in Namecheap. Its `setHosts` call replaces the whole record set, so check nothing else went missing; the audit log has a `record.clobbered` entry when the module had to retry.

## The gateway's sshd doesn't start

The `sshd` container waits, at start, until the registry has written `authorized_keys` under the key policy its own release expects. Its log stops at:

```
[sshd] Waiting for authorized_keys at keys-policy=2...
```

The registry writes the file at every start, so this means the registry is not running, or is running an older release than `sshd`. Recreate both from the same build: `docker compose --env-file .env up -d --build registry sshd`.

## Related

- [Deployment](Deployment.md): upgrading, and what a deploy recreates
- [DNS and TLS](DNS%20and%20TLS.md)
- [Services and Routes](Services%20and%20Routes.md)
- [SSH Gateway](SSH%20Gateway.md)
- [Rail Operations](Rail%20Operations.md): a rail that has gone dark

Next: [Architecture](Architecture.md)
