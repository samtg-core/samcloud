#!/usr/bin/env python3
"""plists/install-broker.py renders a broker's LaunchAgent from its env file (ticket #1010).

Runs the real script against a temp home with a stub `launchctl` on PATH. The
stub reproduces what the host showed on 30 Oct: `bootout` returns before the job is
gone, and a `bootstrap` that lands first fails with "5: Input/output error". The
installer must wait for the job to go, and the broker must end up loaded.
"""
import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "plists/install-broker.py"
results = []

STUB = """#!/bin/sh
# launchctl stand-in: the job stays visible for two `print`s after bootout.
log="$HOME/launchctl.log"; state="$HOME/launchctl.state"
echo "$*" >> "$log"
case "$1" in
  bootout)   echo 2 > "$state" ;;
  print)     n=$(cat "$state" 2>/dev/null || echo 0)
             if [ "$n" -gt 0 ]; then echo $((n - 1)) > "$state"; exit 0; fi
             exit 113 ;;
  bootstrap) n=$(cat "$state" 2>/dev/null || echo 0)
             if [ "$n" -gt 0 ]; then echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; fi
             exit 0 ;;
esac
"""


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  {'✓' if ok else '✗'} {name}" + ("" if ok else f"  [{detail}]"))


def run(home, *args):
    env = {**os.environ, "HOME": str(home), "PATH": f"{home}/bin:{os.environ['PATH']}"}
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env)


def main():
    home = Path(tempfile.mkdtemp(prefix="install-broker-"))
    (home / "bin").mkdir()
    (home / "bin/launchctl").write_text(STUB)
    (home / "bin/launchctl").chmod(0o755)
    cfg = home / ".config/samcloud"
    cfg.mkdir(parents=True)

    r = run(home, "gh")
    check("no env file: refused", r.returncode == 2 and "is missing" in r.stderr, (r.returncode, r.stderr))
    (cfg / "gh-broker.env").write_text("SAMCLOUD_API=https://cloud.example.org/api/v1\n")
    r = run(home, "gh")
    check("gh without its public URL: refused", r.returncode == 2 and "GH_BROKER_PUBLIC_URL" in r.stderr,
          (r.returncode, r.stderr))
    (cfg / "gh-broker.env").write_text('# comment\nSAMCLOUD_API=https://cloud.example.org/api/v1\n'
                                       'GH_BROKER_PUBLIC_URL="https://ghb.example.org"\n')

    r = run(home, "gh", "--dry")
    agent = plistlib.loads(r.stdout.encode()) if r.returncode == 0 else {}
    check("--dry renders the agent and touches nothing",
          r.returncode == 0 and not (home / "Library/LaunchAgents").exists(), (r.returncode, r.stderr))
    check("every env key is in the agent's environment, quotes stripped",
          agent.get("EnvironmentVariables") == {"SAMCLOUD_API": "https://cloud.example.org/api/v1",
                                                "GH_BROKER_PUBLIC_URL": "https://ghb.example.org"},
          agent.get("EnvironmentVariables"))
    check("the agent runs the installed broker on its port, under this home",
          agent.get("ProgramArguments") == [f"{home}/.local/samcloud/gh-broker/.venv/bin/uvicorn", "main:app",
                                            "--host", "0.0.0.0", "--port", "7781"]
          and agent.get("WorkingDirectory") == f"{home}/.local/samcloud/gh-broker", agent)

    r = run(home, "gh")
    calls = (home / "launchctl.log").read_text().splitlines()
    path = home / "Library/LaunchAgents/com.samcloud.gh-broker.plist"
    check("a real run loads the agent", r.returncode == 0 and "written and loaded" in r.stdout,
          (r.returncode, r.stdout, r.stderr))
    check("it waits for the bootout to finish before bootstrapping",
          [c.split()[0] for c in calls] == ["bootout", "print", "print", "print", "bootstrap"], calls)
    check("the agent is written 0600", path.exists() and (path.stat().st_mode & 0o777) == 0o600)
    r = run(home, "gh")
    check("an unchanged agent is left alone", r.returncode == 0 and "unchanged" in r.stdout
          and len((home / "launchctl.log").read_text().splitlines()) == len(calls), r.stdout)

    if all(results):
        print(f"ALL PASSED: install-broker ({len(results)} checks)")
        return 0
    print(f"FAILED: {results.count(False)} of {len(results)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
