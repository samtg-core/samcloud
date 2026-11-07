# Roadmap

What comes next, concretely. Each item says what exists today and what the next step is. None of them is a promise, and the order is rough.

## A login in front of routed services

**Today:** samcloud routes traffic and leaves access to the service. Scope decides who can find, manage and reach a service through the API, the dashboard and the gateway, but it doesn't guard the service's own address: anyone who can reach its port, or its subdomain with a domain, can load it, unless the app has its own login. [Security](Security.md) states this limit. The Caddy block the registry writes for a route is a plain `reverse_proxy`, with no authentication step.

**Next:** an optional gate in front of a routed service that checks the caller's samcloud session or token against the same groups as the API, so a service shared with a group is reachable only by that group's members.

## Choose your plane's name at setup

Choose the name shown in the dashboard and agent guidance when you initialise a plane—for example, `yournameherecloud`. Today those surfaces use the samcloud name.

## More clients on the rail

**Today:** the rail delivers to Claude, Codex and Hermes ([Rail Clients](Rail%20Clients.md)). Claude runs as a held, streaming session. Hermes has a native streaming client (`clients/hermes/`). A runtime that can't be fed while it runs goes through the batch adapter (`GET /api/v1/helpers/adapter`), one delivery per turn, configured by `RAIL_RUNTIME*` in the seat's `wake.env`. An interactive CLI or GUI chat connects to its threads through a client-specific feed; Claude Code uses the samcloud channel.

**Next:** more clients get support as they're needed, as native streaming clients where the runtime allows it, through the adapter where it doesn't.

## Shared compute

**Today:** compute is a primitive ([Compute Resources and Leases](Compute%20Resources%20and%20Leases.md)).

- A device's manager registers its GPUs, CPUs and other resources (`POST /api/v1/resources`) and pushes live utilisation (`POST /api/v1/resources/{id}/stats`).
- Agents lease declared capacity (`POST /api/v1/resources/{id}/leases`). Shared resources grant by memory; an `exclusive` resource grants one lease at a time. A request that doesn't fit can queue, in priority then arrival order, and a lease is renewed in place up to a ceiling. A `hold` resource keeps charging an expired holder until it is settled.
- `sc-lease` (`GET /api/v1/helpers/lease`) is the shell client: `acquire` blocks until granted, `hold` renews while the work runs, `release` waits for the work's processes to exit, and `queue` shows who waits. A model load, a render and a build queue against the same host in one order, without knowing about each other.
- `registry/resource-bootstrap.sh` (resource discovery and a stats daemon) ships in the registry image, but no route serves it; it is run by hand.

**Next:** builds and other kinds of job on the same ledger, with the job tooling published alongside it.

## Modules as their own packages

**Today:** DNS (`dns-namecheap`) and the credential brokers (GitHub, AWS, Google, adb) are off unless configured, but they live in this repository ([DNS and TLS](DNS%20and%20TLS.md), [Credential Brokers](Credential%20Brokers.md)).

**Next:** they move into separate modules, along with one view of the tokens you can mint and the ones you manage.

## A local samcloud CLI

**Today:** an agent drives the plane with `curl` and a header file, the served pens (`sc-say`, `sc-new`, `sc-edit`, `sc-pr`, `sc-lease`, `scope-check`) and the gateway (`ssh samcloud "<device> -- <cmd>"`). Each tool reads the same token, but an agent still keeps the API's URL, the gateway's host and port, and its access key in mind.

**Designed, not built:** one local command as the single entry point, holding the credentials behind one config directory:

```
samcloud login                       # store a token; verify it with /auth/verify
samcloud whoami                      # identity and scopes
samcloud devices [--json]            # the targets you can reach (the gateway's devices --json)
samcloud ssh <device> [-- <cmd>]     # a shell, or one command with its exit code
samcloud status [--json]             # control-plane status
samcloud ticket {list|show|new|comment|resolve} …
samcloud enroll <device> …           # POST /enroll, print the bootstrap one-liner
samcloud guide [device|service|me]   # the relevant /guide page
```

- **Config:** `~/.config/samcloud/` (or `$SAMCLOUD_HOME`) holds `token` (0600), `key` (the access key) and `config.toml` (registry URL, gateway host and port, identity).
- **Transport stays SSH:** `samcloud ssh` shells out to the same gateway, so it inherits its auth and audit. No new daemon, port or trust root.
- **One credential for agents:** the CLI provisions the access key from the bearer on first use (`POST /access-keys`) and caches it, so an agent holds one token and never handles the SSH key.

Open questions: Go (one static binary, like the gateway) or Python (already on every seat); serving it from the registry like the skills; and whether `samcloud ssh` should later use a registry-mediated exec endpoint instead of `ssh`.

## The pieces around it

The service and container setups that run alongside samcloud are published as they're cleaned up.

## Many instances

Further out: your instance and a friend's sharing resources, with trust between operators as explicit as trust between users. Today a seat can already address more than one plane: a helper's `--plane` names the planes `SAMCLOUD_PLANES` lists ([Configuration](Configuration.md#the-plane)).

## Related

- [Security](Security.md): the security model and its known limits
- [Contributing](Contributing.md): how a change lands
- [Architecture](Architecture.md)
