# mac-env — a Mac sub-user as an agent environment

`mac-env.sh` makes, runs and removes an agent environment on a Mac the way you would a container. An environment is a standard macOS account with its own samcloud identity, a device row the gateway enters it by, and a rail. One Mac holds as many as you like, each with its own home, keys, token and processes, and none with admin or sudo.

```
scripts/mac-env.sh create <name> --ip <this Mac's LAN IP>    # or --via <device> to borrow its tunnel
scripts/mac-env.sh start  <name>
scripts/mac-env.sh stop   <name>
scripts/mac-env.sh remove <name>
scripts/mac-env.sh status <name>
scripts/mac-env.sh list
```

Run it from an admin account on the Mac, not under `sudo`. It asks for your password when a step needs root, and calls the plane with your own admin token (`~/.samcloud/token.hdr`, or `$SAMCLOUD_TOKEN_HDR`). The plane's API base comes from `$SAMCLOUD_API`, or else from your `~/.config/samcloud/wake.env`.

## What each stage does

| stage | on the Mac | on the plane |
|---|---|---|
| `create` | a standard account `<name>` with a random password nobody keeps; Remote Login (`com.apple.access_ssh`) and `_developer`; the gateway's published key as its only `authorized_keys`; the rail installed parked (`RAIL_DRY=1`); `/Library/LaunchDaemons/com.samcloud.listen.<name>.plist` installed, disabled and not loaded; a root-owned record in `/var/db/samcloud-envs/<name>` | a device row `<host>-<name>` (`direct-ssh` to `--ip`, or through `--via`'s tunnel) with `outbound_ssh_user` `<name>`; an agent identity `<name>` scoped to that device and each `--group`, invited and activated, with its token in the account's `~/.samcloud` (0600) |
| `start` | enables and loads the LaunchDaemon; the listener runs as `<name>` and is supervised by launchd | the listener registers the rail on connect |
| `stop` | unloads and disables the LaunchDaemon (it stays down across reboots), then ends every process running as `<name>` | — |
| `remove` | `stop`; deletes the LaunchDaemon, the account and its home (`--keep-home` keeps the home) and the record | retires the rail's registration; deletes the identity and its tokens; deletes the device row |

`start`, `stop`, `remove` and `status` act only on a name with a root-owned record, so the script never touches an account it did not make. It also refuses an admin account and your own. If `remove` cannot finish a step, it keeps the record and says which step failed; run it again to finish.

## Options

- `--ip IP`: the gateway reaches this Mac directly at `IP`. On a Mac with two interfaces, use the address its other device rows use: `GET /devices` shows them.
- `--via DEVICE`: no direct route. Borrow `DEVICE`'s reverse tunnel (shared transport); the gateway hops through it and logs in as `<name>`.
- `--device ID`: the device row's id. The default is `<host>-<name>`, where `<host>` is `$MAC_ENV_HOST` or the short hostname. It must differ from the name, because the plane refuses a device id that is also a username.
- `--group G[:role]`: the device joins group `G`, and the identity is scoped to it, with `role` (`viewer`, `editor`, `owner`) if given. An identity with only its device scope can work its own machine but cannot file tickets or docs; filing needs a group it edits.
- `--no-rail`: no rail and no LaunchDaemon. The identity takes part by polling (`docs/Rail Clients.md`).
- `--skip-preflight`: install the rail without wake-install's check that the runtime completes a turn headless. Use it for an environment that has no runtime yet.
- The rail's own settings pass through from your environment: `RAIL_RUNTIME`, `RAIL_RUNTIME_CMD`, `RAIL_RUNTIME_RESUME`, `RAIL_RUNTIME_CONV_CMD`, `RAIL_RUNTIME_CONV_RE`, `RAIL_RUNTIME_LEASE`, `MODEL`, `MAX_SESSION_MIN`, `PERMISSION_ARGS`, `WAKE_CMD`, `PREFLIGHT_CMD`. For example, a Hermes environment: `RAIL_RUNTIME=hermes RAIL_RUNTIME_CMD="hermes -z {prompt} --cli --yolo" scripts/mac-env.sh create …`.

## What it leaves to you

- **Unparking.** The rail starts parked: it logs what it would deliver and delivers nothing. Unparking is a decision about the seat, not a step of the script. When it is time, remove the `RAIL_DRY=1` line from the account's `~/.config/samcloud/wake.env` and `stop` and `start` it.
- **The runtime.** `create` does not install Claude Code, Hermes or a model. Install the runtime as the account (`sudo -H -u <name> …`, or over the gateway). A Claude environment needs no login of its own: the rail mints one from the broker each time it starts a holder (`RAIL_TOKEN_MINT_URL`).
- **What the identity may see.** It starts with its own device and the groups you name. Widen it with `PATCH /users/{id}` and group membership.

## A throwaway run, end to end

```bash
export SAMCLOUD_API=https://<plane>/api/v1 MAC_ENV_HOST=<host>
N=try-$(date +%H%M)
{ scripts/mac-env.sh create $N --ip <LAN IP> --skip-preflight &&
  scripts/mac-env.sh start  $N &&
  scripts/mac-env.sh status $N &&
  ssh samcloud "$MAC_ENV_HOST-$N -- whoami" &&
  scripts/mac-env.sh stop   $N &&
  scripts/mac-env.sh status $N &&
  scripts/mac-env.sh remove $N &&
  scripts/mac-env.sh list; } 2>&1 | tee ~/mac-env-$N.log
```

`ssh samcloud "<device> -- whoami"` prints `<name>`: the gateway reached the environment as itself.

## Before this script

`scripts/new-seat.sh` is the `create` stage alone. It runs under `sudo`, leaves the plane calls and activation to you, and reads the gateway key from a container on the plane host. `mac-env.sh create` does all of that in one step, and reads the key from `GET /gateway/pubkey`. [Devices and Enrolment](../docs/Devices%20and%20Enrolment.md) covers device rows and shared transport.
