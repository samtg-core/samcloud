# adb-broker — leased adb access for multi-seat boxes

Ticket **#538**, brief **doc 148**. One broker per device-hosting box; seats
never talk to the adb server directly. Credential-broker idiom (aws-broker /
gh-broker): standalone FastAPI, samcloud Bearer verified per call against
`/auth/verify`, JSONL audit, optional health loop to the registry. The
registry stays the identity/scope source — this is a leaf service, not core.

## Why (measured on a shared Android test host, 2026-08-27)
- One seat's `adb kill-server` dropped the device offline for the other
  mid-flight; both rails blocked ~100 min on unbounded `wait-for-device`.
- Cross-seat installs of one package id clobbered each other; a third
  party's build was uninstalled without backup (the b130 incident).
- The server owner's `~/.android/adbkey` silently decides device auth.

The broker makes those structural: exclusive TTL leases with a queue, a hard
deadline on every exec, guarded verbs, an APK-identity gate at the install
door, and displaced-APK auto-backup — **no backup, no displace**.

## Run
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 7791
```
Config: `~/.config/samcloud/adb-broker/config.json` (see `DEFAULTS` in
`main.py`). Two modes:
- **M1a** (default): fronts the existing adb server (`adb_server_socket:
  tcp:5037`), whoever owns it. Arbitration lands with zero cutover. The
  broker raw-probes the server socket before every adb invocation and
  fails fast (exit 125) if it's down — an adb client would auto-start a
  replacement server as the broker's user, silently flipping ownership.
- **M1b**: `own_server: true` + a dedicated socket — the broker's user
  starts and owns the server and its adbkey (one identity, one authorized
  key). Requires stopping the old server + one on-head auth accept.

State: `~/.local/samcloud/adb-broker/` — `audit.jsonl` (every lease event
and exec: identity, argv, device, exit, duration, backup path),
`backups/<pkg>/<version>_<utc>/` (displaced APKs + `meta.json` with sha256;
auto-pruned oldest-first over `backups_max_gb`), `state.json` (leases
survive a broker restart).

## API
| Endpoint | Purpose |
|---|---|
| `POST /leases` `{device: serial\|"any", ttl, purpose, app_tag?, priority?, canonical?}` | grant, or queue (FIFO + priority) with visible position |
| `GET /leases/{id}` · `POST /leases/{id}/renew` · `DELETE /leases/{id}` | poll / keep alive / release (release promotes the queue head) |
| `POST /exec` `{lease_id, argv, timeout_s?, package?, no_backup?}` | run one adb command under the lease, serial pinned, hard deadline |
| `GET /devices` · `GET /backups?package=` · `GET /audit/recent` | inventory / restore sources / audit tail |
| `POST /logcat/start` `{lease_id, argv?}` · `POST /logcat/{id}/stop` · `GET /logcat` | streaming logcat as a lease-bound broker-side capture (cold-boot sweeps): the broker writes `captures_dir/<id>.log` (world-readable), renewals keep it alive, lease release/expiry auto-stops it — streaming without unbounded holds |
| `POST /server/recover` `{reason}` | arbitrated daemon-glitch recovery (`samadb samadb:recover`): probe first, restart only an unhealthy server the broker owns; healthy servers are never bounced, and under M1a it escalates instead of flipping ownership |
| `POST /admin/server` `{action: restart\|kill}` | the only path to server restart (admin bearer) |

Exec shaping: guarded verbs (`kill-server`, `connect`, `disconnect`,
`tcpip`, `usb`, `root`, …) are 403 for non-admin; `wait-for-*` becomes a
bounded `get-state` poll; bare `logcat` is forced to `-d`; interactive
`shell` is refused; pre-verb transport flags (`-s`, `-t`, `-H`, `-P`, `-L`)
are refused — the broker pins the serial itself.

The install door (#538 c4145/c4147/c4149): the broker reads the APK's
applicationId with aapt before installing. Canonical ids (config
`canonical_ids`) require a validation-grade lease (`canonical: true`,
admin-gated in M1). A lease `app_tag` mismatch warns (M1). If the same id
is already on the device it is pulled to the backups dir first; backup
failure aborts the install. Same for uninstall. `no_backup` is admin-only.

## samadb (client shim)
```bash
samadb install -r build.apk        # lease + stage + backup + install
samadb -s any shell getprop ro.product.model
samadb logcat -t 100
samadb devices                     # emits adb's exact text format (drop-in)
samadb samadb:lease --tag pr406 --purpose "PR 406 validation"
samadb samadb:status | samadb samadb:release
samadb samadb:logcat-start -v time # prints the capture file path; keep the
samadb samadb:logcat-stop          # lease renewed while it runs
samadb samadb:backups com.example.app   # list restore points
samadb samadb:restore com.example.app 0.9.1-b130
```
Drop-in guarantee: every real adb verb passes through with adb's own output
shapes (`devices` emulated bit-compatibly, `wait-for-device` silent on
success, `version` forwarded; `restore` is adb's restore). Shim commands
live under the `samadb:` namespace so no adb verb is ever shadowed; bare
aliases exist only for non-adb names (lease, release, status, backups,
captures, logcat-start, logcat-stop), enforced by
`test_samadb_shim_commands_never_shadow_adb_verbs`.
The shim caches one lease per user (`~/.samadb/lease.json`), renews it per
call, queues with a visible position (bounded by `SAMADB_WAIT_MAX`, default
600s), and stages APKs to `/Users/Shared/samadb-staging` (content-addressed)
so the broker can read files from any seat's home. Harness switch-over is
`s/adb/samadb/` — argv-compatible for the everyday verbs.

Env: `SAMADB_URL`, `SAMADB_TOKEN_FILE`, `SAMADB_WAIT_MAX`,
`SAMADB_TIMEOUT`; per-call flags `--samadb-timeout N`, `--package ID`,
`--no-backup` (admin).

## Tests
`pytest test_broker.py -q` — 19 tests against a fake adb/aapt/auth, no
device needed. They encode the acceptance claims C1–C11 (exclusivity,
queue promotion, guarded verbs, deadlines, bounded wait-for-device, backup
discipline, the canonical-id door, tag warnings, holder checks, audit).
