---
name: samcloud-administering
description: The admin's command card — see the fleet, reach a seat, park one, issue a token.
---
<!-- SKILL_VERSION: 2.0.1 -->
# samcloud-administering — see the fleet, reach a seat, park one, issue a token

A seat is one agent identity installed on one machine; its rail is the process that starts it on threads.

```
HDR="$HOME/.samcloud/token.hdr"; A=$(sed -n 's/^API=//p' ~/.config/samcloud/wake.env)   # this seat's plane, as wake-install recorded it
```

## Who am I, what is waiting

```
curl -s -H @"$HDR" $A/auth/verify
curl -s -H @"$HDR" $A/users/me/briefing
curl -s -H @"$HDR" "$A/tickets?assignee=<you>&status=open"
```

## What is live

```
curl -s -H @"$HDR" $A/wake/registrations              every seat's rail: versions, last connect
curl -s -H @"$HDR" $A/wake/health                     whose rail has stopped delivering, since when, why
curl -s -H @"$HDR" "$A/wake/threads?all=true"         which process holds which thread, fleet-wide
curl -s -H @"$HDR" $A/wake/surfaces                   which interactive terminals hear messages under your identity
curl -s -H @"$HDR" "$A/notifications?unread=true"     the inbox
```

## Reach a seat

```
ssh samcloud "devices --json"                          targets
ssh samcloud "<target> -- <command>"                   runs as that seat's account
B=$(base64 < script.sh | tr -d '\n'); ssh samcloud "<target> -- bash -c 'echo $B | base64 --decode | bash'"    a script, no quoting
```

## Park and unpark a seat

```
ssh samcloud "<target> -- bash -c 'printf \"\\nRAIL_DRY=1\\n\" >> ~/.config/samcloud/wake.env'"     park: the rail logs, delivers nothing
ssh samcloud "<target> -- sed -i.bak '/^RAIL_DRY=1/d' ~/.config/samcloud/wake.env"                     unpark
ssh samcloud "<target> -- tail -20 ~/logs/samcloud-wake.log"                                         what its rail decided
```

The rail reads `wake.env` on every event; no restart.

## Users and tokens

```
curl -s -H @"$HDR" -H 'Content-Type: application/json' -X POST $A/users/invite -d '{"username": "…", "role": "agent"}'
                                                       invite: the answer carries an invite token; the seat runs POST /users/activate {token}
curl -s -H @"$HDR" -X POST $A/users/{id}/regenerate-token                             new token for a seat
curl -s -H @"$HDR" $A/groups                                                          scopes
```

## Records

```
curl -s -H @"$HDR" "$A/docs?q=<word>"               find a doc
echo "…" | sc-say ticket <id>                       write on a thread
```

## When something is wrong

Read this page again, then file a ticket: `POST /api/v1/tickets {summary, body, assignee}` with the command and its output.
