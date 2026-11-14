# Credential Brokers

A broker swaps a samcloud token for a short-lived login to another system, so nothing long-lived sits on a seat. There are four: GitHub (`gh-broker`), AWS (`aws-broker`), Google (`google-broker`) and Android's `adb` (`adb-broker`). All are optional: a plane runs without any of them.

## How they work

Each broker is a small FastAPI service (`uvicorn main:app`) on a host, **outside** the compose stack, with its own directory in the repo (`gh-broker/`, `aws-broker/`, `google-broker/`, `adb-broker/`).

- **The caller's samcloud token is the only credential it takes.** Every mint sends the caller's bearer to the plane's `GET /auth/verify` (at `SAMCLOUD_API`). A token the plane rejects gets `401`. The answer's scopes decide what the caller may mint.
- **Each grant is gated by scope.** A GitHub App, a Google account, a Claude token slot or a stored secret carries an `allowed_scopes` list. The caller needs one of those scopes, or `*`. A grant with no list, or with `*`, is open to every verified caller.
- **Long-lived secrets never leave the broker's host.** App private keys, refresh tokens and the AWS leaf certificate stay in the broker's config directory. What a caller receives expires on its own.
- **Every mint is audited** to an append-only JSONL file: `~/.local/samcloud/<name>-broker/mints.jsonl` for gh, aws and google, and `~/.local/samcloud/adb-broker/audit.jsonl` for adb. Recent entries are at `GET /mints/recent` (gh, aws, google) or `GET /audit/recent` (adb): google's needs an admin token, the others any token the plane accepts.
- **A broker exits at start** without `SAMCLOUD_API`, and gh and google also without their own public URL.

| Broker | Port | Reached at, by default | Mints |
|---|---|---|---|
| gh-broker | 7781 | `https://ghb.<SAMCLOUD_DOMAIN>` | GitHub App installation tokens, scoped to repositories |
| aws-broker | 7782 | `https://awsb.<SAMCLOUD_DOMAIN>` | AWS STS credentials through IAM Roles Anywhere |
| google-broker | 7783 | `https://gglb.<SAMCLOUD_DOMAIN>` | Google OAuth access tokens; Claude tokens for the rail; stored secrets |
| adb-broker | 7791 | local to its box | leased, guarded `adb` commands on a box several seats share |

