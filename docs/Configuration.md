---
tags: [reference, configuration]
---

# Configuration

Every setting the control plane reads: its default and what it does.

An instance's settings live in the env file passed to compose (`docker compose --env-file .env up -d`). `deploy/.env.example` lists the ones an operator sets. Everything else here is set by `deploy/docker-compose.yml` or an image, or, for the parts that run on the host, by their service files. An instance's other facts (devices, services, routes, users, groups) are registry data entered through the API, not config.

Compose passes a setting to a container only when its `environment:` block names it. A setting marked *not passed by compose* is read by the code but never reaches the container from the env file; to change it, add it to that service's `environment:` block.

Defaults: "(required)" means compose or the code refuses to run without it. "—" means unset turns the feature off or leaves the value empty. Where compose and the code fall back to different values, both are given.

`python3 sandbox/test_config_reference.py` keeps this list complete: it derives every env var the code reads and fails on one this page doesn't name, on a row here that nothing reads, and on a key in `deploy/.env.example` missing from this page.

This page is the reference. What the settings are for is on the concept pages:

| page | covers |
|---|---|
| [Deployment](Deployment.md) | installing and upgrading a plane, the build and deploy helpers, the deploy receiver |
| [DNS and TLS](DNS%20and%20TLS.md) | the service domain, the `dns-namecheap` module, certificates and the Caddyfile |
| [Credential Brokers](Credential%20Brokers.md) | what each broker mints, and how it is installed |
| [Rail Clients](Rail%20Clients.md) | the settings a seat's rail reads from its own `wake.env`, which are not plane settings |

---

## The plane

What an operator sets for the registry and the console. `PUBLIC_URL` and `AUTH_SECRET` are the only required settings. With those two and a LAN address, a plane runs with no domain, DNS or certificate: the API at `PUBLIC_URL`, the dashboard at `CONSOLE_PORT`, and each service on its own port from the route range.

