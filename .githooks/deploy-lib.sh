#!/bin/bash
# Shared by post-commit / post-merge: decide which compose services actually
# need `up` for a given git range.
#
# v1 of the surgical deploy compared freshly-built image IDs against running
# containers — but image IDs are only stable while the BuildKit cache is warm;
# a cache-cold build mints new IDs for byte-identical inputs, which made every
# deploy look like "all five changed" and bounce sshd anyway (#312 tunnel
# drops). Git is the ground truth for "did this service's inputs change", so
# the decision is git-diff based and docker-cache-independent.
#
# A service is deployed when EITHER:
#   - files under its source paths changed in the range, OR
#   - its container isn't running (recovery: `up` on a changed-only list would
#     otherwise never resurrect a dead service).
#
# NOT covered (documented trade-off): edits to docker-compose.yml itself or
# env files — run `docker compose up -d` by hand for those.

_service_paths() {
    case "$1" in
        registry) echo "registry/ deploy/Dockerfile.registry" ;;
        console)  echo "console/ deploy/Dockerfile.console" ;;
        caddy)    echo "deploy/Dockerfile.caddy deploy/caddy-entrypoint.sh" ;;
        sshd)     echo "gateway/ deploy/Dockerfile.sshd deploy/sshd-entrypoint.sh" ;;
        sslh)     echo "deploy/Dockerfile.sslh deploy/sslh" ;;
    esac
}

# release_base <pre-merge-ref>  -> the last release tag reachable from it, or the ref itself
#
# A deploy diffs from the previous RELEASE, not from the previous main. Code can
# reach main without a VERSION change (a PR merged on GitHub and pulled, a
# fast-forward that ran ahead of its release commit); the next release builds it
# into every image, so its service has to be recreated too. Diffing from HEAD@{1}
# missed it: 0.12.83 sat built but not running, and 0.12.88's registry did the same.
release_base() {
    git describe --tags --abbrev=0 --match 'v[0-9]*' "$1" 2>/dev/null || echo "$1"
}

# next_version <ref>  -> the version a deploy of <ref> is, without the leading v
#
# A merge into main is a release, and nobody raises a file by hand (0.12.96): the
# version comes from this host's release tags. <ref>'s own tag when it has one (a
# re-run mints nothing new); otherwise the last release tag reachable from it with
# the patch raised by one; 0.0.1 when there is none. A new minor or major starts by
# tagging main's head by hand (`git tag -a v0.13.0 -m ...`): the next merge releases
# as 0.13.1.
next_version() {
    local exact last maj min pat
    if exact=$(git describe --tags --exact-match --match 'v[0-9]*' "$1" 2>/dev/null); then
        echo "${exact#v}"
        return
    fi
    if ! last=$(git describe --tags --abbrev=0 --match 'v[0-9]*' "$1" 2>/dev/null); then
        echo "0.0.1"
        return
    fi
    IFS=. read -r maj min pat <<< "${last#v}"
    case "$pat" in ''|*[!0-9]*) pat=0 ;; esac
    echo "${maj}.${min}.$((pat + 1))"
}

# merged_prs <git-range>  -> the pull requests merged in the range, "#98 #99"
#
# Read from main's first-parent subjects only: GitHub's merge commit says
# "Merge pull request #98 from ..." and a squash merge ends in "(#98)". A ticket
# number inside a subject ("Fix #975 scoping") is not a pull request.
merged_prs() {
    git log --first-parent --format=%s "$1" 2>/dev/null \
        | grep -oE '^Merge pull request #[0-9]+|\(#[0-9]+\)$' \
        | grep -oE '[0-9]+' | sort -un | sed 's/^/#/' | tr '\n' ' ' | sed 's/ $//'
}

# changed_services <compose-project> <git-range>  -> space-separated list
changed_services() {
    local proj="$1" range="$2" out="" svc p state
    local files
    files=$(git diff --name-only "$range" 2>/dev/null)
    for svc in registry console caddy sshd sslh; do
        for p in $(_service_paths "$svc"); do
            if printf '%s\n' "$files" | grep -q "^${p}"; then
                out="$out $svc"
                break
            fi
        done
        case " $out " in *" $svc "*) continue ;; esac
        state=$(docker inspect "${proj}-${svc}-1" --format '{{.State.Running}}' 2>/dev/null)
        [ "$state" = "true" ] || out="$out $svc"
    done
    echo "$out"
}
