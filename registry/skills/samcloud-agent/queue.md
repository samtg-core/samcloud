<!-- SKILL_VERSION: 3.13.3 -->
# queue — get compute by asking for it and waiting

Every machine on samcloud registers its compute — a GPU, its memory — as a **resource**. Each resource has one **queue**. You ask for what your job really needs, you wait your turn with no deadline, you run, and you give it back. Local models, renders, splat jobs and tools all take their turn in the same queue.

## The model

- **One queue per resource.** Waiters are granted by priority, then first come, first served. A waiter that doesn't fit yet stops the line; nobody jumps ahead of it.
- **Ask for your real need.** The memory your job uses, or the whole machine (`--exclusive`) if that is what it takes.
- **Wait with no deadline.** A long queue is normal: it is the plane doing a day of work in the background. Do not add a timeout, cancel and ask again, or move the job to another machine.
- **A job is submitted when it has a lease id.** A shell you launched is not a job in the queue until the lease call answers.
- **Give back exactly what you took.** Release your own lease when the work ends, and when it fails. A lease nobody renews or releases can keep everyone behind it waiting.

## Call a service: the service queues for you

```
GET /services                                    what runs where, with its endpoint
GET /services/{id}                               one service: description, endpoint, capabilities
```

A service such as image generation or image-to-splat takes the lease itself. You send it the job and wait for the result; you do not take a lease to call a service. If the answer is "queued", it is working: keep waiting on the job, and do not send it again.

The results come back to you through the service's own API. To have them stored as plane files you own, send a grant with the job: `tools.md`, "Find and call a service".

## Run your own job: take a lease

```
GET /resources                                   what each machine offers
sc-lease queue <resource>                        who waits, in grant order
id=$(sc-lease acquire <resource> --memory 16000 --wait)     ask, then block until granted
sc-lease hold "$id" --ttl 300 &                  renew while the job runs
<run the job>
sc-lease release "$id" --after-pids "$!"         give it back once its processes have exited
```

`--wait` with no number waits with no deadline. `sc-lease` is served at `GET /api/v1/helpers/lease`; `tools.md` has the install line and the raw calls.

Release on every path out of the job: success, failure and crash. If you find a lease of yours still held after a crash, release it: `sc-lease release <id>`. A release can meet the plane mid-restart; "Build a service" below says how to make it land.

## Build a service: give the queue a healthy consumer

A service you build takes leases on behalf of its callers, so it answers for them. It leases with its own service token, as itself. "Register a service" in `tools.md` covers its identity, the route, and how it returns results.

- Take a lease only for real work, sized to that work.
- Release or withdraw the lease on every failure: the wait, the load, the run.
- Release what you hold when the service shuts down.
- **Make every release land.** Each merge restarts the plane for about a minute. A release in that minute gets no answer or a 5xx:
  - retry it with back-off for a few minutes;
  - keep the lease id on disk until a 2xx, or a 404 saying `Lease <id> not found`, comes back;
  - have something periodic (a timer, your worker loop) retry whatever is still pending.

  A renew that fails the same way is retried within the TTL; it is not a lost lease. On 25 Nov one 502 on a release held 32 GB of slice's GPU for three hours, and another held 16 GB on Obelisk.
- **On a `hold` resource the TTL doesn't give memory back.** Check `GET /resources/{id}` → `expiry_policy`. An expired lease there stays `unsettled`, holding its memory, until its holder releases it.
- Answer a health check, so the plane can see it is alive.

## When something is wrong

Read this page and `tools.md` again, then file a ticket: `sc-new ticket --summary "…" --assignee __ADMIN__`.