| setting | default | what it does |
|---------|---------|--------------|
| `PUBLIC_URL` | (required) | Where people and agents reach this plane, e.g. `https://cloud.example.org`. Every URL the registry hands out (bootstrap scripts, helpers, skills, guides) starts with it. A LAN address such as `http://192.0.2.10:7778` works for a first run. |
| `AUTH_SECRET` | (required) | The console's iron-session secret, which encrypts the login cookie. Compose refuses to start without it. Generate one with `openssl rand -hex 32`; iron-session needs at least 32 characters. Changing it logs everyone out of the console. |
| `SESSION_COOKIE_SECURE` | — | `true` marks the console's login cookie Secure on every request. Unset, it is Secure when the browser reached the console over HTTPS, which behind the bundled Caddy is every request (Caddy sets `X-Forwarded-Proto`). Reached directly on `CONSOLE_PORT` over plain HTTP, as on a LAN first run, it isn't, or the browser would drop it and login would never stick. Set it on a plane that is only ever reached over HTTPS. |
| `SAMCLOUD_ENV` | `prod` | The environment's name. Anything other than `prod` namespaces what bootstrap installs on a device (`<env>-` prefixes), so one machine can be enrolled in two planes. The name `staging` also makes served helpers read `stg-token.hdr` and `stg-token` instead of `token.hdr` and `token`. Unset `SAMCLOUD_PLANES`, it is this plane's name for `--plane`. |
| `SAMCLOUD_DOMAIN` | — | Parent domain for service subdomains, e.g. `example.org` puts a service at `app.example.org`. Unset, services go under `PUBLIC_URL`'s host (`app.cloud.example.org`). When that host is an IP address, which can't carry subdomains, each service gets its own port from the route range instead, plain HTTP (`http://192.0.2.10:18101`). Also the domain the broker URLs default under, and the zone `scripts/ddns-update.sh` updates (from its own environment or `~/.config/samcloud/namecheap.env`). |
| `SAMCLOUD_PLANES` | — | Planes a helper's `--plane` can name, as `name=url,name=url` (e.g. `prod=https://cloud.example.org,lab=https://lab.example.org`). A trailing `/api/v1` is stripped. Unset, only this plane, under its `SAMCLOUD_ENV` name. Running a second instance is the operator's choice; this is how helpers name it. |
| `SAMCLOUD_REPO` | `OWNER/samcloud` | The code's repository as `owner/name`. Served skill pages name it where they tell an agent how to contribute. |
| `SAMCLOUD_ADMIN_SEAT` | the first admin user, else `admin` | The seat served skill pages tell an agent to file problems to (`sc-new ticket --assignee …`). Set it when that seat is not the user `POST /init` created. |
| `SAMCLOUD_MODULES` | — | Optional modules to switch on, comma-separated, or `none` for all off. The one module is `dns-namecheap`. Unset or empty, each module decides from its own credentials. See [DNS and TLS](DNS%20and%20TLS.md). |
| `PLANE_HOST` | `PUBLIC_URL`'s host | Host devices dial for their reverse tunnel and SSH aliases. Set it when devices reach the plane under another name than people do, e.g. `tunnel.example.org`. Its old name, `SLICE_HOST`, is still read when it is unset. |
| `PLANE_SSH_USER` | `samcloud` | The user devices log in as on the gateway's sshd, written into bootstrap scripts, helpers and SSH config. The sshd image creates `samcloud`; set it only if you change that image. Compose passes it; `deploy/.env.example` does not list it. Its old name, `SLICE_SSH_USER`, is still read when it is unset. |
| `SLICE_HOST` | — | The old name of `PLANE_HOST`, read when that is unset. |
| `SLICE_SSH_USER` | — | The old name of `PLANE_SSH_USER`, read when that is unset. |
| `SLICE_SSH_PORT` | — | The old name of `PLANE_SSH_PORT`, read when that is unset. |
| `GATEWAY_SSH_HOST` | `PLANE_HOST` | Public host in the `ssh` aliases and device guides the registry generates. Set it when the gateway's SSH is published under a different host than the tunnel endpoint. |
| `GATEWAY_SSH_PORT` | `PLANE_SSH_PORT` | Public port in those aliases and guides. Set it when only the sslh mux is reachable from outside, e.g. to the `SSLH_PORT` value. |
| `OPERATOR_TZ` | `UTC` | The operator's time zone, an IANA name such as `Europe/London`. The registry stamps "now" in this zone into `auth/verify`, user guides and served helpers, so agents reason about "morning" and "end of day" in the operator's time, and steps recurring tasks on its calendar. |
| `REGISTRY_FILE_MAX_MB` | `1024` | Largest upload the registry accepts, in MB, for any file type. *Not passed by compose.* |
| `REGISTRY_FILE_RESERVE_MB` | `2048` | Free space, in MB, an upload must leave on the data volume, which also holds `registry.db`, the Caddyfile and the gateway keys. An upload that would cut into it is refused. *Not passed by compose.* |
| `WSL_DISTRO_DEFAULT` | `Ubuntu` | WSL distribution name written into the Windows device helper scripts. *Not passed by compose.* |

---

## Ports and ranges

Compose publishes each container port on the host port named here, and the route range on Caddy. The tunnel and forward ranges are not published: reverse tunnels and service forwards terminate inside the sshd container, and the registry hands their ports out of these ranges. The defaults here are compose's; [Architecture](Architecture.md#port-map) shows the ports a plane published on 443 typically uses.

