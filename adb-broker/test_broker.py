"""
adb-broker test suite — runs against a fake adb + fake aapt + fake auth,
no device and no samcloud needed:  pytest test_broker.py -q

Covers the #538 acceptance claims:
  C1  leases are exclusive per device; second seat queues with a position
  C2  release/expiry promotes the queue head
  C3  guarded verbs (kill-server & co) are 403 for non-admin
  C4  every exec has a hard deadline — a hung command returns timed_out
  C5  wait-for-device is a bounded poll, never a raw unbounded wait
  C6  install: displaced APK is backed up first; backup failure aborts
  C7  uninstall without backup is admin-only (no backup, no displace)
  C8  canonical applicationId installs need a validation-grade lease
  C9  app_tag mismatch warns (M1) and lands in the response
  C10 bare logcat is forced to -d; transport flags before the verb are refused
  C11 exec under another holder's lease is 403
"""

import json
import os
import re
import stat
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).parent

FAKE_ADB = r"""#!/bin/bash
# fake adb for tests. State dir in $FAKE_STATE.
S="$FAKE_STATE"
args=("$@")
# strip -s <serial>
if [ "$1" = "-s" ]; then shift 2; fi
case "$1" in
  devices)
    echo "List of devices attached"
    echo "FAKESERIAL device usb:0-1 product:eureka model:Quest_3 device:eureka transport_id:1"
    ;;
  get-state)
    cat "$S/state" 2>/dev/null || echo device
    ;;
  shell)
    shift
    case "$1 $2 $3" in
      "pm list packages")
        if [ -f "$S/pm_fail" ]; then echo "error: device offline" >&2; exit 1; fi
        pkg="$4"
        if [ -f "$S/installed_$pkg" ]; then echo "package:$pkg"; fi
        ;;
      "pm path "*)
        pkg="$3"
        if [ -f "$S/installed_$pkg" ]; then echo "package:/data/app/$pkg/base.apk"; fi
        ;;
      "dumpsys package "*)
        echo "    versionName=1.2.3"
        ;;
      "sleep 999 ")
        sleep 999
        ;;
      *)
        echo "shell:$*"
        ;;
    esac
    ;;
  pull)
    if [ -f "$S/pull_fail" ]; then echo "adb: error: no such file" >&2; exit 1; fi
    echo fakeapkbytes > "$3"
    ;;
  install)
    echo "Success"
    ;;
  uninstall)
    echo "Success"
    ;;
  logcat)
    shift
    case "$1" in
      -d|-t)
        echo "logcat:$*"
        ;;
      *)
        while true; do echo "streamline $*"; sleep 0.1; done
        ;;
    esac
    ;;
  kill-server|start-server)
    ;;
  *)
    echo "fake-adb:$*"
    ;;
esac
exit 0
"""

FAKE_AAPT = r"""#!/bin/bash
# fake aapt: package id comes from the apk's file content marker.
apk="$3"
id=$(grep -o 'ID=[a-zA-Z0-9._-]*' "$apk" | head -1 | cut -d= -f2)
echo "package: name='${id:-com.fake.app}' versionCode='7' versionName='0.7.3-test-b1'"
"""


@pytest.fixture()
def fake_server_socket():
    """A live TCP listener standing in for the adb server socket — the
    broker probes it raw (never via adb, which would auto-start a server)."""
    import socket
    import threading
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)

    def drain():
        while True:
            try:
                conn, _ = s.accept()
                conn.close()
            except OSError:
                return

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    yield s.getsockname()[1]
    s.close()


@pytest.fixture()
def client(tmp_path, monkeypatch, fake_server_socket):
    state = tmp_path / "fake-state"
    state.mkdir()
    adb = tmp_path / "adb"
    adb.write_text(FAKE_ADB)
    adb.chmod(adb.stat().st_mode | stat.S_IEXEC)
    aapt = tmp_path / "aapt"
    aapt.write_text(FAKE_AAPT)
    aapt.chmod(aapt.stat().st_mode | stat.S_IEXEC)
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({
        "seat-a-token": {"username": None, "owner_id": "claude-studio", "role": "agent", "scopes": []},
        "seat-b-token": {"username": None, "owner_id": "studio-unity", "role": "agent", "scopes": []},
        "admin-token": {"username": "samclaude-admin", "role": "admin", "scopes": ["*"]},
    }))
    monkeypatch.setenv("FAKE_STATE", str(state))
    monkeypatch.setenv("ADB_BROKER_FAKE_AUTH", str(auth))
    monkeypatch.setenv("ADB_BROKER_CONFIG", str(tmp_path / "cfg"))
    monkeypatch.setenv("ADB_BROKER_STATE", str(tmp_path / "broker-state"))
    monkeypatch.setenv("SAMCLOUD_API", "https://cloud.example.org/api/v1")

    for mod in [m for m in list(sys.modules) if m == "main"]:
        del sys.modules[mod]
    sys.path.insert(0, str(HERE))
    import main
    main.CFG.update({
        "adb_server_socket": f"tcp:{fake_server_socket}",
        "adb_path": str(adb),
        "aapt_path": str(aapt),
        "backups_dir": str(tmp_path / "backups"),
        "staging_dir": str(tmp_path / "staging"),
        "captures_dir": str(tmp_path / "captures"),
        "canonical_ids": ["com.example.real"],
        "default_exec_timeout": 5,
        "max_exec_timeout": 10,
    })
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        c.fake_state = state
        c.tmp = tmp_path
        c.main = main
        yield c


