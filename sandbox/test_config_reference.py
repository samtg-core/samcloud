#!/usr/bin/env python3
"""Every setting the control plane reads is in docs/Configuration.md (ticket 1010).

An instance is described by its config, so the list of what can be configured
has to be complete. This gate derives the env vars the code reads (the
registry, gateway, console, brokers and the compose file) and fails on any the
reference doesn't name. It also fails on a reference row that names something
nothing reads, and on a key in deploy/.env.example the reference lacks. The
reference is a table per part; a row starts with the backticked name.

Run: python3 sandbox/test_config_reference.py
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REFERENCE = ROOT / "docs" / "Configuration.md"
EXAMPLE = ROOT / "deploy" / ".env.example"
results = []

# Where the control plane's code is, and how each language reads its env.
SOURCES = ("registry", "gateway", "console/src", "deploy", "gh-broker", "aws-broker", "google-broker", "adb-broker")
SKIP = re.compile(r"(^|/)(test_[^/]*|[^/]*_test\.go|conftest\.py)$|^registry/(helpers|skills)/|^deploy/\.env")
READS = [
    re.compile(r'os\.environ\.get\(\s*["\']([A-Z][A-Z0-9_]+)'),
    re.compile(r'os\.environ\[\s*["\']([A-Z][A-Z0-9_]+)'),
    re.compile(r'os\.getenv\(\s*["\']([A-Z][A-Z0-9_]+)'),
    re.compile(r'os\.Getenv\("([A-Z][A-Z0-9_]+)'),
    re.compile(r'process\.env\.([A-Z][A-Z0-9_]+)'),
    re.compile(r'\$\{([A-Z][A-Z0-9_]+)(?::?[-?=])'),
]
# Set by the OS or the runtime, never by an operator.
AMBIENT = {"HOME", "SHELL", "PATH", "USER", "TMPDIR", "SSH_ORIGINAL_COMMAND", "NODE_ENV"}


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  {'✓' if ok else '✗'} {name}" + ("" if ok else f"  [{detail}]"))


def read_by_code():
    files = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", *SOURCES],
                           capture_output=True, text=True).stdout.split("\0")
    found = {}
    for f in files:
        if not f or SKIP.search(f) or not f.endswith((".py", ".go", ".ts", ".tsx", ".yml", ".yaml", ".sh")):
            continue
        text = (ROOT / f).read_text(errors="replace")
        for rx in READS:
            for name in rx.findall(text):
                found.setdefault(name, set()).add(f)
    return {k: v for k, v in found.items() if k not in AMBIENT}


def referenced():
    names = set()
    for line in REFERENCE.read_text().splitlines():
        m = re.match(r"\|\s*`([A-Z][A-Z0-9_]+)`", line)
        if m:
            names.add(m.group(1))
    return names


def compose_parses():
    """The compose file parses with the example env. Nothing else reads it before a deploy does."""
    if not shutil.which("docker"):
        print("  - compose file not parsed: no docker on PATH")
        return
    r = subprocess.run(["docker", "compose", "-f", str(ROOT / "deploy/docker-compose.yml"),
                        "--env-file", str(EXAMPLE), "config", "-q"],
                       capture_output=True, text=True, env={**os.environ, "AUTH_SECRET": "x" * 32})
    if "is not a docker command" in r.stderr:
        print("  - compose file not parsed: docker has no compose plugin")
        return
    check("deploy/docker-compose.yml parses with deploy/.env.example", r.returncode == 0, r.stderr.strip()[:300])


def main():
    print("config reference")
    check("docs/Configuration.md exists", REFERENCE.exists())
    if not REFERENCE.exists():
        return 1
    code, ref = read_by_code(), referenced()
    missing = sorted(set(code) - ref)
    check(f"every setting the code reads is in the reference ({len(code)} read)", not missing,
          "; ".join(f"{n} ({sorted(code[n])[0]})" for n in missing[:12]))
    stale = sorted(ref - set(code))
    check("the reference names nothing the code no longer reads", not stale, ", ".join(stale[:12]))
    example = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", EXAMPLE.read_text(), re.M))
    undocumented = sorted(example - ref)
    check("every key in deploy/.env.example is in the reference", not undocumented, ", ".join(undocumented))
    compose_parses()
    if all(results):
        print(f"ALL PASSED: config reference ({len(results)} checks)")
        return 0
    print(f"FAILED: {results.count(False)} of {len(results)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