| setting | default | what it does |
|---------|---------|--------------|
| `REGISTRY_PORT` | `7778` | Host port for the registry API (container port 7777). |
| `CONSOLE_PORT` | `3010` | Host port for the console (container port 3000). |
| `HTTPS_PORT` | `8444` | Host port for Caddy's HTTPS (container port 443). |
| `HTTP_PORT` | `9080` | Host port for Caddy's HTTP (container port 80). |
| `SSH_PORT` | `2200` | Host port for the gateway's sshd (container port 22). Compose also passes it to the registry as `PLANE_SSH_PORT`, with the same default. |
| `SSLH_PORT` | `9443` | Host port for the sslh mux, which serves SSH and HTTPS on one port. Compose also passes it to the registry as `PUBLIC_HTTPS_PORT`. |
| `PLANE_SSH_PORT` | `2222` (compose: `SSH_PORT`, else `2200`) | The port devices dial for SSH and their tunnel, written into bootstrap scripts, helpers and guides. Set from `SSH_PORT` by compose. Its old name, `SLICE_SSH_PORT`, is still read when it is unset. |
| `PUBLIC_HTTPS_PORT` | `443` (compose: `SSLH_PORT`, else `9443`) | Port in the service URLs the registry builds. At `443` the URL carries no port; anything else is appended (`https://app.example.org:9443`). Set from `SSLH_PORT` by compose. |
| `TUNNEL_RANGE_START` | `10001` | First port of the device reverse-tunnel range. Compose passes it to the registry as `PORT_RANGE_TUNNEL_START`. |
| `TUNNEL_RANGE_END` | `10099` | Last port of that range. Compose passes it as `PORT_RANGE_TUNNEL_END`. |
| `FORWARD_RANGE_START` | `18001` | First port of the service-forward range, where the gateway exposes a service from a tunnelled device. Compose passes it as `PORT_RANGE_FORWARD_START`. |
| `FORWARD_RANGE_END` | `18099` | Last port of that range. Compose passes it as `PORT_RANGE_FORWARD_END`. |
| `ROUTE_RANGE_START` | `18101` | First port of the route range, used only when services go under an IP address (see `SAMCLOUD_DOMAIN`). Caddy publishes the range on the same host ports and serves each route on its port, plain HTTP. Compose passes it to the registry as `PORT_RANGE_ROUTE_START`. |
| `ROUTE_RANGE_END` | `18110` | Last port of that range, so ten services by default. Compose passes it as `PORT_RANGE_ROUTE_END`. |
| `PORT_RANGE_TUNNEL_START` | `10001` (compose: `TUNNEL_RANGE_START`) | The registry's port allocator reads the tunnel range from these. |
| `PORT_RANGE_TUNNEL_END` | `10099` (compose: `TUNNEL_RANGE_END`) | End of the allocator's tunnel range. |
| `PORT_RANGE_FORWARD_START` | `18001` (compose: `FORWARD_RANGE_START`) | Start of the allocator's forward range. |
| `PORT_RANGE_FORWARD_END` | `18099` (compose: `FORWARD_RANGE_END`) | End of the allocator's forward range. |
| `PORT_RANGE_ROUTE_START` | `18101` (compose: `ROUTE_RANGE_START`) | Start of the allocator's route range. |
| `PORT_RANGE_ROUTE_END` | `18110` (compose: `ROUTE_RANGE_END`) | End of the allocator's route range. |

---

## TLS and the proxy

The bundled Caddy terminates TLS for the console, the API and every service route. Its entrypoint writes a Caddyfile to the registry's data volume **on first boot only**; after that the registry appends routes to that file and Caddy reuses it. Changing `CONSOLE_DOMAIN`, `ACME_*`, `RUSTDESK_DOMAIN` or the Namecheap credentials later has no effect on Caddy until that Caddyfile is removed or edited. [DNS and TLS](DNS%20and%20TLS.md#the-caddyfile) describes the file and how to check it.

| setting | default | what it does |
|---------|---------|--------------|
| `CONSOLE_DOMAIN` | `:443` | Site address of the console and API block in the first-boot Caddyfile. An address with no host, such as `:443`, gets certificates from Caddy's own CA, issued on demand for whatever name or IP address the client asks for; clients that don't trust that CA see a certificate warning. A name such as `cloud.example.org` gets a public certificate. |
| `ACME_EMAIL` | — | Contact email for the ACME account, written into the first-boot Caddyfile. |
| `ACME_CA` | — | ACME directory URL, e.g. a CA's test directory while you try things out. Unset, Caddy uses its default CA. |
| `RUSTDESK_DOMAIN` | — | Adds a site block proxying `/ws/id` and `/ws/relay` to the RustDesk containers (`deploy/rustdesk-compose.yml`). Unset, no RustDesk block. |
| `CADDYFILE_PATH` | `/var/lib/caddy/Caddyfile` (compose: `/data/Caddyfile`) | The Caddyfile the registry appends routes to and reloads. Compose points it at the shared data volume Caddy reads. |
| `CADDY_ADMIN_HOST` | `localhost` (compose: `caddy`) | Host of Caddy's admin API, which the registry posts the Caddyfile to on every route change. |
| `CADDY_ADMIN_PORT` | `2019` (compose: `2019`) | Port of Caddy's admin API. |
| `CADDY_BIN` | `caddy` | The `caddy` binary the registry runs to validate a Caddyfile and to reload when the admin API fails. The registry image has no Caddy, so in compose these CLI paths fail and the admin API is what reloads. *Not passed by compose.* |

