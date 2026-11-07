#!/usr/bin/env python3
"""Env-namespacing of device-side bootstrap artifacts (ticket #106).

The bug: re-enrolling a device against a second env (staging) clobbered the
first env's (prod) tunnel/token/key/ssh-config, because every device-side
artifact was single-namespace. Fix: prod keeps the BARE names (backward-compat,
no migration), any other env gets an <env>-scoped namespace, and re-enroll
cleanup is scoped to its own env only.

This test renders the real bootstrap.sh template, evaluates its env-namespacing
block in bash for prod / staging / un-injected, and asserts:
  - prod (and the un-replaced placeholder) → byte-identical bare names
  - staging → an isolated <env> namespace
  - the cleanup globs never reach across envs (the actual lockout cause)
No live registry needed — it's a pure template/logic check.
"""
import re
import subprocess
import sys
from pathlib import Path

SH = Path(__file__).resolve().parent.parent / "registry" / "bootstrap.sh"

GREEN, RED, RESET = "\033[0;32m", "\033[0;31m", "\033[0m"
passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        print(f"      {GREEN}✓{RESET} {name}")
        passed += 1
    else:
        print(f"      {RED}✗{RESET} {name}")
        failed += 1


def derive(env_value, device="desk-1"):
    """Render __ENV__ and evaluate the namespacing block; return the derived vars."""
    script = SH.read_text().replace("__ENV__", env_value)
    block = re.search(
        r"^# ── Env-namespacing.*?^TOKEN_FILE=.*?$",
        script,
        re.S | re.M,
    )
    assert block, "namespacing block not found in bootstrap.sh"
    body = block.group(0)
    probe = (
        body
        + '\nprintf "%s\\n" "$SSH_HOST" "$KEY" "$TOKEN_FILE" "$TUN_LABEL"'
        + ' "$TUN_GLOB" "$UNIT" "$UNIT_GLOB" "$CFG_SED"\n'
    )
    out = subprocess.run(
        ["bash", "-c", probe],
        env={"DEVICE": device, "HOME": "/tmp/h", "ENV": env_value, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    keys = ["SSH_HOST", "KEY", "TOKEN_FILE", "TUN_LABEL", "TUN_GLOB", "UNIT", "UNIT_GLOB", "CFG_SED"]
    return dict(zip(keys, out))


def glob_matches(pattern_prefix, label):
    """Mimic `case $label in ${prefix}*)` — does the cleanup glob catch this label?"""
    return label.startswith(pattern_prefix)


print("\n  Env-namespacing (#106)\n" + "-" * 60)

prod = derive("prod")
empty = derive("")               # SAMCLOUD_ENV="" → must fall back to bare (prod) names
staging = derive("staging")

# 1. prod = the exact legacy bare names (no migration for existing devices)
check("prod ssh host is bare 'samcloud-gw'", prod["SSH_HOST"] == "samcloud-gw")
check("prod key is bare samcloud_<device>", prod["KEY"] == "/tmp/h/.ssh/samcloud_desk-1")
check("prod token is bare ~/.config/samcloud/device_token",
      prod["TOKEN_FILE"] == "/tmp/h/.config/samcloud/device_token")
check("prod tunnel label is bare com.samcloud.tunnel-<device>",
      prod["TUN_LABEL"] == "com.samcloud.tunnel-desk-1")
check("prod unit is bare samcloud-tunnel-<device>", prod["UNIT"] == "samcloud-tunnel-desk-1")
check("prod cleanup sed unchanged", prod["CFG_SED"] == "# samcloud.*bootstrap")

# 2. empty env falls back to the bare prod namespace (-z guard)
check("empty ENV falls back to bare names", empty == prod)

# 3. staging is a distinct, isolated namespace
check("staging ssh host is 'staging-samcloud-gw'", staging["SSH_HOST"] == "staging-samcloud-gw")
check("staging key is env-scoped", staging["KEY"] == "/tmp/h/.ssh/samcloud_staging_desk-1")
check("staging token under per-env dir",
      staging["TOKEN_FILE"] == "/tmp/h/.config/samcloud/staging/device_token")
check("staging tunnel label is env-scoped",
      staging["TUN_LABEL"] == "com.samcloud.staging.tunnel-desk-1")
check("staging unit is env-scoped", staging["UNIT"] == "samcloud-staging-tunnel-desk-1")

# 4. THE lockout guard: neither env's cleanup glob may reach the other's artifacts
check("prod plist glob does NOT match staging label",
      not glob_matches(prod["TUN_GLOB"], staging["TUN_LABEL"]))
check("staging plist glob does NOT match prod label",
      not glob_matches(staging["TUN_GLOB"], prod["TUN_LABEL"]))
check("prod unit glob does NOT match staging unit",
      not glob_matches(prod["UNIT_GLOB"], staging["UNIT"]))
check("staging unit glob does NOT match prod unit",
      not glob_matches(staging["UNIT_GLOB"], prod["UNIT"]))
# and each glob DOES match its own (cleanup still works within an env)
check("prod glob matches its own label", glob_matches(prod["TUN_GLOB"], prod["TUN_LABEL"]))
check("staging glob matches its own label", glob_matches(staging["TUN_GLOB"], staging["TUN_LABEL"]))

print("-" * 60)
if failed:
    print(f"  {RED}{passed} passed, {failed} failed{RESET}")
    sys.exit(1)
print(f"  {GREEN}ALL PASSED: {passed} passed, 0 failed{RESET}")
