# Skills

A **skill** is the agent-facing form of these docs: a set of short markdown pages an agent loads to learn how to work on samcloud from its token alone. The plane serves one bundle, `samcloud-agent`, and fills in its own URL, brokers, repository and admin seat when it serves each page. The rail's listener installs it on every seat and keeps it current. This page says what the bundle contains page by page, which docs page is each one's human-facing companion, and how the pages are installed, rendered and kept current.

The skill pages are the most exercised description of the primitives: agents act on them every day. These docs follow their framing and terms, and link to them where an agent-facing detail lives there.

## In the dashboard

Skills have no dashboard page. They are files: read them at `GET /api/v1/skills/samcloud-agent/<page>`, or in the repository under `registry/skills/samcloud-agent/`.

## What is served

| route | auth | what it returns |
|---|---|---|
| `GET /api/v1/skills` | none | every served skill: `name`, `dir`, `description`, `path` to its `SKILL.md`, and `chunks` (the other pages beside it) |
| `GET /api/v1/skills/install.sh` | none | the installer, with this plane's URL and the list of every page filled in |
| `GET /api/v1/skills/{name}/SKILL.md` | none | a skill's entry page, rendered |
| `GET /api/v1/skills/{name}/{file}.md` | none | one of its other pages, rendered. Markdown only, same directory only |

Skills are operational knowledge, not secrets, so they need no token. The source pages are `registry/skills/<name>/*.md`, copied into the registry image at build.

## The bundle, page by page

`SKILL.md` is the entry page an agent always loads. The pages beside it are read when the work needs them. Each page carries a version stamp on its first line (`<!-- SKILL_VERSION: x.y.z -->`).

| page | what it covers | human-facing companion |
|---|---|---|
| [`SKILL.md`](../registry/skills/samcloud-agent/SKILL.md) | what the plane, a seat and the rail are; how work reaches you (the thread model, cold wakes, the wrap-up); replying with `sc-say`; sibling conversations; non-Claude clients (native streaming, the batch adapter, polling); the index of the other pages | [Agent Rail](Agent%20Rail.md), [Rail Clients](Rail%20Clients.md), [Thread Lifecycle](Thread%20Lifecycle.md) |
| [`tickets.md`](../registry/skills/samcloud-agent/tickets.md) | filing a ticket, replying, taking, reassigning and resolving, finding tickets, watching without owning | [Tickets](Tickets.md), [Notifications and Subscriptions](Notifications%20and%20Subscriptions.md) |
| [`tasks.md`](../registry/skills/samcloud-agent/tasks.md) | projects and tasks, working a task, delegating one to an agent, hearing a project's events | [Tasks and Schedules](Tasks%20and%20Schedules.md), [Projects](Projects.md) |
| [`docs.md`](../registry/skills/samcloud-agent/docs.md) | writing a doc, revisions and `base_rev`, discussing, linking, finding, retiring and superseding | [Docs](Docs.md) |
| [`collab.md`](../registry/skills/samcloud-agent/collab.md) | asking someone, handing work over, sharing a record, replying well, disagreeing, finding who is around | [Agent Rail](Agent%20Rail.md), [Notifications and Subscriptions](Notifications%20and%20Subscriptions.md) |
| [`auth.md`](../registry/skills/samcloud-agent/auth.md) | the token and its header file, groups as scope, the scope check, minted logins from the brokers, the gateway key | [Identity and Access](Identity%20and%20Access.md), [Credential Brokers](Credential%20Brokers.md), [SSH Gateway](SSH%20Gateway.md) |
| [`tools.md`](../registry/skills/samcloud-agent/tools.md) | the API index, `sc-say` and `sc-edit`, the served helpers, native-client and adapter settings, cap notices, the skills themselves, machines and leases, threads and `sc-new`, files | [API Reference](API%20Reference.md), [Rail Clients](Rail%20Clients.md), [Rail Operations](Rail%20Operations.md), [SSH Gateway](SSH%20Gateway.md), [Compute Resources and Leases](Compute%20Resources%20and%20Leases.md), [Files](Files.md) |
| [`contributing.md`](../registry/skills/samcloud-agent/contributing.md) | changing samcloud's code from a seat: `sc-pr`, the checkout, one change, what a change carries, the rules | [Contributing](Contributing.md) |
| [`tone.md`](../registry/skills/samcloud-agent/tone.md) | the writing standard for skill pages and every text the plane puts in front of an agent | [Contributing](Contributing.md) |

## How a page is rendered

The source pages name no instance. They carry placeholders, and the plane replaces them each time it serves a page (`_render_skill` in `registry/main.py`):

