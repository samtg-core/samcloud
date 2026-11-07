<!-- SKILL_VERSION: 3.2.3 -->
# auth — your token, what it can see, and logins to other systems

Your token is who you are on the plane. A broker swaps it for a short-lived login to another system, so nothing long-lived sits on a seat (one agent identity on one machine).

## Your token

One token per seat, in two files that hold the same secret:

```
~/.samcloud/token        the raw token — read by tools that take a token file
~/.samcloud/token.hdr    one line: Authorization: Bearer <token> — read by curl with -H @
```

`chmod 600 ~/.samcloud/token ~/.samcloud/token.hdr`. Every call to the plane:

```
curl -s -H @"$HOME/.samcloud/token.hdr" __API__/auth/verify
```

The answer names you (`owner_id`, `username`), your token type (`agent`, `user`, `device` — a seat that manages a device — or `service`), your scopes, your `groups` with your role in each, and your `default_group`. `scopes: ["*"]` sees everything; `group:<name>` sees that group's devices, services, tickets and docs; `device:<id>` sees one device; `service:register` may only register services.

When a call answers `401`, the token in the file is not the one the plane knows: file a ticket to an admin; they run `POST /users/{id}/regenerate-token` and put the new token in both files.

## Groups — the group is the scope

```
GET /groups                    the groups you belong to
GET /groups/{id}               its purpose, its working doc, members with roles, devices, services, your_role — the roster
POST /groups                   {"id", "name", "description"} — make your own; you are its owner
POST /groups/{id}/members      {"user_id", "role"} — an owner adds people (viewer | editor | owner)
PATCH /groups/{id}             an owner sets the purpose and the working doc ({"doc_id"})
```

Confirm your scopes whenever you are asked to, or after anything about your access changes — expected against actual, no side effects:

```
curl -s -H @"$HOME/.samcloud/token.hdr" __API__/helpers/scope-check | python3 -
```

Every ticket, doc, project (and its tasks) and file is in exactly one group. What you can see is the union of your groups; what you can do in one is your role there:

| role | you can |
|---|---|
| viewer | read the group's objects; answer on them (a comment is a message, not content); use its services |
| editor | file tickets, write and revise docs, add and change tasks, change state, register devices and routes, assign and mention members |
| owner | everything an editor can, and add or remove members and set their role |

A write names its group with `{"group_scope": "<group id>"}`; a write that names none goes to your **default group** — the group you were added to first among those you can edit; `GET /auth/verify` names it as `default_group` and your guide says it in words. A ticket with an assignee goes to the first group you edit that they are in; an admin is a member of every group, so a ticket filed to the admin stays in your default. The admin's own unnamed writes go to `samcloud-contributors`, the group for work on samcloud itself. Nothing is unscoped, and nothing is cleared to a commons: a write that clears its group (`"group_scope": ""`) answers `422`. `core` is the plane's inner city — administration, the gateway — and has the fewest members. An object outside your groups does not exist to you: lists leave it out and a read answers `404`. A viewer who names their group on a create gets `403` with the role that is needed.

Addressing follows scope. An assignee must be a member of the object's group — assigning an outsider answers `422` with a note naming the group and the call an owner makes to add them. A mention of an outsider is inert: the write succeeds, they are not notified, and the response carries the same note in `notes`. A thread is not scoped — it may hold objects from several groups — but every notification row follows the object, so a member of a thread hears only what they can read.

Every call takes your token; nothing answers without one. A device is *yours to manage* — a fresh enrollment token, a port, a route for a service on it, its resources' stats and metadata — when your token carries `device:<id>` or you are an editor or owner of a group that holds the device; a viewer who is not its manager gets `403`. Enrolling a device (`POST /enroll`) takes the editor role too: it joins the groups you name, each one a group you edit, or else every group you edit. `GET /users/{id}` shows you a fellow member of any of your groups by name, role and the groups you share; `GET /users` answers the admin with everyone and anyone else with themselves alone — the roster of a group is `GET /groups/{id}`.

## Logins to other systems — minted, not stored

Ask a broker when you need one. Send your token; it returns a login that expires on its own.

| you need | call |
|---|---|
| a Claude login for a headless `claude -p` run | `curl -s -H @"$HOME/.samcloud/token.hdr" __GOOGLE_BROKER__/claude-token` answers `{"token": …}`; export it as `CLAUDE_CODE_OAUTH_TOKEN` for that run |
| a GitHub token scoped to one repository | `POST __GH_BROKER__/token {"repos": ["owner/name"]}` |
| AWS credentials for a role | `POST __AWS_BROKER__/token {"agent_scope": "…", "role": "…"}` — `GET __AWS_BROKER__/docs` shows every field |
| adb (Android's device shell) on a shared box | `samadb shell <cmd>` — takes a lease for you, with a queue, then runs adb (it ships with the adb broker, `adb-broker/samadb` in the samcloud repo) |
| a Google access token for an enrolled account | `POST __GOOGLE_BROKER__/mint {"account": "<key>", "scopes": ["calendar.readonly"]}` — 1 h, and **narrowed to the scopes you ask for** |

The rail mints your Claude login at every start; you do not need a stored Claude login on a seat.

**On GitHub, everyone acts through a shared App bot, and the name is yours.** Your scopes pick the App (`samcloud-agents-core[bot]` and its siblings, one per trust boundary). Each GitHub mint also answers `git_author`: your samcloud name at that bot's address, so commits say who they're from and GitHub links them to the bot. `sc-pr author` applies it to a clone.

**Ask the Google mint for the least you need, per call.** An account is enrolled once, at a consent screen only its owner can approve, and the refresh token never leaves the broker. `/mint` refuses anything outside that grant — `scopes not granted at consent` — and sends a narrowed token when you ask for a subset, so a job that only reads should ask only for the readonly scope even when the key holds more. Short names are sugar for the full URLs (`calendar`, `calendar.readonly`, `gmail.readonly`, `gmail.send`, `gmail.modify`, `analytics.edit`, `analytics.readonly`, `userinfo.email`); anything else is passed through verbatim, so an unknown **full URL works and a misspelled short name reaches Google as-is** and fails at the consent screen rather than here. `GET __GOOGLE_BROKER__/docs` lists the routes.

A scope that was never granted is not something an admin can add for you: it comes from the account owner's consent, so widening a key means its owner re-approving at `__GOOGLE_BROKER__/oauth/start`. Ask for a **new key** scoped to the job rather than for a wider grant on an existing one — a key is cheap and its `allowed_scopes` can be narrowed to the one group that needs it.

## The gateway key — reaching machines as yourself

Run the script `POST /access-keys/setup-script` returns: it writes your key and the `samcloud` host into `~/.ssh/config`. Then:

```
ssh samcloud "devices --json"            what your key can reach
```

`POST /access-keys` returns the private key on its own, once, if you would rather install it yourself.

A device with no samcloud token (a person's new laptop) gets its key from a pair link. `POST /access-keys/pair {"username": "…", "device_name": "…"}` answers a `pair_command`; run it on that device. It works once, for an hour. Same rule as a key: your own username, or anyone's if you are an admin.

## When something is wrong

Read this page again, then file a ticket: `sc-new ticket --summary "…" --assignee __ADMIN__` — include the call and the status code, never the token.
