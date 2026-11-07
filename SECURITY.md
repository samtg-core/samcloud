# Security

**Please report vulnerabilities privately**, through GitHub's private vulnerability reporting: the **Security** tab of this repository, then **Report a vulnerability**. Don't open a public issue for a security problem.

A useful report says:
- what you found
- how to reproduce it, against which version (`GET /api/v1/` names the release)
- what an attacker gains: another user's scope, a machine behind the gateway, or a token

You'll get an answer on the report. A confirmed issue is fixed in a release before it is discussed in public, and the advisory credits you unless you'd rather it didn't.

**What's in scope:** the registry, the gateway and its SSH handling, enrolment and bootstrap, tokens and scopes, the dashboard, the rail, and the optional modules in this repository.

**What's out of scope:** how a particular deployment is run (its network, its host, its other software), and findings that need an admin token to begin with.
