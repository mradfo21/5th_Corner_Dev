#!/usr/bin/env python3
"""Replay recorded runs through the old and new location-change rules.

Free, and decisive: every choice a real playthrough actually made, scored twice.
The number that matters is how many PORTAL crossings — doors, hatches, gateways
— were refused a fresh composition. A refused portal cannot be rendered
honestly: the reference frame holds the near side and the prose says the player
is on the far side, so the image contradicts the prose and puts them back where
they came from.

Run: python tools/replay_transitions.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine  # noqa: E402


def old_throttle(state: dict, wants_cut: bool, dispatch: str) -> bool:
    """The rule as it shipped: no grade, and a refusal HOLDS the count."""
    run = int(state.get("n", 0) or 0)
    if not wants_cut:
        state["n"] = 0
        return False
    if (run >= engine.MAX_CONSECUTIVE_HARD_TRANSITIONS
            and not engine.narrative_opened_new_space(dispatch)):
        return False
    state["n"] = run + 1
    return True


def replay(path: Path) -> dict | None:
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(history, list) or not history:
        return None

    old_state, new_state = {}, {}
    rows = []
    for turn in history:
        choice = str(turn.get("choice") or "")
        dispatch = str(turn.get("dispatch") or "")
        if not choice:
            continue
        wants = engine.is_hard_transition(choice, dispatch)
        kind = engine.transition_kind(choice)
        was = old_throttle(old_state, wants, dispatch)
        now = engine.throttle_hard_transition(new_state, wants, dispatch, kind)
        rows.append({"choice": choice, "wants": wants, "kind": kind,
                     "old": was, "new": now})
    return {"session": path.parent.name, "rows": rows}


def main() -> int:
    # Quiet: is_hard_transition narrates every hit, and this calls it hundreds
    # of times. The report is the output, not the running commentary.
    import io
    import contextlib

    sessions = sorted((ROOT / "sessions").glob("*/history.json"))
    reports = []
    with contextlib.redirect_stdout(io.StringIO()):
        for path in sessions:
            r = replay(path)
            if r and r["rows"]:
                reports.append(r)

    total = {"turns": 0, "wanted": 0, "portal_refused_old": 0,
             "portal_refused_new": 0, "approach_refused_new": 0}
    print(f"{'session':<26} {'turns':>5} {'moves':>6} {'refused(old)':>13} "
          f"{'refused(new)':>13}  portals wrongly refused")
    print("-" * 96)
    worst = []
    for r in reports:
        rows = r["rows"]
        wanted = [x for x in rows if x["wants"]]
        old_ref = [x for x in wanted if not x["old"]]
        new_ref = [x for x in wanted if not x["new"]]
        portal_old = [x for x in old_ref if x["kind"] == "portal"]
        portal_new = [x for x in new_ref if x["kind"] == "portal"]
        total["turns"] += len(rows)
        total["wanted"] += len(wanted)
        total["portal_refused_old"] += len(portal_old)
        total["portal_refused_new"] += len(portal_new)
        total["approach_refused_new"] += len([x for x in new_ref
                                              if x["kind"] == "approach"])
        print(f"{r['session'][:26]:<26} {len(rows):>5} {len(wanted):>6} "
              f"{len(old_ref):>13} {len(new_ref):>13}  {len(portal_old)}")
        worst.extend(portal_old)

    print("\n" + "=" * 96)
    print(f"across {len(reports)} recorded runs, {total['turns']} turns:")
    print(f"  turns that asked to change location : {total['wanted']}")
    print(f"  refused, old rule                   : "
          f"{sum(1 for r in reports for x in r['rows'] if x['wants'] and not x['old'])}")
    print(f"  refused, new rule                   : "
          f"{sum(1 for r in reports for x in r['rows'] if x['wants'] and not x['new'])}")
    print(f"  DOORWAYS refused, old rule          : {total['portal_refused_old']}"
          f"   <- each one renders the player back where they came from")
    print(f"  DOORWAYS refused, new rule          : {total['portal_refused_new']}")
    print(f"  approaches still softened, new rule : {total['approach_refused_new']}"
          f"   <- the throttle still does its job")

    # Be honest about WHY the approach column is what it is. If these runs
    # simply never made an approach, "the throttle still works" is a claim this
    # data cannot support, and the reader should be able to see that.
    kinds = {}
    for r in reports:
        for x in r["rows"]:
            if x["wants"]:
                kinds[x["kind"]] = kinds.get(x["kind"], 0) + 1
    print(f"\n  of the {total['wanted']} location changes, by grade: "
          + ", ".join(f"{k or 'ungraded'}={v}" for k, v in sorted(kinds.items())))
    if not kinds.get("approach"):
        print("  (no approach-graded turns in this data — the scan_move harness "
              "prefers enterable targets, so it clicks doors. The approach branch "
              "is exercised by unit tests, not by these runs.)")

    if worst:
        print(f"\nthe doorways that were refused (first 12):")
        seen = set()
        for x in worst:
            c = x["choice"][:86]
            if c in seen:
                continue
            seen.add(c)
            print(f"   {c}")
            if len(seen) >= 12:
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