---

## Optional modules

### dns-namecheap

One DNS record per service subdomain, plus DNS-01 certificates in Caddy. See [DNS and TLS](DNS%20and%20TLS.md). The registry reads the credentials from the environment, else from `~/.config/samcloud/namecheap.env` inside its container. Unset `SAMCLOUD_MODULES`, the module is on when both are set.

| setting | default | what it does |
|---------|---------|--------------|
| `NAMECHEAP_API_KEY` | — | Namecheap API key. The registry uses it to add and remove service records; Caddy's first-boot Caddyfile uses it for DNS-01 certificates. |
| `NAMECHEAP_API_USER` | — | Namecheap API user, needed with the key. |

### Credential brokers

The brokers mint short-lived, scoped credentials for agents. They run on the host, outside compose, each as its own service (`uvicorn main:app`), so their settings go in that service's environment, not the compose env file. On a macOS host, `plists/install-broker.py gh|aws|google` renders the broker's LaunchAgent from `~/.config/samcloud/<name>-broker.env`: every key there becomes the agent's environment. Each verifies a caller's samcloud token against the plane before minting. On the registry side, compose passes the `*_BROKER_URL` settings so served helpers and skills point at the brokers; unset, each defaults to `https://<name>.<SAMCLOUD_DOMAIN>`. [Credential Brokers](Credential%20Brokers.md) says what each one mints and how agents call it.

| setting | default | what it does |
|---------|---------|--------------|
| `SAMCLOUD_API` | (required) | Every broker reads it: the plane's API base, e.g. `https://cloud.example.org/api/v1`, used to verify tokens (`/auth/verify`) and, for aws-broker and adb-broker, to post health. A broker exits at start without it. |

#### gh-broker

GitHub App installation tokens, scoped per repository.

| setting | default | what it does |
|---------|---------|--------------|
| `GH_BROKER_CONFIG` | `~/.config/samcloud/gh-broker` | Directory holding the registered Apps (`apps.json`), manifest state and repository aliases. |
| `GH_BROKER_MINTS` | `~/.local/samcloud/gh-broker/mints.jsonl` | Append-only audit log of every mint. |
| `GH_BROKER_PUBLIC_URL` | (required) | The broker's own public URL, written into the GitHub App manifest's homepage, webhook and redirect URLs, e.g. `https://ghb.example.org`. The broker exits at start without it. |
| `GH_BROKER_DEFAULT_PERMISSIONS` | — | Permissions a mint gets when the caller names none, e.g. `metadata:read`. Unset, GitHub returns the installation's full grant. Setting it narrows unqualified mints, which breaks callers that rely on the wide grant. |
| `GH_BROKER_URL` | `https://ghb.<SAMCLOUD_DOMAIN>` | Registry side: the gh-broker URL served helpers and skills name. |

#### aws-broker

AWS STS credentials through IAM Roles Anywhere. `aws-setup.sh` and `bootstrap-ca.sh` provision the AWS side and the certificates once, from an operator's shell.

