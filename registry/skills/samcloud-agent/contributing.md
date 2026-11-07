<!-- SKILL_VERSION: 2.4.1 -->
# contributing — change samcloud's code with a pull request

samcloud's code is `github.com/__REPO__`. You change it on a branch and open a pull request with `sc-pr`; the admin reviews and accepts it. The admin's merge is the release: the deploy host assigns the next version, builds and deploys it, and reports the result on the pull request. A merge by anyone else waits for the next such merge. You need editor membership of the `samcloud-contributors` group; `GET /auth/verify` lists your groups.

## Once per seat

```
mkdir -p ~/.local/bin && curl -s -H @"$HOME/.samcloud/token.hdr" __API__/helpers/pr > ~/.local/bin/sc-pr && chmod +x ~/.local/bin/sc-pr
sc-pr setup                                            git now asks sc-pr for GitHub credentials
git clone https://github.com/__REPO__.git ~/src/samcloud
python3.12 -m venv ~/src/samcloud/registry/.venv      any Python 3.10 or newer
~/src/samcloud/registry/.venv/bin/pip install -r ~/src/samcloud/registry/requirements.txt
```

`sc-pr` mints a GitHub token for each push, scoped to this repository, valid for an hour, never written to disk.

## Your checkout

The clone is yours to keep current; nothing syncs it for you, and your skills come from the plane, not from it. Other conversations of your identity may use the same clone, so start each change in its own worktree off a fresh `origin/main`:

```
cd ~/src/samcloud && git fetch origin
git worktree add ~/src/samcloud-wt/<change> -b <your-identity>/<change> origin/main
cd ~/src/samcloud-wt/<change>
ln -s ~/src/samcloud/registry/.venv registry/.venv
```

**Run `sc-pr author` once in each clone or worktree.** On GitHub everyone acts through the same App bot. `sc-pr author` sets your commits' author to your samcloud name at that bot's address, so each commit says who it's from and GitHub links it to the bot. Without it, commits carry the machine account's git config, which on a shared host is usually a person's. `sc-pr open` warns about any commit on the branch authored otherwise, and prints the rebase that fixes it.

`git worktree list` shows what is in use. When the pull request is accepted or closed, `git worktree remove ~/src/samcloud-wt/<change>` and delete the branch. Review that asks for a change while `main` has moved on: `git fetch origin && git rebase origin/main` in the worktree, then the gates again.

## One change

```
cd ~/src/samcloud-wt/<change>
# edit
(cd registry && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q)
registry/.venv/bin/python sandbox/harness.py
registry/.venv/bin/python sandbox/docs_coverage.py
registry/.venv/bin/python sandbox/test_gateway_build.py      when you changed gateway/
git add -A && git commit -F - <<'MSG'
<what changed and why>
MSG
sc-pr open --title "<what changed>" --ticket <N> -f pr-body.md
echo "PR: <the url sc-pr printed>" | sc-say ticket <N>
```

The registry needs Python 3.10 or newer. The venv lives at `registry/.venv`; the harness starts its sandbox with that interpreter. The gates pass before you open the pull request; paste the last line of each into the body. The body says what was wrong, what you changed, and how you checked it, naming the files. Review arrives on the ticket's thread; fix on the same branch, commit, and run `sc-pr open` again.

## What a change carries

A behaviour change updates, in the same commit: its feature doc in `docs/`, its page in `registry/skills/samcloud-agent/` with the stamp on line 1 raised, the test that fails without it, and its `CHANGELOG.md` entry at the top of the file, headed by the date and what changed. The entry names no version: the version is assigned at deploy and reported on the pull request. When another pull request's entry lands first, rebase.

Acceptance is the admin's interactive session, under one of the GitHub logins the deploy host accepts (its `DEPLOY_HOOK_MERGERS`): the deploy host processes only merges by those logins, and every one of them is a release — the next patch version from the host's tags, built, deployed, tagged, and told to the pull request. A merge made with an `sc-pr` token lands on `main` and deploys nothing until the next admin merge carries it, so open the pull request, say on the ticket that it is ready, and leave the merge — this holds for holders of the admin identity too. The comment on the pull request (or the deploy receiver's `/health`) names the version and result; verify the live `GET /api/v1/` release and feature contract.

## The rules

- Branches only; `sc-pr open` refuses `main`.
- One pull request, one change. A second finding gets its own ticket: `sc-new ticket`.
- Never commit a token, a key, or a file from `~/.samcloud`.
- A test you add fails on the old code; check by reverting the change and running it.
- Commit messages from a quoted heredoc or a file, never `-m`: a backtick or a `$` inside `-m` runs or expands.

## When something is wrong

`sc-pr` prints what failed and exits non-zero. Post the command and its output on the ticket. `remote: Repository not found.` on a fresh token clears on one retry.
