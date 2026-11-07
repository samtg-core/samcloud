"""Served helpers take their plane from the serving plane's config (#1010, C6).

The adapter defaulted to one instance's API, subscribe fell back to it, and the
rail named its plane by matching that instance's staging hostname. Each now
reads what the serving plane renders in, and an unrendered copy names no plane.
"""
import os
import subprocess
import sys
from pathlib import Path

from test_registry import client  # noqa: F401

HELPERS = Path(__file__).parent / "helpers"


def _load(path, home, api):
    """Import a rendered helper as a module and print its API and PLANE."""
    code = ("import importlib.util,sys; s=importlib.util.spec_from_file_location('h', sys.argv[1]); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(m.API, getattr(m, 'PLANE', '-'))")
    env = {k: v for k, v in os.environ.items() if k not in ("API", "PLANE", "SAMCLOUD_API")}
    env["HOME"] = str(home)
    if api:
        env["API"] = api
    r = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
    return r.stdout.split()


def _served(client, name, tmp_path):
    r = client.get(f"/api/v1/helpers/{name}")
    assert r.status_code == 200, r.text
    p = tmp_path / f"{name}.py"
    p.write_text(r.text)
    return p


def test_the_rail_names_its_plane_from_the_planes_map(client, tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://cloud.example.org")
    monkeypatch.setenv("SAMCLOUD_PLANES", "main=https://cloud.example.org,test=https://test.example.org:9443")
    rail = _served(client, "rail", tmp_path)
    assert _load(rail, tmp_path, "https://test.example.org:9443/api/v1") == ["https://test.example.org:9443/api/v1", "test"]
    assert _load(rail, tmp_path, None) == ["https://cloud.example.org/api/v1", "main"]
    # An API the map doesn't name is called by the serving plane's environment name.
    monkeypatch.setenv("SAMCLOUD_ENV", "lab")
    rail = _served(client, "rail", tmp_path)
    assert _load(rail, tmp_path, "https://elsewhere.example.net/api/v1")[1] == "lab"


def test_the_adapter_defaults_to_the_plane_that_served_it(client, tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "http://192.0.2.10:7778")
    adapter = _served(client, "adapter", tmp_path)
    assert _load(adapter, tmp_path, None)[0] == "http://192.0.2.10:7778/api/v1"


def test_an_unrendered_subscribe_names_no_plane(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "SAMCLOUD_API"}
    env["HOME"] = str(tmp_path)
    r = subprocess.run([sys.executable, str(HELPERS / "samcloud-subscribe.py")],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode != 0 and "set SAMCLOUD_API" in r.stderr

