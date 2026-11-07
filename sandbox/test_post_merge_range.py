#!/usr/bin/env python3
"""post-merge diffs from the last release, not from the last main.

Reproduces 2026-10-11: PR 77's commit reached main without a VERSION change, then
the 0.12.88 release commit (CHANGELOG + VERSION only) fast-forwarded on top. The
range HEAD@{1}..HEAD held no registry/ change, so the registry was built but not
recreated. `release_base` (deploy-lib.sh) must anchor the range at the previous
release tag. Also checks the host-config deploy target in post-merge.

Run: python3 sandbox/test_post_merge_range.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / ".githooks" / "deploy-lib.sh"
HOOK = ROOT / ".githooks" / "post-merge"
results = []


def isolated_env():
    local_vars = subprocess.run(["git", "rev-parse", "--local-env-vars"],
                                check=True, capture_output=True, text=True).stdout.splitlines()
    return {k: v for k, v in os.environ.items() if k not in local_vars}


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  {'✓' if ok else '✗'} {name}" + ("" if ok else f"  [{detail}]"))


def git(repo, *args):
    env = {**isolated_env(), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", repo, *args], env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


def commit(repo, path, text, msg):
    p = Path(repo) / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def lib(repo, expr):
    return subprocess.run(["bash", "-c", f'. "{LIB}"; {expr}'], cwd=repo,
                          env=isolated_env(), capture_output=True, text=True).stdout.strip()


def main():
    with tempfile.TemporaryDirectory() as repo:
        git(repo, "init", "-q", "-b", "main")
        commit(repo, "VERSION", "0.1.0\n", "0.1.0")
        git(repo, "tag", "-a", "v0.1.0", "-m", "Release v0.1.0")
        unbumped = commit(repo, "registry/helpers/wake-install.sh", "GT_VERSION=9\n", "PR merged, no bump")
        commit(repo, "VERSION", "0.1.1\n", "0.1.1")      # the release commit
        head = git(repo, "rev-parse", "HEAD")

        base = lib(repo, f"release_base {unbumped}")
        check("an untagged pre-merge main anchors at the last release tag", base == "v0.1.0", base)
        files = git(repo, "diff", "--name-only", f"{base}..{head}").splitlines()
        check("the release range includes code merged without a bump",
              "registry/helpers/wake-install.sh" in files, files)
        old = git(repo, "diff", "--name-only", f"{unbumped}..{head}").splitlines()
        check("(the old HEAD@{1} range missed it: the defect this guards)",
              "registry/helpers/wake-install.sh" not in old, old)

        tagged = git(repo, "rev-parse", "v0.1.0^{commit}")
        check("a tagged pre-merge main anchors at its own tag",
              lib(repo, f"release_base {tagged}") == "v0.1.0")

        # 0.12.96: a merge into main is a release; the version comes from the tags.
        check("the release after v0.1.0 is 0.1.1, raised by nobody",
              lib(repo, f"next_version {head}") == "0.1.1")
        git(repo, "tag", "-a", "v0.1.1", "-m", "Release v0.1.1")
        check("a head that is already a release keeps its version (a re-run mints nothing)",
              lib(repo, "next_version HEAD") == "0.1.1")
        commit(repo, "a", "a\n", "Merge pull request #12 from o/minor")
        git(repo, "tag", "-a", "v0.2.0", "-m", "a new minor, tagged by hand")
        commit(repo, "b", "b\n", "Merge pull request #13 from o/next")
        commit(repo, "c", "c\n", "Fix #975 scoping")
        commit(repo, "d", "d\n", "Squash-merged title (#14)")
        check("a hand-made minor tag starts the next series",
              lib(repo, "next_version HEAD") == "0.2.1")
        prs = lib(repo, "merged_prs v0.2.0..HEAD")
        check("the pull requests in a range come from merge subjects; a ticket number is not one",
              prs == "#13 #14", prs)

    with tempfile.TemporaryDirectory() as repo:
        git(repo, "init", "-q", "-b", "main")
        first = commit(repo, "VERSION", "0.1.0\n", "first")
        check("with no release tag the ref itself is the base",
              lib(repo, f"release_base {first}") == first)
        check("with no release tag the first release is 0.0.1",
              lib(repo, "next_version HEAD") == "0.0.1")

    hook = HOOK.read_text()
    check("post-merge ranges both stacks from release_base",
          'BASE=$(release_base "HEAD@{1}")' in hook
          and 'changed_services "$PROD_PROJECT" "${BASE}..HEAD"' in hook
          and 'changed_services "$STG_PROJECT" "${BASE}..HEAD"' in hook
          and 'changed_services samcloud-prod "HEAD@{1}..HEAD"' not in hook)
    check("post-merge reads its deploy target from host config, defaults to samcloud-prod",
          "$HOME/.config/samcloud/deploy.env" in hook
          and 'PROD_PROJECT="${PROD_PROJECT:-samcloud-prod}"' in hook
          and '-p "$PROD_PROJECT"' in hook)
    check("a failed tag is reported, not printed as a success",
          'if git tag -a "v${VERSION}"' in hook and "could not tag" in hook)
    check("post-merge releases every merge: the version from the tags, no file to raise",
          "VERSION_CHANGED" not in hook and "VERSION=$(next_version HEAD)" in hook
          and 'export SAMCLOUD_VERSION="$VERSION"' in hook)
    check("post-merge ends with the result line the receiver reads, on success and on failure",
          'echo "post-merge: result=$1 version=${VERSION} commit=${COMMIT} prs=' in hook
          and "finish build-failed" in hook and hook.rstrip().endswith("finish ok"))
    check("post-merge tags after the deploy, so a failed build leaves main untagged",
          hook.index('if git tag -a "v${VERSION}"') > hook.index("up -d --no-build --no-deps $PROD_CHANGED"))
    syntax = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True)
    check("post-merge parses", syntax.returncode == 0, syntax.stderr)

    if all(results):
        print(f"ALL PASSED: post-merge release range ({len(results)} checks)")
        return 0
    print(f"FAILED: {results.count(False)} of {len(results)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
