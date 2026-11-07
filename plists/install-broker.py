#!/usr/bin/env python3
"""Render and load a credential broker's LaunchAgent on a macOS host.

    plists/install-broker.py gh|aws|google [--port N] [--dry]

The broker's settings come from ~/.config/samcloud/<name>-broker.env (KEY=VALUE
lines; docs/Configuration.md lists them). Every key there becomes the agent's
EnvironmentVariables, so the broker reads its plane and public URL from the env
file, not from a default. SAMCLOUD_API is required, and so is the gh and google
brokers' public URL.

The agent runs the broker installed at ~/.local/samcloud/<name>-broker (its .venv
and main.py) and logs to ~/Library/Logs/samcloud/. When the rendered agent differs
from the one in ~/Library/LaunchAgents, it is written and reloaded. --dry prints
the rendered agent and changes nothing.
"""
import argparse
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

PORTS = {"gh": 7781, "aws": 7782, "google": 7783}
REQUIRED = {"gh": ["GH_BROKER_PUBLIC_URL"], "aws": [], "google": ["GOOGLE_BROKER_PUBLIC_URL"]}


def read_env(path):
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def render(name, home, env, port):
    root = home / ".local/samcloud" / f"{name}-broker"
    logs = home / "Library/Logs/samcloud"
    return {
        "Label": f"com.samcloud.{name}-broker",
        "ProgramArguments": [str(root / ".venv/bin/uvicorn"), "main:app",
                             "--host", "0.0.0.0", "--port", str(port)],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(logs / f"{name}-broker.log"),
        "StandardErrorPath": str(logs / f"{name}-broker.err"),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render and load a broker's LaunchAgent.")
    ap.add_argument("name", choices=sorted(PORTS))
    ap.add_argument("--port", type=int)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    home = Path.home()
    envf = home / ".config/samcloud" / f"{a.name}-broker.env"
    if not envf.is_file():
        print(f"install-broker: {envf} is missing; docs/Configuration.md lists its settings", file=sys.stderr)
        return 2
    env = read_env(envf)
    missing = [k for k in ["SAMCLOUD_API", *REQUIRED[a.name]] if not env.get(k)]
    if missing:
        print(f"install-broker: set {', '.join(missing)} in {envf}", file=sys.stderr)
        return 2
    data = plistlib.dumps(render(a.name, home, env, a.port or PORTS[a.name]))
    if a.dry:
        sys.stdout.write(data.decode())
        return 0
    agent = home / "Library/LaunchAgents" / f"com.samcloud.{a.name}-broker.plist"
    (home / "Library/Logs/samcloud").mkdir(parents=True, exist_ok=True)
    if agent.is_file() and agent.read_bytes() == data:
        print(f"install-broker: {agent.name} is unchanged")
        return 0
    agent.parent.mkdir(parents=True, exist_ok=True)
    tmp = agent.with_suffix(".plist.tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o600)          # the env can carry settings an operator keeps private
    tmp.replace(agent)
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/com.samcloud.{a.name}-broker"
    subprocess.run(["launchctl", "bootout", service], capture_output=True)
    # bootout returns before the job is gone, and a bootstrap that lands first
    # fails with "5: Input/output error", leaving the broker down. Wait for it.
    for _ in range(40):
        if subprocess.run(["launchctl", "print", service], capture_output=True).returncode != 0:
            break
        time.sleep(0.25)
    for attempt in range(3):
        r = subprocess.run(["launchctl", "bootstrap", domain, str(agent)], capture_output=True, text=True)
        if r.returncode == 0:
            break
        time.sleep(1)
    if r.returncode != 0:
        print(f"install-broker: launchctl bootstrap failed: {r.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"install-broker: {agent.name} written and loaded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
