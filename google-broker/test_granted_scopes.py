"""What Google grants at consent is what the broker records and mints (#1154).

No network, no Google, no samcloud:  pytest test_granted_scopes.py -q

A Workspace consent screen has a box per scope. When one is left unticked, Google's
token response carries fewer scopes than the consent URL asked for. The broker used to
record the requested list, so `/mint` passed a full-URL request for the missing scope to
Google (bare `invalid_scope`), or handed back a token narrower than asked for.
Isolated the same way as test_scopes.py: config and mint log in a temp dir.
"""

import importlib.util
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.mkdtemp(prefix="google-broker-granted-"))
os.environ["GOOGLE_BROKER_CONFIG"] = str(_TMP / "config")
os.environ["GOOGLE_BROKER_MINTS"] = str(_TMP / "mints/mints.jsonl")
os.environ.setdefault("SAMCLOUD_API", "https://cloud.example.org/api/v1")
os.environ.setdefault("GOOGLE_BROKER_PUBLIC_URL", "https://gglb.example.org")

_spec = importlib.util.spec_from_file_location(
    "google_broker_main_granted", Path(__file__).parent / "main.py")
main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(main)

G = "https://www.googleapis.com/auth/"
DRIVE, EMAIL = G + "drive.file", G + "userinfo.email"
AUTH = {"Authorization": "Bearer x"}


class Google:
    """Stands in for Google's token and userinfo endpoints."""
    grant = ""            # what the code exchange says was granted
    refresh = None        # (status, body) for a refresh; default echoes the request or the grant
    calls = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, data=None, **k):
        Google.calls.append(dict(data))
        if data["grant_type"] == "authorization_code":
            status, body = 200, {"access_token": "at", "refresh_token": "rt", "scope": Google.grant}
        elif Google.refresh is not None:
            status, body = Google.refresh
        else:
            status, body = 200, {"access_token": "at2", "expires_in": 3599,
                                 "scope": data.get("scope") or Google.grant}
        return _Reply(status, body)

    async def get(self, url, **k):
        return _Reply(200, {"email": "sam@example.org"})


class _Reply:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


@pytest.fixture
def broker(monkeypatch):
    async def fake_verify(authorization):
        return {"username": "claude-producer", "scopes": ["group:work"]}

    monkeypatch.setattr(main, "verify_samcloud", fake_verify)
    monkeypatch.setattr(main.httpx, "AsyncClient", Google)
    for f in (main.ACCOUNTS_FILE, main.STATE_FILE):
        f.unlink(missing_ok=True)
    main._write_json(main.CLIENT_FILE, {"web": {"client_id": "id", "client_secret": "secret"}})
    Google.grant, Google.refresh, Google.calls = "", None, []
    return TestClient(main.app)


def consent(broker, granted):
    r = broker.get("/oauth/start?key=sam-drive&scopes=" + DRIVE + ",userinfo.email&allowed_scopes=group:work",
                   follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    Google.grant = granted
    return broker.get(f"/oauth/callback?code=c&state={state}")


def account():
    return next(a for a in main.load_accounts() if a["key"] == "sam-drive")


def test_an_unticked_box_is_recorded_and_said_at_consent(broker):
    r = consent(broker, EMAIL + " openid")
    assert r.status_code == 200
    assert "did not grant" in r.text and DRIVE in r.text
    assert account()["google_scopes"] == [EMAIL], "store what Google granted, not what was asked"
    logged = [json.loads(l) for l in main.MINTS_LOG.read_text().splitlines()][-1]
    assert logged["dropped_scopes"] == [DRIVE]


def test_a_full_grant_enrols_as_before(broker):
    r = consent(broker, f"{DRIVE} {EMAIL} openid")
    assert "Account enrolled: sam-drive" in r.text and "did not grant" not in r.text
    assert account()["google_scopes"] == sorted([DRIVE, EMAIL])


@pytest.mark.parametrize("scope", ["drive.file", DRIVE])
def test_an_ungranted_scope_is_refused_by_alias_or_full_url(broker, scope):
    consent(broker, EMAIL + " openid")
    Google.calls.clear()
    r = broker.post("/mint", json={"account": "sam-drive", "scopes": [scope]}, headers=AUTH)
    assert r.status_code == 403 and "not granted at consent" in r.text
    assert Google.calls == [], "refused here, not sent to Google"


def test_a_token_narrower_than_asked_is_never_handed_back(broker):
    """An account enrolled before this fix records the requested list."""
    main._write_json(main.ACCOUNTS_FILE, [{"key": "sam-drive", "refresh_token": "rt", "allowed_scopes": ["group:work"],
                                           "google_scopes": [DRIVE, EMAIL]}])
    Google.refresh = (200, {"access_token": "at2", "expires_in": 3599, "scope": EMAIL + " openid"})
    r = broker.post("/mint", json={"account": "sam-drive", "scopes": [DRIVE, "userinfo.email"]}, headers=AUTH)
    assert r.status_code == 403 and DRIVE in r.text and "at2" not in r.text


def test_google_s_invalid_scope_reads_as_not_granted(broker):
    main._write_json(main.ACCOUNTS_FILE, [{"key": "sam-drive", "refresh_token": "rt", "allowed_scopes": ["group:work"],
                                           "google_scopes": [DRIVE, EMAIL]}])
    Google.refresh = (400, {"error": "invalid_scope"})
    r = broker.post("/mint", json={"account": "sam-drive", "scopes": [DRIVE]}, headers=AUTH)
    assert r.status_code == 403 and "not granted at consent" in r.text


def test_a_granted_subset_still_mints(broker):
    consent(broker, f"{DRIVE} {EMAIL} openid")
    for scope in ("drive.file", DRIVE):
        r = broker.post("/mint", json={"account": "sam-drive", "scopes": [scope]}, headers=AUTH)
        assert r.status_code == 200 and r.json()["access_token"] == "at2", scope
    r = broker.post("/mint", json={"account": "sam-drive"}, headers=AUTH)
    assert r.status_code == 200, "no scopes asked: the whole grant, as Google returns it"
