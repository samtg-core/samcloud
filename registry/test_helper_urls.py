"""Served helpers carry the instance's own planes and brokers (ticket 1010, doc 490 step 3).

`--plane` presets come from SAMCLOUD_PLANES (else just this plane under its
SAMCLOUD_ENV name), broker URLs from their env or the service domain, and
the staging header from SAMCLOUD_ENV. An instance shaped like the reference deployment (two
planes, a parent domain; example names here) renders the preset lines byte for
byte as they were hard-coded; a generic instance renders none of the
reference deployment's.
"""
import subprocess

import pytest
from fastapi.testclient import TestClient

REF_PROD = {"PUBLIC_URL": "https://cloud.example.net", "SAMCLOUD_ENV": "prod", "SAMCLOUD_DOMAIN": "example.net",
            "SAMCLOUD_PLANES": "prod=https://cloud.example.net,stg=https://stg.example.net:9443"}
REF_STG = {**REF_PROD, "PUBLIC_URL": "https://stg.example.net:9443", "SAMCLOUD_ENV": "staging"}
GENERIC = {"PUBLIC_URL": "https://cloud.example.test", "SAMCLOUD_ENV": "val"}
PLANE_HELPERS = ("say", "new", "edit", "lease")


def served(tmp_db, monkeypatch, env, names):
    import main
    for k in ("SAMCLOUD_PLANES", "SAMCLOUD_DOMAIN", "GH_BROKER_URL", "RAIL_TOKEN_MINT_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with TestClient(main.app) as c:
        tok = c.post("/api/v1/init", json={"username": "admin", "password": "t"}).json()["token"]
        return {n: c.get(f"/api/v1/helpers/{n}", headers={"Authorization": f"Bearer {tok}"}).text
                for n in names}


def test_reference_prod_renders_the_presets_it_always_had(tmp_db, monkeypatch):
    out = served(tmp_db, monkeypatch, REF_PROD, PLANE_HELPERS + ("pr", "wake-install"))
    for n in PLANE_HELPERS:
        assert '  prod) api="https://cloud.example.net/api/v1" ;;\n  stg)  api="https://stg.example.net:9443/api/v1" ;;\n' in out[n], n
        assert "__PLANE" not in out[n], n
    assert 'GH_BROKER_URL", "https://ghb.example.net")' in out["pr"]
    assert "${RAIL_TOKEN_MINT_URL:-https://gglb.example.net/claude-token}" in out["wake-install"]
    assert 'token.hdr" __API__' not in out["pr"] and "/.samcloud/token.hdr" in out["pr"]


def test_reference_staging_keeps_its_own_header(tmp_db, monkeypatch):
    out = served(tmp_db, monkeypatch, REF_STG, ("pr",))
    assert "/.samcloud/stg-token.hdr" in out["pr"]


def test_generic_instance_carries_nothing_of_the_reference(tmp_db, monkeypatch):
    out = served(tmp_db, monkeypatch, GENERIC, PLANE_HELPERS + ("pr", "wake-install"))
    for n, text in out.items():
        assert "example.net" not in text, n
    for n in PLANE_HELPERS:
        assert '  val) api="https://cloud.example.test/api/v1" ;;\n' in out[n], n
        assert "--plane must be val, got" in out[n], n
    assert 'GH_BROKER_URL", "https://ghb.cloud.example.test")' in out["pr"]
    assert "/.samcloud/token.hdr" in out["pr"]


@pytest.mark.parametrize("name", PLANE_HELPERS)
def test_served_helper_parses(tmp_db, monkeypatch, tmp_path, name):
    text = served(tmp_db, monkeypatch, GENERIC, (name,))[name]
    f = tmp_path / name
    f.write_text(text)
    assert subprocess.run(["bash", "-n", str(f)], capture_output=True).returncode == 0
