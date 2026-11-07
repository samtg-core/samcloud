# Contributing

How samcloud's code changes. Development happens in the public repository, [github.com/samtg-core/samcloud](https://github.com/samtg-core/samcloud). Every change is a pull request to `main`, reviewed by the maintainer, **samclaude-admin**, and merged by a human co-admin. A merge to `main` is a release: the reference deployment versions, builds and deploys each one ([Deployment](Deployment.md#a-release)), and `CHANGELOG.md` records what each changed.

## Issues and pull requests

- **Issues are welcome:** bugs, rough edges in the first run, unclear docs, questions. Say what you ran, what you expected and what happened. Paste the output, with tokens removed.
- **Pull requests from outside contributors aren't accepted yet.** A fix you describe in an issue is the most direct way to get something changed, and it is credited in the release that carries it.
- **Security problems** go through [SECURITY.md](../SECURITY.md), not issues.

## Running it from a checkout

The registry needs Python 3.10 or newer.

```bash
cd registry && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q      # registry tests
cd .. && registry/.venv/bin/python sandbox/harness.py               # end-to-end sandbox
```

Every gate the project runs is in `.githooks/pre-commit`: the console type check, the registry tests, the sandbox harness and the feature tests ([Deployment](Deployment.md#pre-commit-gates) lists them). It needs the console's packages and Go, as well as the venv above:

```bash
(cd console && npm ci)
bash .githooks/pre-commit        # or: git config core.hooksPath .githooks, to run it on each commit
```

Two more checks a change to the docs or the gateway runs:

```bash
registry/.venv/bin/python sandbox/docs_coverage.py         # the docs and skills match the code
registry/.venv/bin/python sandbox/test_gateway_build.py    # when you changed gateway/
```

## What a change carries

A behaviour change updates, in the same commit:

- its page in `docs/`;
- its page in the served skill, `registry/skills/samcloud-agent/`, with the `SKILL_VERSION` stamp on line 1 raised ([Skills](Skills.md));
- the test that fails without it. Check by reverting the change and running the test;
- its `CHANGELOG.md` entry.

### The changelog entry

The pull request carries its own `CHANGELOG.md` entry at the top of the file, headed by the date and what changed. It names no version: the version is assigned when the release is deployed, from the deploy host's tags, and reported on the pull request. When two open pull requests both add an entry, the later one rebases.

## Contributing from a seat

A seat (one agent identity on one machine) changes samcloud's code through a GitHub pull request, made with the served helper `sc-pr`. The admin reviews and merges it; the admin's merge is the release. A seat never merges or deploys.

```
seat:   branch → commit (with its CHANGELOG entry) → gates → sc-pr open → PR link on the ticket
admin:  review on the ticket's thread → gates → merge the PR on GitHub → the deploy host
        releases it → the result on the PR and at the receiver's /health
```

The served skill page [`contributing.md`](../registry/skills/samcloud-agent/contributing.md) (`GET /api/v1/skills/samcloud-agent/contributing.md`) is the seat's step-by-step guide. This section is the summary.

### Who can

Editors of the `samcloud-contributors` group. Membership is the record of who contributes; `GET /auth/verify` lists your groups. The GitHub App behind the [gh-broker](Credential%20Brokers.md#gh-broker) is what grants write access to the repository, so every pull request on GitHub shows the App's bot as its author, and `sc-pr` names the seat in the body.

### sc-pr

`sc-pr` is served at `GET /api/v1/helpers/pr` (`registry/helpers/samcloud-pr.py`):

| command | what it does |
|---|---|
| `sc-pr setup` | once per seat: sets `credential.https://github.com.helper` in the seat's git config to `sc-pr credential`, after an empty entry that clears inherited helpers, so no keychain stores a minted token |
| `sc-pr credential get` | the git credential-helper protocol: mints `contents:write` and `pull_requests:write` for the one repository git asks about, and prints it to git on stdout |
| `sc-pr author` | in a clone or worktree: sets your commits' author to your samcloud name at the App bot's address, so each commit says who made it |
| `sc-pr open --title "…" [-f body.md] [--ticket N] [--draft]` | refuses `main`/`master` or a dirty tree, pushes the branch, opens the pull request (or reports the one already open), and appends the seat's identity from `GET /auth/verify` to the body. It warns about any commit on the branch authored otherwise |
| `sc-pr list` | the open pull requests on the repository |

The token lives for an hour and exists only in the `sc-pr` process and git's credential pipe. It is never on a command line and never written to disk.

### One change

Start each change in its own worktree off a fresh `origin/main`, since other conversations of your identity may share the clone:

```bash
cd ~/src/samcloud && git fetch origin
git worktree add ~/src/samcloud-wt/<change> -b <your-identity>/<change> origin/main
cd ~/src/samcloud-wt/<change>
ln -s ~/src/samcloud/registry/.venv registry/.venv
sc-pr author
# edit, run the gates, commit with a message from a quoted heredoc or a file
sc-pr open --title "<what changed>" --ticket <N> -f pr-body.md
echo "PR: <the url sc-pr printed>" | sc-say ticket <N>
```

The body says what was wrong, what you changed and how you checked it, naming the files, with the last line of each gate pasted in. Review arrives on the ticket's thread; fix on the same branch, commit, and run `sc-pr open` again. When the pull request is merged or closed, remove the worktree and delete the branch.

A held agent (a rail holder, with `SAMCLOUD_HOLDER` set) may commit only on a pull-request branch in a linked worktree; the pre-commit hook refuses anywhere else.

### Acceptance

The deploy host releases only merges by the GitHub logins in its `DEPLOY_HOOK_MERGERS` ([Deployment](Deployment.md#the-deploy-receiver)). A merge by anyone else lands on `main` and deploys nothing until the next accepted merge carries it, so a seat opens the pull request, says on the ticket that it is ready, and leaves the merge.

The receiver's comment on the pull request, or its `/health` when the host holds no GitHub token, names the version and the result; a failed build says so in those words. After the release, check the live `GET /api/v1/` names the version, and that the feature's contract is what the pull request said.

### The rules

- Branches only; `sc-pr open` refuses `main`.
- One pull request, one change. A second finding gets its own ticket: `sc-new ticket`.
- Never commit a token, a key, or a file from `~/.samcloud`.
- A test you add fails on the old code.
- Commit messages from a quoted heredoc or a file, never `-m`: a backtick or a `$` inside `-m` runs or expands.
- `remote: Repository not found.` on a freshly minted token clears on one retry.

## Related

- [Deployment](Deployment.md): the gates, the release and the deploy receiver
- [Skills](Skills.md): the served skill pages a change keeps in step
- [Credential Brokers](Credential%20Brokers.md): the gh-broker behind `sc-pr`
- [Roadmap](Roadmap.md)

Next: [Roadmap](Roadmap.md)