| setting | default | what it does |
|---------|---------|--------------|
| `AWS_BROKER_CONFIG` | `~/.config/samcloud/aws-broker` | Directory holding `aws.json`, `samcloud.json` (the broker's service id and token) and the CA and leaf certificates. The setup scripts write here too. |
| `AWS_BROKER_MINTS` | `~/.local/samcloud/aws-broker/mints.jsonl` | Append-only audit log of every mint. |
| `AWS_BROKER_HEALTH_INTERVAL` | `30` | Seconds between the broker's health posts to the plane. |
| `AWS_PROFILE_NAME` | `default` | `aws-setup.sh` only: the local AWS CLI profile with admin rights it provisions with. |
| `AWS_REGION` | `us-east-1` | `aws-setup.sh` only: the region it creates the trust anchor and profile in. |
| `HOST` | this machine's short hostname, lowercased | `aws-setup.sh` and `bootstrap-ca.sh` only: the leaf certificate's name (`$HOST.crt`, `$HOST.key`, its CN), and the names of the broker role (`$HOST-aws-broker`) and trust anchor (`samcloud-$HOST`). Pass the same value to both scripts; re-running for an existing setup needs the name it was made with. |
| `AWS_BROKER_URL` | `https://awsb.<SAMCLOUD_DOMAIN>` | Registry side: the aws-broker URL served skills name. |

#### google-broker

Google OAuth access tokens, plus the slots that hand out Claude tokens to rails.

| setting | default | what it does |
|---------|---------|--------------|
| `GOOGLE_BROKER_CONFIG` | `~/.config/samcloud/google-broker` | Directory holding the OAuth client secret, registered accounts and refresh tokens, and Claude token slots. |
| `GOOGLE_BROKER_MINTS` | `~/.local/samcloud/google-broker/mints.jsonl` | Append-only audit log of every mint. |
| `GOOGLE_BROKER_PUBLIC_URL` | (required) | The broker's own public URL, used as the OAuth redirect (`/oauth/callback`) and in secret-submit links, e.g. `https://gglb.example.org`. The broker exits at start without it. |
| `GOOGLE_BROKER_URL` | `https://gglb.<SAMCLOUD_DOMAIN>` | Registry side: the google-broker URL served skills name. |
| `RAIL_TOKEN_MINT_URL` | `https://gglb.<SAMCLOUD_DOMAIN>/claude-token` | Registry side: where served rail helpers fetch their Claude token. |

#### adb-broker

Leased adb access to Android devices on a box shared by several seats. One per device-hosting box.

| setting | default | what it does |
|---------|---------|--------------|
| `ADB_BROKER_CONFIG` | `~/.config/samcloud/adb-broker` | Directory holding `config.json` and `samcloud.json` (the broker's service id and token). |
| `ADB_BROKER_STATE` | `~/.local/samcloud/adb-broker` | Directory for leases, the audit log and displaced-APK backups. |
| `ADB_BROKER_HEALTH_INTERVAL` | `30` | Seconds between the broker's health posts to the plane. |
| `ADB_BROKER_FAKE_AUTH` | — | Test hook: a file mapping tokens to identities, used instead of the plane. Outside pytest the broker refuses every request while it is set. Never set it in a service. |

---

## The deploy hook

`deploy/deploy-hook/receiver.py` turns a merged pull request into a release. It runs on the deploy host under systemd, outside compose, so a deploy cannot restart it. Its unit reads these from `~/.config/samcloud/deploy-hook.env` (`EnvironmentFile=`). `deploy/deploy-hook/install.sh` renders the unit from `samcloud-deploy-hook.service.in` for the user who runs it: their home, and `DEPLOY_HOOK_REPO_DIR`. [Deployment](Deployment.md#the-deploy-receiver) describes the receiver; it is optional.

| setting | default | what it does |
|---------|---------|--------------|
| `DEPLOY_HOOK_MERGERS` | (required) | Comma-separated GitHub logins whose merges deploy. The receiver exits at start when it is empty. |
| `DEPLOY_HOOK_REPO` | (required) | The `owner/name` a webhook must name, e.g. `OWNER/samcloud`; events for any other repository are ignored. The receiver and `install.sh` refuse to start without it. |
| `DEPLOY_HOOK_SECRET_FILE` | `~/.config/samcloud/deploy-hook.secret` | 0600 file holding the GitHub webhook secret the receiver checks signatures against. |
| `DEPLOY_HOOK_REPO_DIR` | `~/samcloud` | The deploy host's checkout, with `core.hooksPath` set to `.githooks`. The receiver fast-forwards it and `post-merge` releases. |
| `DEPLOY_HOOK_PORT` | `9911` | Port the receiver listens on, on all interfaces. Caddy proxies the webhook to it. |
| `DEPLOY_HOOK_LOG` | `~/logs/deploy-hook.log` | The receiver's log file. |
| `DEPLOY_HOOK_GITHUB_TOKEN_FILE` | `~/.config/samcloud/deploy-hook.github-token` | Optional 0600 file with a GitHub token (`contents` and `pull requests` write). Present, the receiver pushes each release tag and comments the result on the pull requests; absent, it only logs. `askpass.sh` reads it for git, so it is never on a command line. |
| `DEPLOY_HOOK_PLANE` | (none) | The plane URL a release comment names, e.g. `https://cloud.example.org`, so a reader can check `GET /api/v1/` for the version. Unset, the comment leaves that line out. |
| `DEPLOY_HOOK_PUBLIC_URL` | (none) | Where the receiver is reachable from outside (the route Caddy gives it), e.g. `https://hooks.example.org`. A failure comment points at its `/health`; unset, it says "the receiver's /health". |
| `PORT` | `9911` | `install.sh` only: a shell variable it sets from `DEPLOY_HOOK_PORT` in `deploy-hook.env`, to call the receiver's `/health` after a restart. Not a setting. |

---

## Background loops and timing

The registry runs these loops and thresholds itself. None is passed by compose; to change one, add it to the registry's `environment:` block.

**Agent connectivity health.** The plane alerts admins when an agent's connection goes offline or returns, or when repeated starts fail. The `DARK_*`, `RAIL_STREAM_DARK_*` and `WAKE_FAIL_DARK_THRESHOLD` settings below control those alerts, not how long an agent has to answer.

| setting | default | what it does |
|---------|---------|--------------|
| `FORWARD_RECONCILE_INTERVAL` | `120` | Seconds between passes that re-establish service forwards through the sshd container. Forwards die when sshd restarts and tunnels reconnect on their own schedule; this brings them back. |
| `DARK_WATCH_INTERVAL` | `300` | Seconds between agent connection-health checks; admins are notified when the result changes. The first pass waits 60 seconds after start. |
| `TASK_SCHEDULER_INTERVAL` | `30` | Seconds between passes that fire due task schedules into their assignees' inboxes. |
| `RAIL_STREAM_DARK_MIN` | `15` | Minutes without a live event stream before the agent is flagged as disconnected. |
| `RAIL_STREAM_DARK_BOOT_GRACE_S` | `300` | Seconds after the registry starts during which the stream check above is skipped, since every rail has to reconnect first. |
| `WAKE_FAIL_DARK_THRESHOLD` | `3` | Consecutive relevant launch failures before the agent is flagged for attention. Values below 1 become 1. |
| `WAKE_PROBE_IDLE_DAYS` | `3` | Days a connected rail may go without a launch attempt before the registry sends it a probe notification, which makes it launch once and prove it still can. Also the cooldown between probes. `0` or less turns probing off. |

---

## Device-side scripts

What the device scripts read on the device. `registry/bootstrap.sh` takes everything from values the registry fills in when it serves the script, so it reads no settings of its own. `registry/resource-bootstrap.sh` (resource discovery and a stats daemon) is copied into the registry image, but no route serves it; run by hand, with its placeholders unfilled, it reads these.

| setting | default | what it does |
|---------|---------|--------------|
| `SAMCLOUD_REGISTRY` | `http://localhost:7778` | The registry the script registers with and posts stats to. |
| `SAMCLOUD_DEVICE_ID` | `local-test` | The device id it registers resources under. |
| `SAMCLOUD_DEVICE_TOKEN` | (required) | The device's token. The script exits without one. |
| `SAMCLOUD_STATS_INTERVAL` | `15` | Seconds between stats posts from the installed daemon. |

---

## Set by compose or the image — not for operators

Internal wiring between the containers, build stamps, the ports of the test devices compose adds with `--profile sim`, and values the scripts set themselves. The default column is what compose or the image sets; the code's own fallback, where different, is the value outside compose. Changing one of these breaks the wiring the others assume.

| setting | default | what it does |
|---------|---------|--------------|
| `REGISTRY_URL` | `http://registry:7777` | How the console, the sshd container, the sim devices and the gateway reach the registry inside the compose network. The console bakes it at build time too. The registry also writes it into each access key's forced command, so the gateway gets it from there. Code fallback: `http://localhost:7777`. |
| `REGISTRY_DB_PATH` | `/data/registry.db` | The registry's SQLite database, on the `registry-data` volume. Code fallback: `/var/lib/samcloud/registry.db`. |
| `REGISTRY_FILE_ROOT` | `/data/files` | Where uploaded files are stored. Code fallback: `/var/lib/samcloud/files`. |
| `AUTHORIZED_KEYS_PATH` | `/data/authorized_keys` | The `authorized_keys` file the registry writes from access and device keys; the sshd container copies it every 5 seconds. Code fallback: `~/.ssh/authorized_keys`. |
| `GATEWAY_KEY` | `/data/keys/gateway` | The gateway's SSH private key. The registry generates it on first boot; the gateway binary uses it to reach devices. Code fallback: `/var/lib/samcloud/gateway`. |
| `GATEWAY_KEY_FORCECOMMAND` | `/etc/ssh/keys/gateway` | The path to that key as the sshd container sees it, written into each forced command. Code fallback: `GATEWAY_KEY`. |
| `GATEWAY_BINARY` | `/usr/local/bin/samcloud-gateway` | The gateway binary named in each forced command. |
| `GATEWAY_TOKEN_PATH` | `/data/keys/gateway_token` | Where the registry keeps the gateway's own API token, generated on first boot. Code fallback: `~/.local/samcloud/staging/keys/gateway_token`. |
| `GATEWAY_TOKEN` | the token in `GATEWAY_TOKEN_PATH` | Read by the gateway binary. The registry writes it into each access key's forced command; the gateway sends it as a bearer to see tunnel ports and SSH users. |
| `FORWARD_SSH_HOST` | `sshd` | Host the registry SSHes into to set up service forwards, where tunnels terminate. Code fallback: `localhost`. |
| `FORWARD_SSH_KEY` | `/data/keys/gateway` | Key for that SSH. Code fallback differs by file: `/data/keys/gateway` in `main.py`, `~/.ssh/samcloud` in `forward.py`. |
| `FORWARD_SSH_USER` | `samcloud` | User for that SSH. |
| `SSHD_HOST_KEYS_DIR` | `/sshd-hostpub` | Read-only mount of the sshd container's public host keys, so the registry can verify what it SSHes into and give devices the key to pin. Code fallback: `/sshd-keys`. |
| `SAMCLOUD_VERSION` | `dev` | The release version, passed as a build argument by `post-merge` on the deploy host. `GET /api/v1/` names it. |
| `GIT_COMMIT` | `unknown` | The commit the image is built from, passed as a build argument by `post-merge`. The registry image stores it as `SAMCLOUD_COMMIT`. |
| `SAMCLOUD_COMMIT` | `GIT_COMMIT` | Set by the registry image from `GIT_COMMIT`. `GET /api/v1/` names it. Code fallback: `unknown`. |
| `SIM_SSH_START` | `2201` | Host SSH port of the `sim-alpha` test device (`--profile sim`). |
| `SIM_HEALTH_START` | `9201` | Host health port of `sim-alpha`. |
| `SIM_SSH_BRAVO` | `2202` | Host SSH port of `sim-bravo`. |
| `SIM_HEALTH_BRAVO` | `9202` | Host health port of `sim-bravo`. |
| `SIM_SSH_CHARLIE` | `2203` | Host SSH port of `sim-charlie`. |
| `SIM_HEALTH_CHARLIE` | `9203` | Host health port of `sim-charlie`. |
| `SIM_SSH_DELTA` | `2204` | Host SSH port of `sim-delta`. |
| `SIM_HEALTH_DELTA` | `9204` | Host health port of `sim-delta`. |
| `REGISTRY` | `http://localhost:7778` | `deploy/seed.sh` only: the registry it seeds through the API. `--registry=<url>` overrides it. |
| `SEED_ADMIN_USER` | `admin` | `deploy/seed.sh` only: the admin user it creates and logs in as. |
| `SEED_ADMIN_PASS` | `admin123` | `deploy/seed.sh` only: that user's password. Seed a test plane only; never a plane anyone else can reach. |
| `ENV` | the plane's `SAMCLOUD_ENV` | `bootstrap.sh` only: a shell variable the script sets from the value the registry fills in. Not a setting. |
| `TMPSCRIPT` | — | `bootstrap.sh` only: a shell variable holding the temp copy the script makes of itself when piped from `curl`. Not a setting. |
| `PRESSURE` | — | `resource-bootstrap.sh` only: a shell variable the stats daemon sets from macOS `memory_pressure`, falling back to `50`. Not a setting. |

## Related

- [Deployment](Deployment.md): where the env file is used, and the build and deploy helpers
- [Architecture](Architecture.md): the containers, volumes and ports these settings wire together
- [Troubleshooting](Troubleshooting.md)

Next: [DNS and TLS](DNS%20and%20TLS.md)