| placeholder | becomes | from |
|---|---|---|
| `__API__` | `<plane URL>/api/v1` | `PUBLIC_URL`, else the request's own scheme and host |
| `__GH_BROKER__` | the GitHub broker's URL | `GH_BROKER_URL`, else `https://ghb.<service domain>` |
| `__GOOGLE_BROKER__` | the Google broker's URL | `GOOGLE_BROKER_URL`, else `https://gglb.<service domain>` |
| `__AWS_BROKER__` | the AWS broker's URL | `AWS_BROKER_URL`, else `https://awsb.<service domain>` |
| `__REPO__` | the code's repository, `owner/name` | `SAMCLOUD_REPO`, else `OWNER/samcloud` |
| `__ADMIN__` | the seat agents file problems to | `SAMCLOUD_ADMIN_SEAT`, else the plane's first admin user, else `admin` |

The service domain is `SAMCLOUD_DOMAIN`, else `PUBLIC_URL`'s host. The installer gets two more when it is served: `__REGISTRY_URL__` (the plane URL) and `__SKILL_FILES__` (every page of every served skill). Run from a checkout, those are still placeholders, and the installer needs `SAMCLOUD_API` to say which plane to install from.

So a plane's settings decide what its skills say: set `PUBLIC_URL`, `SAMCLOUD_REPO`, `SAMCLOUD_ADMIN_SEAT` and the broker URLs in [Configuration](Configuration.md), and every served page names them. `registry/test_skill_render.py` checks that no page is ever served with a placeholder left in it.

## Installing and keeping current

**On a seat with the rail**, nothing to do. On every connect, the listener:

- installs any served skill the seat lacks, entry page and other pages together;
- swaps any stamped page whose served stamp differs from the local one;
- installs a page the plane now serves that an existing skill directory lacks;
- removes a stamped page the plane no longer serves.

A page without a stamp is the seat's own and is never touched. The skills directory is `~/.claude/skills`, or `SKILLS_DIR` in `wake.env` ([Rail Clients](Rail%20Clients.md#the-listener)).

**Anywhere else**, run the installer:

```sh
curl -fsSL https://cloud.example.org/api/v1/skills/install.sh | bash
```

| setting | default | what it does |
|---|---|---|
| `SAMCLOUD_API` | the plane that served the script | the plane to install from; required when run from a checkout |
| `SAMCLOUD_SKILLS` | `samcloud-agent` | the skills to install, space-separated |
| `CLAUDE_SKILLS_DIR` | `~/.claude/skills` | where they go |

It is idempotent: re-run it to update. Run from a checkout, it copies the local pages instead of fetching them.

**A runtime that does not load Claude Code skills** reads the same pages over HTTP. The charter `wake-install` writes for a non-Claude runtime points it at `GET /api/v1/skills/samcloud-agent/SKILL.md`.

**Where agents are pointed at it.** An invite's briefing (`GET /api/v1/users/brief/{token}`), the next steps returned on activation, a device's guide (`GET /api/v1/devices/{id}/guide`) and `GET /api/v1/users/me/guide` all name the installer or `SKILL.md` as the agent's operating guide.

## Served helpers and the ground truth

The skills are one of several things the plane serves rendered and stamped. The helpers under `GET /api/v1/helpers/*` (the listener, the rail, the adapter, the pens, the installer) get the same treatment: the plane fills in its URL, header-file name, planes and broker URLs when it serves them, and each carries a version stamp the listener compares ([Agent Rail](Agent%20Rail.md)). The installer's ground-truth block is rendered the same way. Helpers need a token; skills do not.

**The repository's bytes and the served bytes differ by design.** To know what a seat runs, read it on the seat or fetch it from the plane, not from the repository.

## Changing a skill page

A behaviour change updates its skill page in the same commit as the code, its docs page, its test and its changelog entry, and raises the page's stamp on line 1; otherwise the listener does not deliver the new text to seats. The rule is in [`contributing.md`](../registry/skills/samcloud-agent/contributing.md) and [Contributing](Contributing.md). `tone.md` is the standard a reviewer checks a page against.

## The repository's own skills

A Claude Code session opened in a samcloud checkout also loads the skills in `.claude/skills/`:

| skill | what it is |
|---|---|
| `samcloud-agent` | a link to `registry/skills/samcloud-agent/`: the same served bundle, unrendered |
| `samcloud-administering` | the admin's command card: see the fleet, reach a seat, park one, issue a token |
| `samcloud-contributing` | changing samcloud's code and served files: the gates, the deploy train, the record a change carries |
| `samcloud-unity-work` | Unity and headset work on shared devices: lease the device, drive it, capture and report |

The plane does not serve these three. They are for people and agents working in the repository.

## Related

- [Agent Rail](Agent%20Rail.md): the rail that installs and refreshes the skills.
- [Rail Clients](Rail%20Clients.md): the charter `wake-install` writes, and `SKILLS_DIR`.
- [Configuration](Configuration.md): the settings the pages are rendered from.
- [Contributing](Contributing.md): changing a page.
- [API Reference](API%20Reference.md): the full HTTP surface.

Next: [Deployment](Deployment.md)
