---
name: samcloud-contributing
description: Changing samcloud's code and served files — the gates, the deploy train, and the record a change must carry.
---
<!-- SKILL_VERSION: 2.3.1 -->
# samcloud-contributing — changing the code

Work in your clone of the repo. The registry (the API) is Python in `registry/`, the console is Next.js in `console/`. Once per clone: `git config core.hooksPath .githooks`.

## Run the gates before you commit

```
(cd registry && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q)
python3 sandbox/harness.py
python3 sandbox/docs_coverage.py
python3 sandbox/test_gateway_build.py      when the change touches gateway/
```

All three green, then commit. The pre-commit hook runs them again, plus the TypeScript compiler and the sandbox suites.

`gateway/` is Go and the first three gates are Python, so they never compile it.
`sandbox/test_gateway_build.py` builds it the way `deploy/Dockerfile.sshd` does —
the `go` directive pinned to the image's toolchain, `CGO_ENABLED=0`, `GOOS=linux` —
then vets, tests, and checks it is still stdlib-only. It runs in the pre-commit
suite, so a gateway that does not compile cannot be committed.

## The deploy train

```
git checkout -b change/<name> main
# commit your change, with its CHANGELOG.md entry (date + what changed; no version)
git push -u origin change/<name>
gh pr create --fill && gh pr merge --merge
```

**A merged PR into `main` is the release.** GitHub sends the merge to the deploy host, which fast-forwards and runs `post-merge`: it assigns the next patch version from the host's release tags, builds the images as that version, recreates what changed since the previous release, and tags. Only a PR merged by an allowed merger deploys; a direct push to `main` never does. Check the result at the deploy receiver's `/health` (`last`: version and result; the receiver also comments it on the PR when the deploy host holds a GitHub token), then `GET /api/v1/` for the served version. Nobody merges into `main` locally, and nobody raises a version by hand: a new minor is a hand-made tag on `main`'s head on the deploy host. After a change to `deploy/deploy-hook/`, run its `install.sh` on the deploy host; the service runs an installed copy.

## Where things live

| thing | where |
|---|---|
| the API | `registry/main.py`; the `/api/v1/` index agents read is in the same file |
| served files (rail, listener, `sc-say`, `sc-new`, the `subscribe` helper, the `channel`) | `registry/helpers/` — each carries a version stamp in its first lines |
| served skills | `registry/skills/samcloud-agent/` — `<!-- SKILL_VERSION: x.y.z -->` on line 1 of each file |
| stack config | `deploy/docker-compose.yml`, `deploy/.env.production` |
| feature docs | `docs/` (start at `docs/index.md`) |

## Changing a served file or skill

1. Edit it.
2. Bump its version stamp — listeners fetch a file when the served stamp differs from theirs, and only then.
3. Ship on the train. Check what a seat (one agent identity on one machine) receives: `GET /api/v1/helpers/<name>` or `GET /api/v1/skills/samcloud-agent/<file>`.

## What a change carries

Every behaviour change updates four things in the same commit:

| record | where |
|---|---|
| the feature doc | `docs/<Feature>.md` — the mechanism as it now is |
| the API map | the `/api/v1/` index, when a route is added, removed or reshaped |
| the skill | the page that teaches the behaviour, with its stamp bumped |
| the changelog | `CHANGELOG.md`, a new entry at the top headed by the date and what changed; the version is assigned at deploy |

Docs describe how a thing works. Live state — which devices, services, users exist — is answered by the API (`GET /api/v1/devices`, `/services`, `/users`); link the call rather than copying its output into a doc.

## Writing for agents

Follow `registry/skills/samcloud-agent/tone.md`: commands first, present tense, one idea per paragraph, no history in guides.

## When something is wrong

Read this page again, then file a ticket: `POST /api/v1/tickets {summary, body, assignee}` with the command and its output.
