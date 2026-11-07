"""Shared fixtures for registry tests."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# --- First-boot isolation, before anything imports `main` -------------------
#
# `main.lifespan` performs a machine's FIRST-BOOT SETUP: it generates the
# gateway ed25519 keypair (main.py:431-439) and mints a real `sc_device_`
# token (main.py:441-452). Every test runs it, because `with TestClient(app)`
# is what starts a lifespan and the `client` fixture has always used one.
# Both paths defaulted OUTSIDE the repo — `/var/lib/samcloud/gateway` and
# `~/.local/samcloud/staging/keys/gateway_token` — so `pytest` reached them:
# on a seat account the mkdir cannot succeed and 341 of 362 tests ERROR
# before they start (#853), and where the paths are writable it silently
# writes a keypair and a device token into somebody's home instead.
#
# `sandbox/harness.py` has set GATEWAY_KEY since it was written, so the
# second gate was isolated and the first never was. Same shape as
# isolate_authorized_keys below, which is the other half of the same defect.
#
# Set at IMPORT time rather than in a fixture: `main.GATEWAY_KEY_PATH`
# (main.py:9175) is a module constant read once when `main` is imported,
# which happens while pytest collects test_registry.py — already too late
# for any fixture, autouse or not.
#
# Unconditional rather than `setdefault`: an inherited GATEWAY_KEY pointing
# at the real path is exactly how this would come back unnoticed.
GATEWAY_TMPDIR = tempfile.mkdtemp(prefix="samcloud-tests-gateway-")
os.environ["GATEWAY_KEY"] = f"{GATEWAY_TMPDIR}/gateway"
os.environ["GATEWAY_TOKEN_PATH"] = f"{GATEWAY_TMPDIR}/gateway_token"

# Third path with the same shape (#889): `main.FILE_ROOT` defaults to
# /var/lib/samcloud/files and `main.blobs` is built from it at import, so a
# test that uploads anything writes blobs into the real store -- or, on a seat
# account where that mkdir cannot succeed, fails for a reason that has nothing
# to do with the test. There were no upload tests when the files plane landed,
# which is why this one went unnoticed until there were.
os.environ["REGISTRY_FILE_ROOT"] = f"{GATEWAY_TMPDIR}/files"

# The real paths as they stood before this session, so a test can assert the
# run left them alone. None means ABSENT, which is a third outcome and not a
# falsy one: where the key does not exist, "untouched" means it still does
# not exist afterwards — and a check that read None as "nothing to compare"
# would pass on exactly the machine where the bug creates files.
REAL_FIRST_BOOT_PATHS = (
    Path("/var/lib/samcloud/gateway"),
    Path.home() / ".local" / "samcloud" / "staging" / "keys" / "gateway_token",
)


def first_boot_path_state():
    """mtime_ns of each real first-boot path, None where the path is absent."""
    return {p: (p.stat().st_mtime_ns if p.exists() else None)
            for p in REAL_FIRST_BOOT_PATHS}


REAL_PATHS_BEFORE_TESTS = first_boot_path_state()

import authorized_keys as authorized_keys_mod
import db


@pytest.fixture(autouse=True)
def isolate_authorized_keys(tmp_path):
    """Keep the suite out of the real ~/.ssh/authorized_keys.

    `authorized_keys.AUTHORIZED_KEYS_PATH` defaults to `~/.ssh/authorized_keys`,
    and `POST /devices/activate` calls `authorized_keys.sync()`, which REWRITES
    that file — replacing every line it considers managed. `sandbox/harness.py`
    points the env var at its temp dir; the pytest suite never did, so any test
    that activates a device edited the SSH access-control file of whoever ran it.

    The module attribute is patched rather than the env var because the path is
    read once at import time, which has already happened by the time a fixture
    runs.
    """
    with patch.object(authorized_keys_mod, "AUTHORIZED_KEYS_PATH",
                      tmp_path / "authorized_keys"):
        yield


@pytest.fixture()
def tmp_db(tmp_path):
    """Create a temporary SQLite DB with schema applied, patching DB_PATH."""
    db_file = tmp_path / "test_registry.db"
    with patch.object(db, "DB_PATH", db_file):
        db.init_db()
        yield db_file


@pytest.fixture()
def seeded_db(tmp_path):
    """The old v1 seed is gone (archive/registry/seed_production.py). Tests that
    want state build it through the API; these wait to be migrated."""
    pytest.skip("seeded tests are being migrated to API-driven state")
