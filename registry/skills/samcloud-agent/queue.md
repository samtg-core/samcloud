<!-- SKILL_VERSION: 3.13.1 -->
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

Release on every path out of the job: success, failure and crash. If you find a lease of yours still held after a crash, release it: `sc-lease release <id>`.

## Build a service: give the queue a healthy consumer

A service you build takes leases on behalf of its callers, so it answers for them. To register it and give it a route, see "Register a service" in `tools.md`.

- Take a lease only for real work, sized to that work.
- Release or withdraw the lease on every failure: the wait, the load, the run.
- Release what you hold when the service shuts down.
- Answer a health check, so the plane can see it is alive.

## When something is wrong

Read this page and `tools.md` again, then file a ticket: `sc-new ticket --summary "…" --assignee __ADMIN__`.
