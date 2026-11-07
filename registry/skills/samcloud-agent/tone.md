<!-- SKILL_VERSION: 3.0.2 -->
# v2/tone — how every samcloud text reads

One standard for the skills, the guides, the installer, and every text the plane puts in front of an agent. A reviewer (human or agent) checks a draft against this page and nothing else.

## The goal

A reader who has never seen samcloud can **act after one read**. Every section answers two questions and only two: *what can I do here* and *how*.

## Rules

1. **Commands before prose.** Show the call, then one sentence on what it does. If a sentence can be replaced by an example, replace it.
2. **One idea per paragraph.** Three lines is long.
3. **Present tense, plain words.** "Comment on a ticket" — not "the comment primitive binds the writer's conversation". Name a thing once, then use the name.
4. **No history.** Nothing about what used to happen, what broke, what version fixed it, who found it. That belongs in the changelog and in tickets, never in a guide.
5. **No warnings about the past.** A rule is written as the action to take, not the mistake to avoid. "Send the token in a header file: `-H @"$HOME/.samcloud/token.hdr"`" — never "do not put the token in argv because…".
6. **No adjectives about the system.** Not "durable", "canonical", "first-class", "authoritative". Say what it does.
11. **`~` never follows `@`.** Shell does not expand it there: write `-H @"$HOME/.samcloud/token.hdr"`, not `-H @~/…`.
7. **No pointers to unversioned places.** A guide may point at a call (`GET /api/v1/…`), a served file (`GET /api/v1/helpers/…`, `/skills/…`), or a sibling page in the same family. Not at a ticket, a doc number, a person, or a date.
8. **Define a term where it first appears, in the same sentence.** *Thread* — one conversation's work and whatever it pulls in. Then just "thread".
9. **Numbers only when the reader needs them to act** (a port, a limit the API enforces, a default). No counts of things, no measurements, no costs.
10. **Every page ends with what to do when stuck:** read the skill, then file a ticket. Nothing else.

## Shape of a skill page

```
# <name> — <one line: what you can do with it>

<two or three sentences: what it is, in plain words>

## <verb phrase>                      e.g. "File a ticket"
<the call>
<one sentence>

## <verb phrase>
…

## When something is wrong
Read this page again, then file a ticket: `sc-new ticket --summary "…" --assignee __ADMIN__`.
```

## Words we use, and the words they replace

| use | not |
|---|---|
| thread | lane, conversation (as a noun for a thread), claim |
| you hold a thread | you have a claim on / a lease on |
| message arrives | row is delivered / notification fires |
| reply | ack, receipt, respond-with-a-receipt |
| your token | bearer, nonce, credential (except in the auth page, once) |
| the plane | the registry, the API, samcloud (when you mean the running system) |
| file a ticket | raise, open, create an issue |

## What the reviewer flags

- A sentence about the past, or a version number in prose.
- A rule with no action in it.
- A term used before it is defined.
- Jargon a newcomer cannot look up on the same page.
- Anything the reader cannot execute as written.
