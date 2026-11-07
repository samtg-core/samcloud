---
name: samcloud-unity-work
description: Unity and headset work on shared devices — lease the device, drive it unworn, capture what you did, report with screenshots.
---
<!-- SKILL_VERSION: 2.0.0 -->
# samcloud-unity-work — Unity and headset rounds

A **round** is one lease, one build driven and captured, one report on the ticket. Read the project's own skill first (in the project's `.claude/skills/`); it holds the build and test commands. This page is what samcloud adds around them.

## Lease the device before you touch it

```
POST /resources/{id}/leases          201: yours — 202: queued (poll `GET /leases` until yours is `active`) — 409: someone is driving, wait
DELETE /leases/{id}                  the moment your window ends; {id} is the lease id from the 201
GET  /resources                      which devices exist and who holds them
```

Installing a build needs no lease; driving, capturing and sending input do. On a shared Android box use `samadb shell <cmd>` — it takes the lease for you and runs adb (Android's device shell).

## Drive and capture without wearing it

```
adb shell monkey -p <pkg> -c android.intent.category.LAUNCHER 1        launch, as its own step after install
adb shell am broadcast -a com.oculus.vrpowermanager.prox_close        treat the headset as worn
adb shell screenrecord /sdcard/run.mp4                                 motion
<the project skill's driver command>                                   state and screenshots
adb shell am broadcast -a com.oculus.vrpowermanager.automation_disable restore the sensor when done
adb pull /sdcard/run.mp4 .                                             fetch the recording
```

Look at the captures yourself before you report.

## Pick the check that answers the question

| you want to know | use |
|---|---|
| render order, visibility, culling | the project skill's state-read command (layer, mask, queue) — seconds, no headset |
| what it looks like | a screenshot or recording of what the headset renders |
| comfort, feel, anything only a wearer can judge | a person on-head, scheduled |

Say which check a result came from.

## Long builds

Run editor and GPU work as a launchd job so it outlives your session: write a plist with a `Label` and the build command, `launchctl bootstrap gui/$(id -u) <plist>`, wait for the file your command writes when done, then `launchctl bootout gui/$(id -u)/<Label>`.

## The round report

One message on the ticket, with:

- branch and commit, and the build id
- APK hash and versionCode, plus `adb shell dumpsys package <pkg> | grep version` from the device
- the project's validation output (the JSON file it writes, attached)
- **screenshots** — before/after for a fix, one showing the feature on screen for a feature — uploaded with `POST /files` and linked to the ticket, named by time and channel (`shot_t63_eyebuffer.png`)
- one line per acceptance criterion: pass or fail, pointing at its screenshot or output

## When something is wrong

Read this page and the project's skill again, then file a ticket: `POST /api/v1/tickets {summary, body, assignee}` with the command you ran and its output.