The registry names the broker URLs in the helpers and skills it serves. Unset, each is the name above under the service domain; `GH_BROKER_URL`, `AWS_BROKER_URL`, `GOOGLE_BROKER_URL` and `RAIL_TOKEN_MINT_URL` override them ([Configuration](Configuration.md#credential-brokers)). How a name reaches the broker's port, for example a route through the plane's Caddy, is the operator's setup.

## gh-broker

Mints GitHub App installation tokens for one or more registered Apps, one per organisation or trust boundary.

- `POST /token {"repos": ["owner/name"], "permissions": ["contents:write"]}` picks an App the caller's scopes allow and returns a token scoped to those repositories. `permissions` narrows the grant; with none, the token carries the installation's full grant (or `GH_BROKER_DEFAULT_PERMISSIONS`, when set), and the answer says so. An installation token can narrow what an App was installed with, never widen it.
- The answer also carries `git_author`: the caller's samcloud name at the App bot's address, so commits say who made them. `sc-pr author` applies it to a clone.
- Apps are registered through the broker itself: `GET /manifest/new` creates one from a GitHub App manifest, and `GET /install-url` and `GET /installations` show where it is installed. Registered Apps live in `GH_BROKER_CONFIG` (`apps.json`).

`sc-pr`, the served contribute tool, mints through this broker for each push, and never writes the token to disk ([Contributing](Contributing.md)).

## aws-broker

Mints AWS STS credentials through IAM Roles Anywhere. The broker authenticates to AWS with an X.509 leaf certificate issued by a root CA you manage, registered as a Roles Anywhere trust anchor. There are no long-lived AWS access keys on disk.

- `POST /token {"agent_scope": "…", "role": "…"}` returns credentials for a role key from `aws.json`. `agent_scope` (required) is the declared purpose, recorded in the audit. `session_policy` narrows the session with an inline IAM policy; `duration` is 900 to 43200 seconds, 3600 by default. `GET /docs` shows every field.
- `aws-broker/bootstrap-ca.sh` and `aws-broker/aws-setup.sh` provision the certificates and the AWS side once, from an operator's shell. Pass both the same `HOST`.
- It posts its health to the plane every `AWS_BROKER_HEALTH_INTERVAL` seconds, as a registered service whose id and token are in `samcloud.json`.

## google-broker

Three things, all scope-gated and audited:

- **Google access tokens.** `POST /mint {"account": "<key>", "scopes": ["calendar.readonly"]}` returns a one-hour access token **narrowed to the scopes asked for**. An account is enrolled once, at a consent screen only its owner can approve (`GET /oauth/start`, which redirects back to `GOOGLE_BROKER_PUBLIC_URL/oauth/callback`), and its refresh token never leaves the broker. A scope outside what was granted at consent is refused. Short names (`calendar`, `gmail.readonly` and others) expand to Google's full scope URLs; anything else passes through as written.

  **More than one Google Cloud project.** The broker holds named OAuth clients: `default` is `client_secret.json`, and any other is `clients/<name>.json`. `GET /oauth/start?client=<name>` asks for consent through that client, and the account it enrols refreshes through the same client from then on. An admin stores a client with `POST /oauth/client?name=<name>`. The body is the client JSON, or `{"client_id": "…", "secret_from": "<key>"}` when a person pasted the client secret into a one-time `/secrets/link`; the secret moves out of the secret store into the client record. `GET /health` lists the clients and each account's client.
- **Claude tokens for the rail.** `GET /claude-token` hands out a Claude Code OAuth token from a slot in `claude_tokens.json`. This is distribution, not minting: the broker is the one place the token is kept, revoked and audited, and a rail fetches it into the process environment at every start instead of storing a login on the seat. Served rail helpers call it at `RAIL_TOKEN_MINT_URL`.
- **Stored secrets.** An admin stores a secret with `POST /secrets`, or makes a one-time submit link with `POST /secrets/link` so a person can paste one in a browser. A caller in scope fetches it with `GET /secret?key=…`, into process environment only.

## adb-broker

Leased access to Android devices on a box several seats share. One broker per device-hosting box; seats never talk to the `adb` server directly.

- Every `adb` command goes through `POST /exec` under a lease, with a hard deadline, guarded verbs, an APK identity check on install, and an automatic backup of any APK an install displaces.
- `samadb` (`adb-broker/samadb`) is the client: a drop-in for `adb` that takes or renews a lease, queues with a visible position, runs the command and exits with its exit code. `samadb samadb:status` and the other `samadb:` commands manage the lease.
- Config is `ADB_BROKER_CONFIG/config.json`; leases, the audit and backups are in `ADB_BROKER_STATE`. `adb-broker/README.md` covers its two modes (fronting an existing `adb` server, or owning its own).
- It is run by hand, on `127.0.0.1:7791` in its README: `install-broker.py` does not install it.

## Installing a broker

On a macOS host, `plists/install-broker.py` renders and loads the broker's LaunchAgent:

```bash
plists/install-broker.py gh            # or aws, google; --port N to move it; --dry to print and change nothing
```

| what | where |
|---|---|
| settings | `~/.config/samcloud/<name>-broker.env`, `KEY=VALUE` lines. Every key becomes the agent's environment. `SAMCLOUD_API` is required, and so is `GH_BROKER_PUBLIC_URL` or `GOOGLE_BROKER_PUBLIC_URL` |
| the broker it runs | the installed copy at `~/.local/samcloud/<name>-broker/` (its `.venv` and `main.py`), not the checkout |
| the agent | `~/Library/LaunchAgents/com.samcloud.<name>-broker.plist`, mode 0600, listening on `0.0.0.0` at the broker's port |
| logs | `~/Library/Logs/samcloud/<name>-broker.log` and `.err` |

The script writes and reloads the agent only when the rendered file differs from the one installed. [Configuration](Configuration.md#credential-brokers) lists every setting each broker reads.

On another host, run `uvicorn main:app --host <addr> --port <port>` from the broker's directory under the service manager you use, with the same settings in its environment.

## How agents use them

An agent sends its own token and gets back a login that expires on its own. The served skill page [`auth.md`](../registry/skills/samcloud-agent/auth.md) is the agent-facing guide; in short:

| you need | call |
|---|---|
| a Claude login for a headless run | `GET <google-broker>/claude-token`, exported as `CLAUDE_CODE_OAUTH_TOKEN` for that run. The rail does this at every start |
| a GitHub token for one repository | `POST <gh-broker>/token {"repos": ["owner/name"]}` |
| AWS credentials for a role | `POST <aws-broker>/token {"agent_scope": "…", "role": "…"}` |
| a Google access token | `POST <google-broker>/mint {"account": "<key>", "scopes": ["…"]}` |
| `adb` on a shared box | `samadb <adb args>` |

```bash
curl -s -H @"$HOME/.samcloud/token.hdr" https://ghb.example.org/token \
  -H 'Content-Type: application/json' -d '{"repos": ["owner/name"], "permissions": ["contents:write"]}'
```

The bearer is always presented from the 0600 header file, never on a command line ([Identity and Access](Identity%20and%20Access.md)). Rotating the brokers' own secrets (App keys, refresh tokens, the AWS certificate, Claude tokens) is the operator's job.

## Related

- [Configuration](Configuration.md#credential-brokers): every broker setting
- [Identity and Access](Identity%20and%20Access.md): tokens and scopes, which decide what a broker grants
- [Rail Clients](Rail%20Clients.md): how the rail uses the Claude token
- [Security](Security.md)

Next: [Security](Security.md)
