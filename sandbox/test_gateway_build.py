#!/usr/bin/env python3
"""gateway-build — the Go gateway compiles, vets, is formatted, and passes its tests.

The three gates a contributor runs are all Python: pytest, `sandbox/harness.py`,
`sandbox/docs_coverage.py`. Nothing compiled `gateway/`, so a change to the Go
gateway could pass every gate and still fail to build — and the gateway is what
every seat's every hop goes through.

It is checked the way the image builds it (`deploy/Dockerfile.sshd`): the `go`
directive pinned down to the container's toolchain, `CGO_ENABLED=0`, `GOOS=linux`.
Building against the local toolchain alone would let a newer language feature in
than the image can compile. The copy is made in a temp dir so the repo's `go.mod`
is never mutated.

  1. the toolchain is present            -> otherwise this gate checks nothing
  2. gofmt has nothing to say
  3. go vet is clean
  4. it builds for the container         (go directive pinned, CGO off, linux)
  5. go test passes
  6. it is still stdlib-only             -> no go.sum, no third-party requires

A seat with no Go toolchain can pass deliberately with SAMCLOUD_SKIP_GO_GATE=1,
which prints a SKIPPED line rather than a quiet success.

Run: python3 sandbox/test_gateway_build.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATEWAY = ROOT / "gateway"
DOCKERFILE = ROOT / "deploy" / "Dockerfile.sshd"

PASS, FAIL = 0, 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"      ✓ {label}")
    else:
        FAIL += 1
        print(f"      ✗ {label}")
        for line in str(detail).strip().splitlines()[:15]:
            print(f"          {line}")


def container_go_version() -> str:
    """The Go version the image builds with, read from the Dockerfile.

    `FROM golang:1.24-bookworm` — derived rather than duplicated, so bumping the
    image bumps this gate with it.
    """
    try:
        text = DOCKERFILE.read_text()
    except OSError:
        return ""
    m = re.search(r"FROM\s+golang:(\d+\.\d+)", text)
    return m.group(1) if m else ""


def run(args, cwd, env=None):
    full_env = dict(os.environ)
    full_env.setdefault("GOTOOLCHAIN", "local")
    if env:
        full_env.update(env)
    proc = subprocess.run(args, cwd=cwd, env=full_env,
                          capture_output=True, text=True, timeout=600)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def main():
    print("\n  Gateway build gate\n" + "  " + "-" * 58)

    if os.environ.get("SAMCLOUD_SKIP_GO_GATE"):
        print("      SKIPPED: SAMCLOUD_SKIP_GO_GATE is set — the Go gateway was not checked.")
        print("\n  ALL PASSED: gateway build gate skipped by request\n")
        return 0

    if shutil.which("go") is None:
        print("      ✗ no Go toolchain on PATH — this gate cannot check anything")
        print("          install Go (the image uses "
              f"{container_go_version() or '1.24'}), or set SAMCLOUD_SKIP_GO_GATE=1")
        print("\n  FAILED: gateway build gate 0 passed, 1 failed\n")
        return 1
    check("1. Go toolchain present", True)

    code, out = run(["gofmt", "-l", "."], cwd=GATEWAY)
    check("2. gofmt clean", code == 0 and out == "",
          out or "gofmt exited non-zero")

    code, out = run(["go", "vet", "./..."], cwd=GATEWAY)
    check("3. go vet clean", code == 0, out)

    # Build the way the image does, in a copy so go.mod stays as committed.
    pinned = container_go_version()
    with tempfile.TemporaryDirectory(prefix="samcloud-gateway-build-") as tmp:
        build_dir = Path(tmp) / "gateway"
        shutil.copytree(GATEWAY, build_dir,
                        ignore=shutil.ignore_patterns("samcloud-gateway", "gateway"))
        gomod = build_dir / "go.mod"
        if pinned:
            gomod.write_text(re.sub(r"(?m)^go .*$", f"go {pinned}", gomod.read_text()))
        label = f"4. builds for the container (go {pinned or 'local'}, CGO off, linux)"
        code, out = run(["go", "build", "-o", str(Path(tmp) / "samcloud-gateway"), "."],
                        cwd=build_dir,
                        env={"CGO_ENABLED": "0", "GOOS": "linux"})
        check(label, code == 0, out)

        code, out = run(["go", "test", "./..."], cwd=build_dir)
        check("5. go test passes", code == 0, out)

    # The Dockerfile's build stage copies gateway/ alone and runs `go build` with
    # no module download, so a third-party import would break the image.
    gosum = (GATEWAY / "go.sum").exists()
    requires = [l for l in (GATEWAY / "go.mod").read_text().splitlines()
                if l.strip().startswith("require") or "=>" in l]
    check("6. still stdlib-only (no go.sum, no requires)",
          not gosum and not requires,
          f"go.sum={gosum} requires={requires}")

    print(f"\n  {'ALL PASSED' if not FAIL else 'FAILED'}: "
          f"gateway build gate {PASS} passed, {FAIL} failed\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
