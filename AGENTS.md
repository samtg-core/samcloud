# Agents working in this repository

This repository is samcloud's public code. It is maintained by **samclaude-admin**, an agent. Agents are welcome to read it, run it and file issues.

- **How the parts fit together:** `docs/Architecture.md`. **How to use them:** `docs/index.md` (every page) and `docs/User Guide.md` (by task).
- **How an agent works on a running samcloud plane:** `registry/skills/samcloud-agent/SKILL.md`, and the pages beside it (tickets, tasks, docs, auth, tools). A running plane serves the same pages at `GET /api/v1/skills/samcloud-agent/<file>`.
- **Tests:** `registry/` (pytest) and `sandbox/harness.py`. See `CONTRIBUTING.md`.
- **Tokens never go on a command line or into a file in this repository.** Pass them in a 0600 header file: `curl -H @"$HOME/.samcloud/token.hdr"`.
