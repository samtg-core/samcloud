"""Served skill pages carry the instance's own plane, brokers and repo (ticket 1010).

The pages in registry/skills/ name no instance: they hold __API__, the broker
placeholders and __REPO__, and the plane fills them in when it serves a page.
An instance shaped like the reference deployment (a parent domain, its repo set; example names
here) gets its own URLs; a generic one gets none of the reference
deployment's, and no page is
ever served with a placeholder left in it.
"""
import re

from fastapi.testclient import TestClient

REF_LIKE = {"PUBLIC_URL": "https://cloud.example.net", "SAMCLOUD_ENV": "prod",
            "SAMCLOUD_DOMAIN": "example.net", "SAMCLOUD_REPO": "example-org/samcloud"}
GENERIC = {"PUBLIC_URL": "https://cloud.example.test", "SAMCLOUD_ENV": "val"}
ENV_KEYS = ("SAMCLOUD_DOMAIN", "SAMCLOUD_REPO", "SAMCLOUD_ADMIN_SEAT", "GH_BROKER_URL", "GOOGLE_BROKER_URL", "AWS_BROKER_URL")


def served(tmp_db, monkeypatch, env):
    import main
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with TestClient(main.app) as c:
        pages = {}
        for skill in c.get("/api/v1/skills").json()["skills"]:
            d = skill["dir"]
            pages[f"{d}/SKILL.md"] = c.get(f"/api/v1/skills/{d}/SKILL.md").text
            for chunk in skill["chunks"]:
                pages[f"{d}/{chunk}"] = c.get(f"/api/v1/skills/{d}/{chunk}").text
        return pages


def test_no_page_is_served_with_a_placeholder(tmp_db, monkeypatch):
    for env in (REF_LIKE, GENERIC):
        pages = served(tmp_db, monkeypatch, env)
        assert len(pages) > 5
        for name, text in pages.items():
            assert not re.search(r"__[A-Z_]+__", text), (name, re.findall(r"__[A-Z_]+__", text))


def test_reference_like_instance_gets_its_own_urls(tmp_db, monkeypatch):
    pages = served(tmp_db, monkeypatch, REF_LIKE)
    assert "Base URL: `https://cloud.example.net/api/v1`" in pages["samcloud-agent/SKILL.md"]
    assert "POST https://ghb.example.net/token" in pages["samcloud-agent/auth.md"]
    assert "https://gglb.example.net/claude-token" in pages["samcloud-agent/auth.md"]
    assert "POST https://awsb.example.net/token" in pages["samcloud-agent/auth.md"]
    assert "git clone https://github.com/example-org/samcloud.git" in pages["samcloud-agent/contributing.md"]


def test_generic_instance_carries_nothing_of_the_reference(tmp_db, monkeypatch):
    pages = served(tmp_db, monkeypatch, GENERIC)
    for name, text in pages.items():
        assert "example.net" not in text, name
    assert "Base URL: `https://cloud.example.test/api/v1`" in pages["samcloud-agent/SKILL.md"]
    assert "https://ghb.cloud.example.test/token" in pages["samcloud-agent/auth.md"]
    assert "github.com/OWNER/samcloud" in pages["samcloud-agent/contributing.md"]


STUB_CURL = """#!/bin/sh
# curl stand-in: serves pages the test fetched from the registry, logs each URL.
out=""; url=""
while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2 ;; -*) shift ;; *) url="$1"; shift ;; esac; done
echo "$url" >> "$STUB_LOG"
f="$STUB_WWW/${url#"$STUB_BASE"}"
[ -f "$f" ] || exit 22
cp "$f" "$out"
"""


def test_the_served_installer_runs_with_no_api_set_and_installs_every_page(tmp_db, monkeypatch, tmp_path):
    """#1010 C1: the served installer exited 2 for every plane, because the
    registry's replace also rewrote the check for an unrendered placeholder.
    Run the exact served bytes with no SAMCLOUD_API and an empty destination."""
    import os
    import subprocess
    import main
    for env in (REF_LIKE, GENERIC):
        for k in ENV_KEYS:
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        base = env["PUBLIC_URL"]
        www, dest = tmp_path / env["SAMCLOUD_ENV"] / "www", tmp_path / env["SAMCLOUD_ENV"] / "skills"
        with TestClient(main.app) as c:
            script = c.get("/api/v1/skills/install.sh").text
            listed = c.get("/api/v1/skills").json()["skills"]
            want = {}
            for skill in listed:
                for name in ["SKILL.md", *skill["chunks"]]:
                    page = c.get(f"/api/v1/skills/{skill['dir']}/{name}").text
                    want[f"{skill['dir']}/{name}"] = page
                    (www / "api/v1/skills" / skill["dir"]).mkdir(parents=True, exist_ok=True)
                    (www / "api/v1/skills" / skill["dir"] / name).write_text(page)
        assert "__" not in script.split("_FILES=")[1].split("\n")[0], "the file list was not rendered"
        bindir = tmp_path / env["SAMCLOUD_ENV"] / "bin"
        bindir.mkdir()
        (bindir / "curl").write_text(STUB_CURL)
        (bindir / "curl").chmod(0o755)
        run_env = {k: v for k, v in os.environ.items() if k != "SAMCLOUD_API"}
        run_env.update(PATH=f"{bindir}:{os.environ['PATH']}", CLAUDE_SKILLS_DIR=str(dest),
                       STUB_WWW=str(www), STUB_BASE=f"{base}/", STUB_LOG=str(tmp_path / env["SAMCLOUD_ENV"] / "curl.log"))
        r = subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True, env=run_env,
                           cwd=tmp_path)
        assert r.returncode == 0, (env["SAMCLOUD_ENV"], r.stdout, r.stderr)
        for path, page in want.items():
            if path.startswith("samcloud-agent/"):
                assert (dest / path).read_text() == page, path
        urls = (tmp_path / env["SAMCLOUD_ENV"] / "curl.log").read_text().split()
        assert urls and all(u.startswith(f"{base}/api/v1/skills/") for u in urls), urls


def test_a_checkout_copy_still_asks_for_the_plane(tmp_path):
    """Run from the repo, the placeholders are unrendered: it must ask, not guess."""
    import os
    import subprocess
    from pathlib import Path
    script = Path(__file__).parent / "skills" / "install.sh"
    env = {k: v for k, v in os.environ.items() if k != "SAMCLOUD_API"}
    env["CLAUDE_SKILLS_DIR"] = str(tmp_path / "skills")
    r = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
    assert r.returncode == 2 and "set SAMCLOUD_API" in r.stderr, (r.returncode, r.stderr)


def test_pages_name_this_planes_admin_seat(tmp_db, monkeypatch):
    """Skill pages used to tell every instance to file problems to one plane's
    admin seat by name (codex, #1010). They name this plane's."""
    pages = served(tmp_db, monkeypatch, GENERIC)
    assert "--assignee admin`" in pages["samcloud-agent/SKILL.md"]
    for name, text in pages.items():
        assert "samclaude-admin" not in text, name
        assert "claude-producer" not in text, name
    pages = served(tmp_db, monkeypatch, {**GENERIC, "SAMCLOUD_ADMIN_SEAT": "ops-seat"})
    assert "--assignee ops-seat`" in pages["samcloud-agent/SKILL.md"]
    assert "--assignee ops-seat`" in pages["samcloud-agent/tickets.md"]
