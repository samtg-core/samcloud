#!/usr/bin/env python3
"""The console's reference links (console/src/lib/refLinks.ts): plane refs go to the plane,
a repo's PR goes to GitHub, and a plain #N stays a ticket (#1144). Runs the TypeScript
itself under node --experimental-strip-types; prints ALL PASSED."""
import json, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "console/src/lib/refLinks.ts"
GH = "https://github.com/"
CASES = [
    # (text, [(linked text, href), ...])
    ("unity-service #16", [("unity-service #16", GH + "samtg-projects/unity-service/pull/16")]),
    ("unity-service#16", [("unity-service#16", GH + "samtg-projects/unity-service/pull/16")]),
    ("unity-service PR 16", [("unity-service PR 16", GH + "samtg-projects/unity-service/pull/16")]),
    ("Unity-Service pr #16", [("Unity-Service pr #16", GH + "samtg-projects/unity-service/pull/16")]),
    ("samcloud pull request 49", [("samcloud pull request 49", GH + "samtg-core/samcloud/pull/49")]),
    ("samcloud#49", [("samcloud#49", GH + "samtg-core/samcloud/pull/49")]),
    ("samtg-core/samcloud#47 merged", [("samtg-core/samcloud#47", GH + "samtg-core/samcloud/pull/47")]),
    ("other/thing#3", [("other/thing#3", GH + "other/thing/pull/3")]),
    ("on samcloud #1144 today", [("#1144", "/tickets/1144")]),
    ("samcloud #5", []),
    ("ticket 1082 and #1082 and tickets 7", [("ticket 1082", "/tickets/1082"), ("#1082", "/tickets/1082"), ("tickets 7", "/tickets/7")]),
    ("doc #470, task 869", [("doc #470", "/docs/470"), ("task 869", "/tasks/869")]),
    ("PR #104 and step #1", []),
    ("my-unity-service #16", [("#16", "/tickets/16")]),
    ("unity-service 16", []),
    ("unity-services #16", [("#16", "/tickets/16")]),
    ("https://github.com/samtg-core/samcloud#12", []),
    ("4dgs-pipeline#2 and scene-render PR 3", [("4dgs-pipeline#2", GH + "samtg-projects/4dgs-pipeline/pull/2"),
                                              ("scene-render PR 3", GH + "samtg-projects/scene-render/pull/3")]),
]

script = f"""
import {{ findRefs }} from {json.dumps(str(SRC))};
const cases = JSON.parse(process.argv[1]);
console.log(JSON.stringify(cases.map((t) => findRefs(t).map((r) => [r.text, r.href]))));
"""
r = subprocess.run(["node", "--no-warnings", "--experimental-strip-types", "--input-type=module", "-e", script,
                    json.dumps([c[0] for c in CASES])], capture_output=True, text=True, timeout=60)
if r.returncode != 0:
    print(r.stderr); print("FAILED: node could not run refLinks.ts"); sys.exit(1)
got = json.loads(r.stdout)
fails = 0
for (text, want), g in zip(CASES, got):
    ok = [tuple(x) for x in g] == want
    fails += not ok
    print(("  ✓ " if ok else "  ✗ ") + repr(text) + ("" if ok else f"\n      want {want}\n      got  {g}"))
print(f"\nALL PASSED: {len(CASES)} passed" if not fails else f"\nFAILED: {fails} of {len(CASES)}")
sys.exit(1 if fails else 0)
