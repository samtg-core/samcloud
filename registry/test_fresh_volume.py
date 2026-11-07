"""A fresh registry volume can start the gateway (homehub test stack, 2026-10-11).

sshd mounts `authorized_keys` from the registry volume by subpath (#901), and a
subpath mount fails when the file is missing. The registry used to write it only
on the first access key, so a brand-new deploy brought up registry, console and
caddy, and sshd failed to start.
"""
from fastapi.testclient import TestClient

import authorized_keys


def test_startup_writes_missing_authorized_keys(tmp_db):
    import main
    path = authorized_keys.AUTHORIZED_KEYS_PATH        # per-test tmp path (conftest)
    assert not path.exists()
    with TestClient(main.app):
        pass
    assert path.exists()


def test_startup_keeps_operator_keys_and_rewrites_managed_lines(tmp_db):
    """Startup rewrites the managed lines (#136: an upgrade must not keep their old
    options), and keeps every line the operator wrote by hand."""
    import main
    path = authorized_keys.AUTHORIZED_KEYS_PATH
    path.write_text("ssh-ed25519 AAAAexisting operator-key\n"
                    "restrict,port-forwarding ssh-ed25519 AAAAstale # samcloud-managed\n")
    with TestClient(main.app):
        pass
    assert path.read_text().splitlines() == [
        "ssh-ed25519 AAAAexisting operator-key", authorized_keys.POLICY_LINE]
