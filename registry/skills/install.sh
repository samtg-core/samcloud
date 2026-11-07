#!/usr/bin/env bash
# samcloud agent-skills installer.
#
# Installs the portable samcloud agent skill(s) into the calling agent's
# personal skills dir (~/.claude/skills/<name>/), so they're available across
# all of that agent's projects. Idempotent — re-run to update.
#
# Intended onboarding usage (served by the registry):
#   curl -fsSL "$SAMCLOUD_API/api/v1/skills/install.sh" | bash
#
# Or run from a samcloud checkout:
#   bash .claude/skills/samcloud-agent/install.sh
#
# Env:
#   SAMCLOUD_API        base URL of the registry (required when run from a checkout)
#   SAMCLOUD_SKILLS     space-separated skills to install (default: samcloud-agent)
#   CLAUDE_SKILLS_DIR   install target (default: $HOME/.claude/skills)
set -euo pipefail

# The registry fills in its URL and each skill's files when it serves this
# (GET /api/v1/skills/install.sh). Run straight from a checkout, they are still
# placeholders, and SAMCLOUD_API must say which plane to install from. The test
# is a pattern, not the placeholder itself: the registry replaces every copy of
# the placeholder, and a check written as one would always match (#1010, C1).
_DEFAULT_API="__REGISTRY_URL__"
_FILES="__SKILL_FILES__"
case "$_DEFAULT_API" in __*__) _DEFAULT_API="" ;; esac
case "$_FILES" in __*__) _FILES="" ;; esac
SAMCLOUD_API="${SAMCLOUD_API:-$_DEFAULT_API}"
if [ -z "$SAMCLOUD_API" ]; then
  echo "install.sh: set SAMCLOUD_API to your plane (e.g. https://cloud.example.org), or run the copy it serves" >&2
  exit 2
fi
SKILLS="${SAMCLOUD_SKILLS:-samcloud-agent}"
DEST="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"

# Source mode: if run from inside a checkout, copy local files; otherwise pull
# each skill's SKILL.md from the registry's skill-serving endpoint.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
LOCAL_SKILLS_ROOT=""
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/SKILL.md" ]; then
  LOCAL_SKILLS_ROOT="$(dirname "$SELF_DIR")"   # .../.claude/skills
fi

echo "samcloud agent-skills installer"
echo "  target: $DEST"
echo "  skills: $SKILLS"
mkdir -p "$DEST"

failed=0
for skill in $SKILLS; do
  out="$DEST/$skill"
  mkdir -p "$out"
  if [ -n "$LOCAL_SKILLS_ROOT" ] && [ -f "$LOCAL_SKILLS_ROOT/$skill/SKILL.md" ]; then
    cp "$LOCAL_SKILLS_ROOT/$skill/"*.md "$out/"
    echo "  ✓ $skill (from local checkout)"
    continue
  fi
  # The entry point and every page beside it, as the registry listed them.
  files="SKILL.md"
  for f in $_FILES; do
    case "$f" in "$skill/SKILL.md") ;; "$skill/"*) files="$files ${f#"$skill/"}" ;; esac
  done
  got=0
  for f in $files; do
    if curl -fsSL "$SAMCLOUD_API/api/v1/skills/$skill/$f" -o "$out/$f"; then
      got=$((got + 1))
    else
      echo "  ✗ $skill/$f — could not fetch from $SAMCLOUD_API" >&2
      failed=1
    fi
  done
  echo "  ✓ $skill: $got file(s) from $SAMCLOUD_API"
done

echo
echo "Done. Restart Claude Code (or it will pick up new skills on next start)."
echo "Verify with: /samcloud-agent  — or just ask anything that touches the registry."
exit "$failed"
