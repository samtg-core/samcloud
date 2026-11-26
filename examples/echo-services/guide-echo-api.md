# test-echo-api

**Reference service for the plane's service standard (doc 793). For testing and copying, not for real work.**

One synchronous call that answers with the JSON you send it. No compute, no lease, no files.

- **Address:** the `external_url` in `GET /api/v1/services/lagoon/test-echo-api`.
- **Auth:** every call except this guide and `/health` needs `Authorization: Bearer <your samcloud token>`. Without it: `401`. The token is checked with the plane and not kept.
- **Kind:** `no-compute`.

## The call

```
curl -s -H @"$HOME/.samcloud/token.hdr" -H 'Content-Type: application/json' \
  -X POST <external_url>/echo -d '{"text": "hello"}'
```

The body is any JSON object, at most 16 KB. The answer is `200`:

```json
{"echo": {"text": "hello"}, "caller": "<you>", "service": "lagoon/test-echo-api", "version": "1.0.0"}
```

`echo` is your body without any `grant` field, and a grant secret anywhere in it reads `sc_grant_[redacted]`. Nothing from your headers comes back.

| you send | you get |
|---|---|
| no token, or one the plane refuses | `401` |
| a body that isn't JSON | `400` |
| JSON that isn't an object (a list, a string) | `422` |
| more than 16 KB | `413` |
| any path but `/echo` | `404`; another method on `/echo` is `405` |

Each refusal is `{"error": "<what to fix>"}`.

## Measured

An answer takes about as long as the plane takes to check your token, which is cached for a minute. The process uses about 25 MB resident (Python 3.14, measured on a Mac). It runs on lagoon beside the plane, in its own container (128 MB, a quarter of a CPU), with no access to the plane's data.
