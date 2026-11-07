# samcloud

samcloud connects computers so that people and agents can reach them, share what runs on them and work together, each within the access they are given. This repo is its code:
- the registry (FastAPI, `registry/`), the console (Next.js, `console/`) and the gateway (Go, `gateway/`);
- the stack (`deploy/`) and the credential brokers (`*-broker/`);
- the helpers and skills a plane serves (`registry/helpers/`, `registry/skills/`).

Read `AGENTS.md` first: it is the orientation for any agent in this repository.

## Load the skill for the job

- Changing code, docs or a served file: `Skill(samcloud-contributing)`. It has the gates, the deploy train and the records a change updates.
- Operating an instance (seats, rails, tokens): `Skill(samcloud-administering)`.
- Working on a plane (tickets, tasks, docs, `sc-say`): `Skill(samcloud-agent)`.

A loaded skill is a snapshot. Invoke it again after a compaction.

## Where the truth is

| question | read |
|---|---|
| how a part works | `docs/`, from `docs/index.md`. `docs/Architecture.md` opens with the one-screen map; `docs/Thread Lifecycle.md` owns the conversation model |
| what exists now (devices, services, users, resources) | the registry: `GET /api/v1/` lists the calls |
| how an instance is configured | `docs/Configuration.md` (every setting, its default and what it does); the instance's env file; its own deployment record, a doc on its plane |
| what is being worked on | open tickets and tasks on the plane |
| what changed | `CHANGELOG.md` and `git log` |

`python3 sandbox/docs_coverage.py` checks the docs against the code.

## The conversation model

<!-- model -->
**A thread is shared conversation context, not a ticket or an assignment.** The first thing a conversation makes starts its thread; everything it makes after joins it. Its origin is background, not everyone's current job. A conversation is on a thread or it is not; "held" is not a state. A thread is open while any ticket or task in it is open, or until a person closes it. Comments reach their object's responsible people, named recipients and subscribers. Commenting alone does not subscribe its writer. Routine record changes reach the responsible recipient and explicit update subscribers: a task’s assignee, a ticket’s assignee (otherwise its filer), or a doc’s owner. Group membership and thread presence subscribe nobody. A mention, an assignment or a reply on something you own, on a thread none of your conversations is on, is a cold wake: the rail starts a holder there, and a live session is never handed it.
<!-- /model -->

`docs/Thread Lifecycle.md` owns the states and the verbs; `docs/Agent Rail.md` is the mechanism.

## Ship a change

A pull request merged into `main` is the release: the deploy host's receiver fast-forwards, assigns the version, rebuilds what changed and tags. Run the gates first; the contributing skill lists them.

## Working rules

- Send the token in its header file: `curl -s -H @"$HOME/.samcloud/token.hdr" "$API/<path>"`. Never put a token on a command line or in a file in this repository.
- Reach devices through the gateway (`ssh samcloud "<device> -- <cmd>"`) and services through the API. Use `docker exec` only when the gateway or the registry is the thing that is broken.
- Add routes through the registry (`POST /api/v1/services`, `POST /api/v1/routes`). The live Caddyfile is on the `registry-data` volume.
- Give every service in `deploy/docker-compose.yml` the `x-logging` anchor. An uncapped container log fills the disk and stops every registry write.
- Nothing about one instance goes in this repo: no host names, addresses, domains or people. An instance's facts are its config and its plane's records.
