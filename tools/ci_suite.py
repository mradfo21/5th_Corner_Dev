"""Run the suites that are green, one module at a time, and fail if one goes red.

This is the gate every push and pull request passes (.github/workflows/ci.yml)
and the first step of a release (M3). It is not the whole battery: the browser
e2e suites and the ones CLAUDE.md lists as red on a real machine for
environmental reasons are not in tools/ci_suites.txt, and they are run by hand.
The rule the list encodes is the one that matters on a shared tree — a suite
that was green stays green. Moving a suite onto the list is how it joins the
gate; taking one off needs a line in the commit saying why.

    python tools/ci_suite.py              # every listed suite
    python tools/ci_suite.py test_paths   # just these

Each module runs in its own interpreter (one module's imports and patches must
not leak into the next — the suites were written to be run that way), in mock
mode, with a timeout, and the authoring sandbox engaged by the suites
themselves.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIST = ROOT / "tools" / "ci_suites.txt"
TIMEOUT_S = int(os.environ.get("CI_SUITE_TIMEOUT", "600"))


def listed() -> list[str]:
    out = []
    for line in LIST.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def main(argv: list[str]) -> int:
    mods = argv or listed()
    env = dict(os.environ, SOMEWHERE_MOCK="1", PYTHONIOENCODING="utf-8")
    red = []
    t_all = time.time()
    for m in mods:
        t0 = time.time()
        try:
            p = subprocess.run([sys.executable, "-m", "unittest", m], cwd=ROOT, env=env,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=TIMEOUT_S)
            ok = p.returncode == 0
            tail = [ln for ln in p.stderr.splitlines() if ln.startswith(("Ran ", "OK", "FAILED"))]
            detail = " ".join(tail)
        except subprocess.TimeoutExpired:
            ok, detail, p = False, f"TIMEOUT after {TIMEOUT_S}s", None
        print(f"{'ok  ' if ok else 'RED '} {m:<40} {time.time() - t0:5.1f}s  {detail}", flush=True)
        if not ok:
            red.append(m)
            if p is not None:
                print("\n".join(p.stderr.splitlines()[-40:]), flush=True)
    print(f"\n{len(mods) - len(red)}/{len(mods)} green in {time.time() - t_all:.0f}s")
    if red:
        print("RED: " + ", ".join(red))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
