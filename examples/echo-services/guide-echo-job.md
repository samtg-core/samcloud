# test-echo-job

**Reference service for the plane's service standard (doc 793). For testing and copying, not for real work.**

A compute job that does almost nothing, the way a real one does it: it checks your token, takes a 64 MB lease on `lagoon/test-slot` as its own service id, waits the seconds you ask for, writes `echo.json`, puts it on your grant if you sent one, and gives the lease back.

- **Address:** the `external_url` in `GET /api/v1/services/lagoon/test-echo-job`.
- **Auth:** every call except this guide and `/health` needs `Authorization: Bearer <your samcloud token>`. Without it: `401`. The token is checked with the plane and not kept.
- **Kind:** `compute-job`.

## The quick way

```
sc-job call lagoon/test-echo-job /jobs '{"text": "hello"}' --store <a group you edit>
```

That submits, waits, downloads `echo.json` into `./<job>/` and prints the plane file you now own. Leave out `--store` to get the download only.

## The call by hand

```
POST /jobs
{"text": "hello", "wait_s": 3, "grant": "sc_grant_…"}
```

| field | | |
|---|---|---|
| `text` | required | a string, 1 to 1000 characters |
| `wait_s` | optional | whole seconds to hold the lease, 0 to 30 (default 3) |
| `grant` | optional | a grant for this service from `POST /api/v1/grants {"service_id": "lagoon/test-echo-job", "group_scope": "<group>", "ttl_s": 3600}`. It's checked before the job is queued: a grant that isn't valid is `422`, one somebody else made is `403`. |

Any other field is `422`, and so is `{}`. A body that isn't JSON is `400`.

**Jobs run one at a time, in order.** At most 4 can be waiting or running at once, and 2 of yours. Beyond that, submit is `429` with `Retry-After: 30`: try again when one of yours ends.

The answer is `202 {"job": "<id>", "status": "queued", "poll": "/jobs/<id>"}`.

## Poll, download, withdraw

```
GET    /jobs/<id>                    the job
GET    /jobs/<id>/files/echo.json    the file, once the job is done
DELETE /jobs/<id>                    withdraws a queued or running job; deletes a finished one
```

The poll answers:

- `status`: `queued` (waiting for the lease), `running`, then `done`, `failed` or `cancelled`;
- `error` when it failed;
- `results`: `["/jobs/<id>/files/echo.json"]` when it's done;
- `plane_files`: `{"echo.json": <file id>}` when it was stored on your grant, or `plane_error` saying why it wasn't (the job is still `done`, and the download still works);
- `lease`: the plane lease id it held, and `lease_released`: `true` once the plane confirmed the release;
- `expires`: when the job and its file are dropped (an hour after it finished).

Only you see your jobs. Anybody else's job id is `404`.

## What's in the file

```json
{"service": "lagoon/test-echo-job", "version": "1.0.0", "job": "<id>",
 "caller": "<you>", "lease": "<lease id>", "at": "2026-11-26T04:00:00Z",
 "input": {"text": "hello", "wait_s": 3}}
```

The input is your body without `grant`. A grant secret anywhere in `text` reads `sc_grant_[redacted]`. Nothing from your headers is written.

## The lease

It leases as `lagoon/test-echo-job`, so `GET /api/v1/leases?service_id=lagoon/test-echo-job` shows each one held by the service, not by you. It asks to wait up to 10 minutes in the queue, and its TTL is 120 s. It gives the lease back on every path: done, failed, withdrawn, or the service stopping. If the plane doesn't answer, it retries for up to 5 minutes, and logs the id if it still can't.

## Measured

A job holds its lease for `wait_s` seconds, then writes and uploads one file of under 1 KB. The process uses about 25 MB resident (Python 3.14, measured on a Mac). It leases 64 MB of `lagoon/test-slot`, a 256 MB slot that exists for it, and uses none of it. It runs on lagoon beside the plane, in its own container (128 MB, a quarter of a CPU), with no access to the plane's data.
