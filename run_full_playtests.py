#!/usr/bin/env python3
"""Run the full local playtest suite and record all outcomes to disk."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
STAMP = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
OUT = ROOT / "playtest_results" / f"full_run_{STAMP}"
BASE = (sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:5001")


def run(cmd: list[str], timeout: int = 600) -> dict:
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        ok = proc.returncode == 0
        return {
            "cmd": cmd,
            "ok": ok,
            "exit_code": proc.returncode,
            "elapsed_s": round(time.time() - t0, 1),
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "cmd": cmd,
            "ok": False,
            "exit_code": -1,
            "elapsed_s": round(time.time() - t0, 1),
            "stdout": (e.stdout or "") if isinstance(e.stdout, str) else "",
            "stderr": f"TIMEOUT after {timeout}s",
        }
    except Exception as e:
        return {
            "cmd": cmd,
            "ok": False,
            "exit_code": -1,
            "elapsed_s": round(time.time() - t0, 1),
            "stdout": "",
            "stderr": str(e),
        }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "base_url": BASE,
        "backend_note": "",
        "steps": [],
    }

    # Detect backend
    status = run([sys.executable, "-c",
        f"import urllib.request; print(urllib.request.urlopen('{BASE}/api/status').read().decode())"], timeout=15)
    report["server_status_raw"] = status.get("stdout", "").strip()
    if "mock" in report["server_status_raw"]:
        report["backend_note"] = (
            "Server running in MOCK mode (no GEMINI_API_KEY). "
            "Loop mechanics tested fully; LLM narrative/images not."
        )

    steps: list[tuple[str, list[str], int]] = [
        ("py_compile", [sys.executable, "-m", "py_compile", *[str(p) for p in ROOT.glob("*.py")]], 120),
        ("import_api", [sys.executable, "-c", "import api; print('import ok')"], 120),
        ("autoplay_mixed_15", [
            sys.executable, "autoplay.py", "--url", BASE, "--turns", "10",
            "--strategy", "mixed", "--turn-timeout", "180",
            "--out", str(OUT / "autoplay_mixed_10.json"),
        ], 2400),
        ("autoplay_cycle_10", [
            sys.executable, "autoplay.py", "--url", BASE, "--turns", "8",
            "--strategy", "cycle", "--turn-timeout", "180",
            "--out", str(OUT / "autoplay_cycle_8.json"),
        ], 2400),
        ("autoplay_custom_8", [
            sys.executable, "autoplay.py", "--url", BASE, "--turns", "6",
            "--strategy", "custom", "--turn-timeout", "180",
            "--out", str(OUT / "autoplay_custom_6.json"),
        ], 1800),
        ("autoplay_first_10", [
            sys.executable, "autoplay.py", "--url", BASE, "--turns", "8",
            "--strategy", "first", "--turn-timeout", "180",
            "--out", str(OUT / "autoplay_first_8.json"),
        ], 2400),
        ("capture_15", [
            sys.executable, "playtest_capture.py", BASE, "10",
        ], 2400),
        # The autoplay strategies above only ever press choice buttons, which
        # left SCAN — the detector, the hotspot tags, and the object turn —
        # untested by every automated run. These two play through SCAN instead,
        # and record their frames + a GIF into this run folder.
        #
        # The mixed plan carries scan_interact now. It was excluded on the
        # grounds that INTERACT was shelved and a plan using it "would spend
        # half its turns on a path no player can reach" — true while the verb
        # was switched off wholesale, and false since it shipped on stills as a
        # Moment (interactEnabled / openInteractMoment in standalone.js). For
        # as long as that comment stood, the game's second object verb was in
        # no automated run at all.
        ("scan_playtest_mixed", [
            sys.executable, "playtest_interactive.py", "--url", BASE, "--turns", "9",
            "--plan", "scan_move,scan_interact,choice",
            "--session", f"scanmix{STAMP}", "--out", str(OUT / "scan_run_mixed"),
        ], 3000),
        ("scan_playtest_objects_only", [
            sys.executable, "playtest_interactive.py", "--url", BASE, "--turns", "6",
            "--plan", "scan_move",
            "--session", f"scanobj{STAMP}", "--out", str(OUT / "scan_run_objects_only"),
        ], 2400),
        ("unittest_playtest_interactive", [sys.executable, "-m", "unittest", "test_playtest_interactive", "-v"], 120),
        ("unittest_standalone_e2e", [sys.executable, "-m", "unittest", "test_standalone_e2e", "-v"], 600),
        ("unittest_feed_engine", [sys.executable, "-m", "unittest", "test_feed_engine", "-v"], 300),
        ("unittest_choices_robustness", [sys.executable, "-m", "unittest", "test_choices_robustness", "-v"], 300),
        ("unittest_providers", [sys.executable, "-m", "unittest", "test_providers", "-v"], 120),
        ("unittest_render_mode", [sys.executable, "-m", "unittest", "test_render_mode", "-v"], 120),
        ("unittest_concurrent_sessions", [sys.executable, "-m", "unittest", "test_concurrent_sessions", "-v"], 600),
        ("unittest_cost_tracker", [sys.executable, "-m", "unittest", "test_cost_tracker", "-v"], 120),
        ("unittest_outbound_dns", [sys.executable, "-m", "unittest", "test_outbound_dns", "-v"], 60),
    ]

    for name, cmd, timeout in steps:
        print(f"\n>>> {name} ...", flush=True)
        result = run(cmd, timeout=timeout)
        result["name"] = name
        report["steps"].append(result)
        log_path = OUT / f"{name}.log"
        log_path.write_text(
            f"cmd: {' '.join(cmd)}\nexit: {result['exit_code']}\n"
            f"elapsed: {result['elapsed_s']}s\n\n--- stdout ---\n{result['stdout']}\n\n--- stderr ---\n{result['stderr']}",
            encoding="utf-8",
        )
        mark = "PASS" if result["ok"] else "FAIL"
        print(f"    [{mark}] {result['elapsed_s']}s exit={result['exit_code']}", flush=True)

    # Move latest capture into this run folder
    captures = sorted((ROOT / "playtest_results").glob("full_capture_*.json"))
    if captures:
        latest = captures[-1]
        dest = OUT / latest.name
        if latest.resolve() != dest.resolve():
            dest.write_bytes(latest.read_bytes())

    # Analyze capture if present
    capture_in_run = next(OUT.glob("full_capture_*.json"), None)
    if capture_in_run:
        analyze = run([sys.executable, "playtest_analyze.py"], timeout=60)
        (OUT / "analyze.log").write_text(analyze["stdout"] + analyze["stderr"], encoding="utf-8")
        src_analysis = ROOT / "playtest_results" / "analysis_report.json"
        if src_analysis.exists():
            (OUT / "analysis_report.json").write_bytes(src_analysis.read_bytes())

    # Surface the scan runs' verdicts (and where their frames/GIFs landed)
    # in the master report, so the interaction coverage is readable without
    # opening each run folder.
    report["scan_runs"] = []
    for scan_dir in sorted(OUT.glob("scan_run_*")):
        session_json = scan_dir / "session.json"
        if not session_json.exists():
            continue
        try:
            data = json.loads(session_json.read_text(encoding="utf-8"))
        except Exception:
            continue
        report["scan_runs"].append({
            "folder": str(scan_dir),
            "plan": data.get("plan"),
            "detect_backend": data.get("detect_backend"),
            "gif": data.get("gif"),
            "frames": len(list((scan_dir / "frames").glob("*.png"))),
            "verdict": data.get("verdict"),
        })

    passed = sum(1 for s in report["steps"] if s["ok"])
    failed = [s["name"] for s in report["steps"] if not s["ok"]]
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["summary"] = {"passed": passed, "failed": len(failed), "failed_steps": failed}

    master = OUT / "master_report.json"
    master.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"FULL PLAYTEST RUN: {passed}/{len(report['steps'])} steps passed")
    for scan in report["scan_runs"]:
        v = scan.get("verdict") or {}
        print(f"  scan: {Path(scan['folder']).name} "
              f"objects={v.get('total_objects_detected')} "
              f"selected={', '.join(v.get('unique_objects_selected') or []) or '(none)'} "
              f"gif={'yes' if scan.get('gif') else 'no'}")
    print(f"Output: {OUT}")
    if failed:
        print("Failed:", ", ".join(failed))
    print("=" * 60)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