def hdr(tok):
    return {"Authorization": f"Bearer {tok}"}


def lease(client, tok, **kw):
    body = {"purpose": "test", **kw}
    r = client.post("/leases", json=body, headers=hdr(tok))
    assert r.status_code == 200, r.text
    return r.json()


def make_apk(client, name, app_id):
    apk = client.tmp / name
    apk.write_text(f"ID={app_id}\n")
    return str(apk)


# ---------------------------------------------------------------- C1/C2: exclusivity + queue

def test_exclusive_lease_and_queue_promotion(client):
    a = lease(client, "seat-a-token")
    assert a["lease"]["status"] == "granted"
    assert a["lease"]["serial"] == "FAKESERIAL"
    b = lease(client, "seat-b-token")
    assert b["lease"]["status"] == "queued"
    assert b["position"] >= 1
    # release A -> B is promoted
    r = client.delete(f"/leases/{a['lease']['id']}", headers=hdr("seat-a-token"))
    assert r.status_code == 200
    r = client.get(f"/leases/{b['lease']['id']}", headers=hdr("seat-b-token"))
    assert r.json()["lease"]["status"] == "granted"


def test_expiry_promotes_queue(client):
    a = lease(client, "seat-a-token", ttl=1)
    b = lease(client, "seat-b-token")
    assert b["lease"]["status"] == "queued"
    time.sleep(1.2)
    r = client.get(f"/leases/{b['lease']['id']}", headers=hdr("seat-b-token"))
    assert r.json()["lease"]["status"] == "granted"
    r = client.get(f"/leases/{a['lease']['id']}", headers=hdr("seat-a-token"))
    assert r.json()["lease"]["status"] == "expired"


def test_lease_idempotent_per_holder(client):
    a1 = lease(client, "seat-a-token")
    a2 = lease(client, "seat-a-token")
    assert a1["lease"]["id"] == a2["lease"]["id"]


# ---------------------------------------------------------------- C3: guarded verbs

def test_guarded_verbs_403(client):
    a = lease(client, "seat-a-token")
    for verb in ("kill-server", "tcpip", "disconnect", "reconnect", "usb", "root"):
        r = client.post("/exec", json={"lease_id": a["lease"]["id"], "argv": [verb]},
                        headers=hdr("seat-a-token"))
        assert r.status_code == 403, f"{verb}: {r.text}"


def test_admin_server_restart(client):
    # F3: under M1a (own_server=false) restart would flip server ownership —
    # refused even for admin; it becomes legal only in deliberate M1b mode.
    r = client.post("/admin/server", json={"action": "restart"}, headers=hdr("admin-token"))
    assert r.status_code == 409
    assert "cutover" in r.json()["detail"]
    client.main.CFG["own_server"] = True
    try:
        r = client.post("/admin/server", json={"action": "restart"}, headers=hdr("admin-token"))
        assert r.status_code == 200
    finally:
        client.main.CFG["own_server"] = False
    r = client.post("/admin/server", json={"action": "restart"}, headers=hdr("seat-a-token"))
    assert r.status_code == 403


# ---------------------------------------------------------------- C4/C5: deadlines

def test_exec_deadline_kills_hung_command(client):
    a = lease(client, "seat-a-token")
    t0 = time.time()
    r = client.post("/exec", json={"lease_id": a["lease"]["id"],
                                   "argv": ["shell", "sleep", "999"], "timeout_s": 2},
                    headers=hdr("seat-a-token"))
    assert r.status_code == 200
    assert r.json()["timed_out"] is True
    assert r.json()["exit"] == 124
    assert time.time() - t0 < 10


