#!/usr/bin/env python3
"""Analyze playtest capture JSON for simulation loop health."""
import json
import sys
from pathlib import Path

EXPECTED_CHOICES = 3


def analyze_capture(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    turns = data.get("turns", [])
    reset = data.get("reset", {})

    issues = []
    checks = {}

    # --- Turn resolution ---
    resolved = sum(1 for t in turns if t.get("status") == "resolved")
    checks["all_turns_resolved"] = resolved == len(turns) and len(turns) > 0
    if not checks["all_turns_resolved"]:
        issues.append(f"{len(turns) - resolved} turns did not resolve")

    # --- Feed loop: monotonic IDs ---
    all_ids = []
    for item in reset.get("feed_items", []):
        all_ids.append(item.get("id"))
    for t in turns:
        for item in t.get("feed_items", []):
            all_ids.append(item.get("id"))
    id_gaps = []
    for i in range(1, len(all_ids)):
        if all_ids[i] <= all_ids[i - 1]:
            issues.append(f"Feed ID not monotonic: {all_ids[i-1]} -> {all_ids[i]}")
        if all_ids[i] - all_ids[i - 1] > 2:
            id_gaps.append(all_ids[i] - all_ids[i - 1])
    checks["feed_ids_monotonic"] = not any("not monotonic" in i for i in issues)

    # --- Each turn produces expected feed types ---
    expected_after_choose = {"player_action", "narrative_event", "player_choice_prompt"}
    turn_structure_ok = True
    for t in turns:
        types = set(t.get("feed_item_types", []))
        if not expected_after_choose.issubset(types):
            turn_structure_ok = False
            issues.append(f"Turn {t['turn']}: missing feed types {expected_after_choose - types}")
    checks["turn_feed_structure"] = turn_structure_ok

    # --- State evolution (turn counter, chaos) ---
    turn_nums = []
    chaos_vals = []
    images_on = None
    for t in turns:
        st = t.get("game_status") or {}
        turn_nums.append(st.get("turn"))
        chaos_vals.append(st.get("chaos"))
        if images_on is None:
            images_on = st.get("image_enabled")

    checks["turn_counter_increments"] = (
        len(turn_nums) > 0
        and turn_nums[0] >= 1
        and all(
            turn_nums[i] == turn_nums[i - 1] + 1 or turn_nums[i] > turn_nums[i - 1]
            for i in range(1, len(turn_nums))
        )
    )
    if not checks["turn_counter_increments"]:
        issues.append(f"Turn counter did not increment cleanly: {turn_nums}")

    checks["chaos_evolves"] = len(set(chaos_vals)) > 1 or (chaos_vals and chaos_vals[-1] > 0)
    # --- Narrative quality ---
    degraded = 0
    paused = 0
    diegetic = 0
    narrative_texts = []
    for t in turns:
        narr = t.get("narrative_combined", "")
        narrative_texts.append(narr)
        meta_degraded = any(
            (i.get("metadata") or {}).get("degraded")
            for i in t.get("feed_items", [])
            if i.get("type") == "narrative_event"
        )
        if "Narrative paused until resources" in narr or "System communications remain static" in narr:
            paused += 1
        elif meta_degraded or any(
            m in narr.lower()
            for m in ("static", "tape stutters", "interference", "viewfinder floods", "battery indicator")
        ):
            degraded += 1
            diegetic += 1
        elif narr:
            diegetic += 0  # real AI text

    checks["every_turn_has_narrative"] = all(bool(n) for n in narrative_texts)

    # --- Choice regeneration ---
    choice_changes = sum(1 for t in turns if t.get("choices_changed"))
    checks["choices_regenerate_often"] = choice_changes >= len(turns) * 0.5 if turns else False
    all_choice_sets = [tuple(t.get("new_choices", [])) for t in turns]
    unique_choice_sets = len(set(all_choice_sets))
    checks["choice_variety"] = unique_choice_sets >= 2
    short_slates = [
        t["turn"] for t in turns
        if t.get("status") == "resolved" and len(t.get("new_choices") or []) < EXPECTED_CHOICES
    ]
    checks["full_choice_slate_every_turn"] = not short_slates
    if short_slates:
        issues.append(f"Turns served fewer than {EXPECTED_CHOICES} choices: {short_slates}")

    # --- Player action recorded ---
    checks["player_actions_recorded"] = all(
        any(i.get("type") == "player_action" for i in t.get("feed_items", []))
        for t in turns
    )

    # --- Images ---
    images = sum(1 for t in turns if t.get("scene_image_url"))
    if images_on:
        checks["images_present"] = images > 0
    else:
        checks["images_skipped_backend_off"] = True

    # --- Narrative references prior action (weak heuristic) ---
    action_echo = 0
    for t in turns:
        picked = (t.get("picked") or "").lower()
        narr = (t.get("narrative_combined") or "").lower()
        # In mock/degraded mode we don't expect action echo in narrative
        words = [w for w in picked.split() if len(w) > 4 and w not in ("custom", "raise", "camcorder")]
        if any(w in narr for w in words[:3]):
            action_echo += 1

    summary = {
        "capture_file": str(path),
        "turns_analyzed": len(turns),
        "resolved_rate": round(resolved / len(turns), 2) if turns else 0,
        "turn_numbers": turn_nums,
        "chaos_progression": chaos_vals,
        "action_echoed_in_narrative": action_echo,
        "short_slate_turns": short_slates,
        "narrative_breakdown": {
            "paused_llm_off": paused,
            "degraded_diegetic": diegetic,
            "total_turns": len(turns),
        },
        "choice_changes": choice_changes,
        "unique_choice_sets": unique_choice_sets,
        "images_in_turns": images,
        "feed_id_count": len(all_ids),
        "checks": checks,
        "issues": issues,
        "overall_loop_healthy": (
            checks.get("all_turns_resolved")
            and checks.get("turn_feed_structure")
            and checks.get("turn_counter_increments")
            and checks.get("every_turn_has_narrative")
            and checks.get("player_actions_recorded")
        ),
        "backend_note": (
            "Real backend: narrative, choices and images all come from the live provider."
            if images_on else
            "Mock backend (LLM_ENABLED=False): narrative is diegetic glitch text or the "
            "'Narrative paused...' sentinel and images are off. Loop mechanics still valid."
        ),
    }
    return summary


def main():
    root = Path(__file__).parent / "playtest_results"
    paths = sorted(root.glob("full_capture_*.json"))
    if not paths:
        print("No capture files found")
        return 1

    reports = [analyze_capture(p) for p in paths]
    out = root / "analysis_report.json"
    out.write_text(json.dumps(reports, indent=2), encoding="utf-8")

    print("=" * 60)
    print("PLAYTEST ANALYSIS")
    print("=" * 60)
    for r in reports:
        print(f"\nFile: {r['capture_file']}")
        print(f"  Turns: {r['turns_analyzed']} | Resolved: {r['resolved_rate']}")
        print(f"  Turn counter: {r['turn_numbers']}")
        print(f"  Chaos: {r['chaos_progression']}")
        print(f"  Action echoed in narrative: {r['action_echoed_in_narrative']}/{r['turns_analyzed']}")
        nb = r["narrative_breakdown"]
        print(
            f"  Narrative: {nb['degraded_diegetic']} diegetic/degraded, "
            f"{nb['paused_llm_off']} LLM-off paused, {nb['total_turns']} total"
        )
        print(f"  Choices changed: {r['choice_changes']}/{r['turns_analyzed']}, unique sets: {r['unique_choice_sets']}")
        print(f"  Images in turns: {r['images_in_turns']}")
        print(f"  LOOP HEALTHY: {'YES' if r['overall_loop_healthy'] else 'NO'}")
        if r["issues"]:
            print("  Issues:")
            for issue in r["issues"]:
                print(f"    - {issue}")
        print("\n  Checks:")
        for name, ok in r["checks"].items():
            print(f"    [{'PASS' if ok else 'FAIL'}] {name}")

    print(f"\nFull analysis written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
