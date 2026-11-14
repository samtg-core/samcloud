"""Named OAuth clients for google-broker (#1246).

No network, no Google, no samcloud:  pytest test_named_clients.py -q

One broker holds clients from more than one Google Cloud project. An account
consents through a named client and every later mint refreshes through that
same client. A client's secret can arrive by a person's one-time secret link
and move into the client record without passing through anyone else.
Isolated the same way as test_scopes.py: config and mint log in a temp dir.
"""

import importlib.util
import os
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.mkdtemp(prefix="google-broker-clients-"))
os.environ["GOOGLE_BROKER_CONFIG"] = str(_TMP / "config")
os.environ["GOOGLE_BROKER_MINTS"] = str(_TMP / "mints/mints.jsonl")
os.environ.setdefault("SAMCLOUD_API", "https://cloud.example.org/api/v1")
os.environ.setdefault("GOOGLE_BROKER_PUBLIC_URL", "https://gglb.example.org")

_spec = importlib.util.spec_from_file_location(
    "google_broker_main_clients", Path(__file__).parent / "main.py")
main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(main)

ADMIN = {"username": "admin", "scopes": ["*"]}
AGENT = {"username": "claude-wafer", "scopes": ["group:sam-youtube"]}


@pytest.fixture
def broker(monkeypatch):
    who = {"id": ADMIN}

    async def fake_verify(authorization):
        return who["id"]

    monkeypatch.setattr(main, "verify_samcloud", fake_verify)
    for f in (main.CLIENT_FILE, main.SECRETS_FILE, main.ACCOUNTS_FILE, main.STATE_FILE):
        f.unlink(missing_ok=True)
    if main.CLIENTS_DIR.is_dir():
        for f in main.CLIENTS_DIR.iterdir():
            f.unlink()
    main._write_json(main.CLIENT_FILE, {"web": {"client_id": "family-id", "client_secret": "family-secret"}})
    c = TestClient(main.app)
    c.who = who
    return c


def test_a_secret_pasted_by_link_becomes_a_named_client(broker):
    main._write_json(main.SECRETS_FILE, {"oauth-client:pro": {"secret": "pro-secret", "allowed_scopes": ["admin"]}})
    r = broker.post("/oauth/client?name=pro", json={"client_id": "pro-id", "secret_from": "oauth-client:pro"},
                    headers={"Authorization": "Bearer x"})
    assert r.status_code == 200, r.text
    assert main.load_client("pro") == {"client_id": "pro-id", "client_secret": "pro-secret"}
    assert "oauth-client:pro" not in main._read_json(main.SECRETS_FILE, {}), "the secret must leave the store"
    assert main.load_client()["client_id"] == "family-id", "the default client is untouched"
    assert broker.get("/health").json()["clients"] == ["default", "pro"]


def test_only_the_admin_stores_a_client(broker):
    broker.who["id"] = AGENT
    r = broker.post("/oauth/client?name=pro", json={"client_id": "x", "client_secret": "y"},
                    headers={"Authorization": "Bearer x"})
    assert r.status_code == 403
    assert main.load_client("pro") is None


def test_a_bad_client_name_is_refused(broker):
    r = broker.post("/oauth/client?name=../evil", json={"client_id": "x", "client_secret": "y"},
                    headers={"Authorization": "Bearer x"})
    assert r.status_code == 400
    assert broker.get("/oauth/start?key=k&client=../evil", follow_redirects=False).status_code == 400


def test_consent_asks_through_the_named_client(broker):
    main._write_json(main.client_path("pro"), {"client_id": "pro-id", "client_secret": "pro-secret"})
    r = broker.get("/oauth/start?key=sam-youtube&scopes=x&allowed_scopes=group:sam-youtube&client=pro",
                   follow_redirects=False)
    assert r.status_code == 307
    assert parse_qs(urlparse(r.headers["location"]).query)["client_id"] == ["pro-id"]
    assert main._read_json(main.STATE_FILE, {})["client"] == "pro"
    r = broker.get("/oauth/start?key=assistant&scopes=x", follow_redirects=False)
    assert parse_qs(urlparse(r.headers["location"]).query)["client_id"] == ["family-id"]


def test_consent_through_an_unknown_client_is_refused(broker):
    assert broker.get("/oauth/start?key=k&client=nope", follow_redirects=False).status_code == 503


class _FakeHTTP:
    seen = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, data=None, **k):
        _FakeHTTP.seen.append(data)

        class R:
            status_code = 200

            def json(self):
                return {"access_token": "at", "expires_in": 3599, "scope": "s"}
        return R()


def test_a_mint_refreshes_through_the_account_s_own_client(broker, monkeypatch):
    main._write_json(main.client_path("pro"), {"client_id": "pro-id", "client_secret": "pro-secret"})
    main._write_json(main.ACCOUNTS_FILE, [
        {"key": "sam-youtube", "refresh_token": "rt-yt", "allowed_scopes": ["group:sam-youtube"],
         "google_scopes": ["https://www.googleapis.com/auth/youtube.readonly"], "client": "pro"},
        {"key": "assistant", "refresh_token": "rt-fam", "allowed_scopes": ["group:personal"],
         "google_scopes": ["https://www.googleapis.com/auth/calendar"]},
    ])
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeHTTP)
    _FakeHTTP.seen.clear()
    broker.who["id"] = AGENT
    r = broker.post("/mint", json={"account": "sam-youtube"}, headers={"Authorization": "Bearer x"})
    assert r.status_code == 200, r.text
    assert _FakeHTTP.seen[-1]["client_id"] == "pro-id" and _FakeHTTP.seen[-1]["refresh_token"] == "rt-yt"
    assert broker.post("/mint", json={"account": "assistant"},
                       headers={"Authorization": "Bearer x"}).status_code == 403, "group:sam-youtube is not group:personal"
    broker.who["id"] = ADMIN
    broker.post("/mint", json={"account": "assistant"}, headers={"Authorization": "Bearer x"})
    assert _FakeHTTP.seen[-1]["client_id"] == "family-id", "an account with no client stays on the default"


def test_a_mint_whose_client_is_gone_says_so(broker):
    main._write_json(main.ACCOUNTS_FILE, [
        {"key": "orphan", "refresh_token": "rt", "allowed_scopes": ["*"], "google_scopes": ["s"], "client": "gone"}])
    r = broker.post("/mint", json={"account": "orphan"}, headers={"Authorization": "Bearer x"})
    assert r.status_code == 503 and "gone" in r.text