def test_wait_for_device_bounded(client):
    a = lease(client, "seat-a-token")
    (client.fake_state / "state").write_text("offline")
    t0 = time.time()
    r = client.post("/exec", json={"lease_id": a["lease"]["id"],
                                   "argv": ["wait-for-device"], "timeout_s": 3},
                    headers=hdr("seat-a-token"))
    assert r.status_code == 504
    assert time.time() - t0 < 15
    (client.fake_state / "state").write_text("device")
    r = client.post("/exec", json={"lease_id": a["lease"]["id"],
                                   "argv": ["wait-for-device"], "timeout_s": 5},
                    headers=hdr("seat-a-token"))
    assert r.status_code == 200 and r.json()["exit"] == 0
    assert r.json()["stdout"] == ""  # real adb prints nothing on success


# ---------------------------------------------------------------- C6/C7: backup discipline

def test_install_backs_up_displaced_apk(client):
    (client.fake_state / "installed_com.fake.app").write_text("1")
    a = lease(client, "seat-a-token")
    apk = make_apk(client, "new.apk", "com.fake.app")
    r = client.post("/exec", json={"lease_id": a["lease"]["id"], "argv": ["install", "-r", apk]},
                    headers=hdr("seat-a-token"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["exit"] == 0
    assert body["backup"] and "com.fake.app" in body["backup"]
    bdir = Path(body["backup"])
    assert (bdir / "meta.json").exists()
    meta = json.loads((bdir / "meta.json").read_text())
    assert meta["versionName"] == "1.2.3"
    assert meta["apks"][0]["sha256"]


def test_install_backup_failure_aborts(client):
    (client.fake_state / "installed_com.fake.app").write_text("1")
    (client.fake_state / "pull_fail").write_text("1")
    a = lease(client, "seat-a-token")
    apk = make_apk(client, "new.apk", "com.fake.app")
    r = client.post("/exec", json={"lease_id": a["lease"]["id"], "argv": ["install", "-r", apk]},
                    headers=hdr("seat-a-token"))
    assert r.status_code == 409
    assert "no backup" in r.json()["detail"]


def test_uninstall_no_backup_admin_only(client):
    (client.fake_state / "installed_com.fake.app").write_text("1")
    a = lease(client, "seat-a-token")
    r = client.post("/exec", json={"lease_id": a["lease"]["id"],
                                   "argv": ["uninstall", "com.fake.app"], "no_backup": True},
                    headers=hdr("seat-a-token"))
    assert r.status_code == 403
    # with backup it succeeds
    r = client.post("/exec", json={"lease_id": a["lease"]["id"],
                                   "argv": ["uninstall", "com.fake.app"]},
                    headers=hdr("seat-a-token"))
    assert r.status_code == 200 and r.json()["backup"]


def test_backup_probe_failure_aborts(client):
    # F1: a failed installed-state probe must abort the displace, not read
    # as "not installed" and skip the backup.
    (client.fake_state / "installed_com.fake.app").write_text("1")
    (client.fake_state / "pm_fail").write_text("1")
    a = lease(client, "seat-a-token")
    r = client.post("/exec", json={"lease_id": a["lease"]["id"],
                                   "argv": ["uninstall", "com.fake.app"]},
                    headers=hdr("seat-a-token"))
    assert r.status_code == 409
    assert "cannot verify installed state" in r.json()["detail"]


def test_global_any_device_flags_refused(client):
    # F2: global -d/-e would make device targeting adb-version-dependent.
    a = lease(client, "seat-a-token")
    for flag in ("-d", "-e"):
        r = client.post("/exec", json={"lease_id": a["lease"]["id"],
                                       "argv": [flag, "shell", "ls"]},
                        headers=hdr("seat-a-token"))
        assert r.status_code == 400, flag


def test_fresh_install_needs_no_backup(client):
    a = lease(client, "seat-a-token")
    apk = make_apk(client, "fresh.apk", "com.fake.new")
    r = client.post("/exec", json={"lease_id": a["lease"]["id"], "argv": ["install", apk]},
                    headers=hdr("seat-a-token"))
    assert r.status_code == 200
    assert r.json()["backup"] is None


# ---------------------------------------------------------------- C8/C9: the device door

def test_canonical_id_needs_validation_lease(client):
    a = lease(client, "seat-a-token")
    apk = make_apk(client, "real.apk", "com.example.real")
    r = client.post("/exec", json={"lease_id": a["lease"]["id"], "argv": ["install", apk]},
                    headers=hdr("seat-a-token"))
    assert r.status_code == 403
    assert "canonical" in r.json()["detail"]


def test_canonical_lease_is_admin_gated_then_allows(client):
    r = client.post("/leases", json={"purpose": "t", "canonical": True},
                    headers=hdr("seat-a-token"))
    assert r.status_code == 403
    adm = lease(client, "admin-token", canonical=True)
    apk = make_apk(client, "real.apk", "com.example.real")
    r = client.post("/exec", json={"lease_id": adm["lease"]["id"], "argv": ["install", apk]},
                    headers=hdr("admin-token"))
    assert r.status_code == 200


def test_app_tag_mismatch_warns(client):
    a = lease(client, "seat-a-token", app_tag="pr406")
    apk = make_apk(client, "t.apk", "com.fake.app.gm")
    r = client.post("/exec", json={"lease_id": a["lease"]["id"], "argv": ["install", apk]},
                    headers=hdr("seat-a-token"))
    assert r.status_code == 200
    assert any("app_tag" in w for w in r.json()["warnings"])


# ---------------------------------------------------------------- C10: shaping

def test_bare_logcat_forced_to_dump(client):
    a = lease(client, "seat-a-token")
    r = client.post("/exec", json={"lease_id": a["lease"]["id"], "argv": ["logcat"]},
                    headers=hdr("seat-a-token"))
    assert r.status_code == 200
    assert "logcat:-d" in r.json()["stdout"]
    # logcat -t stays legal (post-verb -t is not the transport flag)
    r = client.post("/exec", json={"lease_id": a["lease"]["id"], "argv": ["logcat", "-t", "50"]},
                    headers=hdr("seat-a-token"))
    assert r.status_code == 200


def test_transport_flags_refused_before_verb(client):
    a = lease(client, "seat-a-token")
    r = client.post("/exec", json={"lease_id": a["lease"]["id"], "argv": ["-s", "OTHER", "shell", "ls"]},
                    headers=hdr("seat-a-token"))
    assert r.status_code == 400


def test_interactive_shell_refused(client):
    a = lease(client, "seat-a-token")
    r = client.post("/exec", json={"lease_id": a["lease"]["id"], "argv": ["shell"]},
                    headers=hdr("seat-a-token"))
    assert r.status_code == 400


# ---------------------------------------------------------------- C11: holder checks

def test_exec_wrong_holder_403(client):
    a = lease(client, "seat-a-token")
    r = client.post("/exec", json={"lease_id": a["lease"]["id"], "argv": ["shell", "ls"]},
                    headers=hdr("seat-b-token"))
    assert r.status_code == 403


def test_queued_lease_is_persisted(client):
    # Regression: try_grants() early-returned before persist_state() when the
    # device was busy, so queued leases vanished on broker restart.
    a = lease(client, "seat-a-token")
    b = lease(client, "seat-b-token")
    assert b["lease"]["status"] == "queued"
    state = json.loads((Path(os.environ["ADB_BROKER_STATE"]) / "state.json").read_text())
    ids = {l["id"] for l in state["leases"]}
    assert a["lease"]["id"] in ids and b["lease"]["id"] in ids


# ---------------------------------------------------------------- C12: streaming captures

def test_logcat_capture_lease_bound(client):
    a = lease(client, "seat-a-token")
    r = client.post("/logcat/start", json={"lease_id": a["lease"]["id"], "argv": ["-v", "time"]},
                    headers=hdr("seat-a-token"))
    assert r.status_code == 200, r.text
    cap = r.json()["capture"]
    time.sleep(0.5)
    f = Path(cap["file"])
    assert f.exists() and f.stat().st_size > 0  # stream is landing
    # second start on the same lease is idempotent
    r = client.post("/logcat/start", json={"lease_id": a["lease"]["id"]},
                    headers=hdr("seat-a-token"))
    assert r.json().get("note") == "already running"
    # another seat cannot stop it
    r = client.post(f"/logcat/{cap['id']}/stop", headers=hdr("seat-b-token"))
    assert r.status_code == 403
    # releasing the lease auto-stops the capture on the next sweep
    client.delete(f"/leases/{a['lease']['id']}", headers=hdr("seat-a-token"))
    client.get(f"/leases/{a['lease']['id']}", headers=hdr("seat-a-token"))  # triggers sweep
    r = client.get("/logcat", headers=hdr("seat-a-token"))
    mine = [c for c in r.json()["captures"] if c["id"] == cap["id"]][0]
    assert mine["stopped"]
    size1 = Path(cap["file"]).stat().st_size
    time.sleep(0.4)
    assert Path(cap["file"]).stat().st_size == size1  # stream really dead


def test_logcat_capture_rejects_clear_and_file_flags(client):
    a = lease(client, "seat-a-token")
    r = client.post("/logcat/start", json={"lease_id": a["lease"]["id"], "argv": ["-c"]},
                    headers=hdr("seat-a-token"))
    assert r.status_code == 400


# ---------------------------------------------------------------- C14: server recovery + flip guard

def test_recover_noop_when_server_healthy(client):
    r = client.post("/server/recover", json={"reason": "test probe"},
                    headers=hdr("seat-a-token"))
    assert r.status_code == 200
    assert r.json()["action"] == "none"


def test_recover_refused_under_m1a_when_server_down(client):
    client.main.CFG["adb_server_socket"] = "tcp:1"  # nothing listens there
    r = client.post("/server/recover", json={"reason": "daemon glitch"},
                    headers=hdr("seat-a-token"))
    assert r.status_code == 409
    assert "own_server" in r.json()["detail"]


def test_recover_restarts_under_own_server(client):
    client.main.CFG["adb_server_socket"] = "tcp:1"
    client.main.CFG["own_server"] = True
    try:
        r = client.post("/server/recover", json={"reason": "daemon glitch"},
                        headers=hdr("seat-a-token"))
        assert r.status_code == 200
        assert r.json()["status"] == "recovered"
    finally:
        client.main.CFG["own_server"] = False


def test_exec_fails_fast_not_autostart_when_server_down(client):
    # An adb client auto-starts a missing server as the broker's user —
    # under M1a that silently flips ownership, so the broker must refuse
    # to invoke adb at all when the socket is dead.
    a = lease(client, "seat-a-token")
    client.main.CFG["adb_server_socket"] = "tcp:1"
    r = client.post("/exec", json={"lease_id": a["lease"]["id"], "argv": ["shell", "ls"]},
                    headers=hdr("seat-a-token"))
    assert r.status_code == 200
    assert r.json()["exit"] == 125
    assert "ownership-flip guard" in r.json()["stderr"]


# ---------------------------------------------------------------- C13: drop-in bit-compatibility

def load_samadb():
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("samadb_mod", str(HERE / "samadb"))
    spec = importlib.util.spec_from_loader("samadb_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_samadb_devices_output_is_adb_bit_compatible():
    # Harnesses parse `adb devices` text (#538 c4165) — exact shape required.
    mod = load_samadb()
    devmap = {
        "ABCDEF012345": {"state": "device", "model": "Quest_3"},
        "GONE123": {"state": "absent", "model": None},
    }
    out = mod.format_adb_devices(devmap)
    assert out == "List of devices attached\nABCDEF012345\tdevice\n"
    long = mod.format_adb_devices(devmap, long_form=True)
    assert long.splitlines()[1].startswith("ABCDEF012345 ")
    assert "model:Quest_3" in long
    assert "GONE123" not in out and "GONE123" not in long


def test_samadb_shim_commands_never_shadow_adb_verbs():
    # Every real adb verb must reach the passthrough; shim commands live
    # under the samadb: namespace, and bare aliases use only non-adb names.
    ADB_VERBS = {
        "devices", "connect", "disconnect", "install", "install-multiple",
        "uninstall", "shell", "push", "pull", "logcat", "wait-for-device",
        "get-state", "get-serialno", "get-devpath", "reboot", "root",
        "unroot", "tcpip", "usb", "sideload", "forward", "reverse",
        "bugreport", "backup", "restore", "emu", "kill-server",
        "start-server", "sync", "jdwp", "mdns", "version",
    }
    src = (HERE / "samadb").read_text()
    m = re.search(r'SHIM_SAFE = \{([^}]*)\}', src)
    bare_aliases = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert not (bare_aliases & ADB_VERBS), bare_aliases & ADB_VERBS


def test_get_state_is_lease_free(client):
    # get-state is a pure query; leasing it left silent 10-min holds behind
    # probe sequences (#538 c4175).
    client.get("/devices", headers=hdr("seat-a-token"))  # force a device poll
    r = client.get("/device-state", headers=hdr("seat-a-token"))
    assert r.status_code == 200
    assert r.json()["text"].strip() == "device"
    r = client.get("/leases", headers=hdr("seat-a-token"))
    assert r.json()["leases"] == []


def test_broker_version_endpoint(client):
    r = client.get("/version", headers=hdr("seat-a-token"))
    assert r.status_code == 200
    assert r.json()["text"]


def test_audit_written(client):
    a = lease(client, "seat-a-token")
    client.post("/exec", json={"lease_id": a["lease"]["id"], "argv": ["shell", "ls"]},
                headers=hdr("seat-a-token"))
    log = Path(os.environ["ADB_BROKER_STATE"]) / "audit.jsonl"
    kinds = [json.loads(l)["kind"] for l in log.read_text().splitlines()]
    assert "lease.granted" in kinds and "exec" in kinds
