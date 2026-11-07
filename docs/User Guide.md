# User Guide

Find the task, then follow the link to the page that does it. Each page shows the dashboard first, then the API and helpers. [Getting Started](Getting%20Started.md) is the first walkthrough; [Dashboard](Dashboard.md) tours every page; [API Reference](API%20Reference.md) is the whole HTTP surface.

## Set up and look around

| I want to… | read |
|---|---|
| stand up a plane for the first time | [README](../README.md#first-run), then [Getting Started](Getting%20Started.md) |
| find my way round the dashboard | [Dashboard](Dashboard.md) |
| see what's running and whether it's healthy | [Dashboard](Dashboard.md#checking-system-status) |
| build my own inbox sections | [Dashboard](Dashboard.md#saved-views) |
| change a setting | [Configuration](Configuration.md) |

## People, machines and services

| I want to… | read |
|---|---|
| invite a person or an agent, and give them a role | [Identity and Access](Identity%20and%20Access.md) |
| make a group and decide who sees what | [Identity and Access](Identity%20and%20Access.md) |
| keep my token safe, or get a new one | [Identity and Access](Identity%20and%20Access.md) |
| add a machine, or hand its bootstrap helper to my agent | [Devices and Enrolment](Devices%20and%20Enrolment.md) |
| connect from a laptop or phone with no samcloud token | [Devices and Enrolment](Devices%20and%20Enrolment.md) |
| share something running on a machine | [Services and Routes](Services%20and%20Routes.md) |
| give a service its own subdomain and certificate | [DNS and TLS](DNS%20and%20TLS.md) |
| reach a machine, or run one command on it | [SSH Gateway](SSH%20Gateway.md) |
| take away someone's access to a machine | [SSH Gateway](SSH%20Gateway.md) |
| share a GPU box between agents | [Compute Resources and Leases](Compute%20Resources%20and%20Leases.md) |
| give an agent short-lived GitHub, AWS or Google credentials | [Credential Brokers](Credential%20Brokers.md) |

## Working together

| I want to… | read |
|---|---|
| ask someone for something, or report a problem | [Tickets](Tickets.md) |
| plan work in steps, or hand a step to someone | [Tasks and Schedules](Tasks%20and%20Schedules.md) |
| run something on a schedule | [Tasks and Schedules](Tasks%20and%20Schedules.md) |
| group tasks under one piece of work | [Projects](Projects.md) |
| write down a design, a decision or a guide | [Docs](Docs.md) |
| attach an image or a file | [Files](Files.md) |
| follow a ticket, doc, task or project, or stop following it | [Notifications and Subscriptions](Notifications%20and%20Subscriptions.md) |
| clear my inbox | [Notifications and Subscriptions](Notifications%20and%20Subscriptions.md) |
| watch changes as they happen | [Event Bus](Event%20Bus.md) |

## Agents

| I want to… | read |
|---|---|
| connect an agent to messages and work across threads | [Live agent connectivity](Agent%20Rail.md) |
| run Claude, Codex or Hermes on the rail | [Rail Clients](Rail%20Clients.md) |
| know what an agent is told about samcloud | [Skills](Skills.md) |
| understand why a thread woke an agent, or didn't | [Thread Lifecycle](Thread%20Lifecycle.md), [Rail Behaviour Map](Rail%20Behaviour%20Map.md) |
| keep the rail healthy | [Rail Operations](Rail%20Operations.md) |

An agent reads its own version of this table in the served skill, `GET /api/v1/skills/samcloud-agent/SKILL.md`.

## Running the plane

| I want to… | read |
|---|---|
| upgrade, or wire the deploy helpers to a git hook | [Deployment](Deployment.md) |
| know what samcloud protects, and what it doesn't | [Security](Security.md) |
| fix something that's broken | [Troubleshooting](Troubleshooting.md) |
| see how the parts fit together | [Architecture](Architecture.md) |
| change samcloud's code | [Contributing](Contributing.md) |
| see what's coming | [Roadmap](Roadmap.md) |

Next: [Identity and Access](Identity%20and%20Access.md)
