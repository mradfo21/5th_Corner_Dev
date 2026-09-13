#!/usr/bin/env python3
"""prompt_probe.py - record every prompt the turn loop actually sends.

Runs the real two-phase turn pipeline in-process against the real providers,
with requests.post wrapped so each outbound call is classified and its full
prompt text captured on the way past. Uses whatever world is currently authored
(it does not write to prompts/simulation_prompts.json).

    python tools/prompt_probe.py --turns 4 --out playtest_results/prompt_probe

Writes report.txt (surface order + sizes + budget analysis), calls.json and one
file per call under prompts/.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

CALLS: list[dict] = []
TURN = {"n": 0}


def _classify(body: str) -> str:
    b = body
    if "SPATIAL:" in b and "SETTING:" in b and "Analyze this image" in b:
        return "vision_analyze"
    if "Rewrite the ENTIRE world_prompt" in b:
        return "world_evolution"
    if "Extract the SINGLE MOST SIGNIFICANT change" in b:
        return "evolution_summary"
    if "extract a list of significant physical entities" in b:
        return "entity_extraction"
    if "visual_scene" in b and "player_alive" in b:
        return "consequence"
    if "PHYSICAL ACTION CHOICES" in b or "physical action choices" in b.lower():
        return "choices"
    if "encounter" in b.lower() and "lane" in b.lower():
        return "encounter"
    if "detect" in b.lower() and "bounding" in b.lower():
        return "detect"
    return "other"


def install_recorder() -> None:
    import requests
    real_post = requests.post

    def post(url, *a, **kw):
        payload = kw.get("json") or {}
        parts, n_images = [], 0
        try:
            for c in payload.get("contents", []):
                for p in c.get("parts", []):
                    if isinstance(p, dict):
                        if "text" in p:
                            parts.append(p["text"])
                        if "inlineData" in p or "inline_data" in p:
                            n_images += 1
        except Exception:
            pass
        body = "\n".join(parts)
        kind = _classify(body) if body else "non_gemini"
        t0 = time.time()
        resp = real_post(url, *a, **kw)
        reply = ""
        try:
            d = resp.json()
            for c in (d.get("candidates") or []):
                for p in ((c.get("content") or {}).get("parts") or []):
                    if isinstance(p.get("text"), str):
                        reply += p["text"]
        except Exception:
            pass
        if body:
            CALLS.append({
                "turn": TURN["n"], "kind": kind,
                "model": url.split("/models/")[-1].split(":")[0] if "/models/" in url else url,
                "status": getattr(resp, "status_code", None),
                "ms": int((time.time() - t0) * 1000),
                "prompt": body, "prompt_chars": len(body),
                "max_tokens": (payload.get("generationConfig") or {}).get("maxOutputTokens"),
                "n_images_attached": n_images,
                "reply": reply, "reply_chars": len(reply),
            })
        return resp

    requests.post = post


def install_image_recorder() -> None:
    import gemini_image_utils as g
    real_t2i, real_i2i = g.generate_with_gemini, g.generate_gemini_img2img

    def t2i(*a, **kw):
        prompt = a[0] if a else kw.get("prompt", "")
        t0 = time.time()
        out = real_t2i(*a, **kw)
        CALLS.append({"turn": TURN["n"], "kind": "image_t2i", "model": "gemini-image",
                      "ms": int((time.time() - t0) * 1000), "prompt": prompt,
                      "prompt_chars": len(prompt), "refs": [], "n_images_attached": 0,
                      "reply": str(out), "reply_chars": 0,
                      "status": 200 if out else 0, "max_tokens": None})
        return out

    def i2i(*a, **kw):
        prompt = a[0] if a else kw.get("prompt", "")
        raw = a[2] if len(a) > 2 else kw.get("reference_image_path")
        refs = raw if isinstance(raw, list) else ([raw] if raw else [])
        t0 = time.time()
        out = real_i2i(*a, **kw)
        CALLS.append({"turn": TURN["n"], "kind": "image_i2i", "model": "gemini-image",
                      "ms": int((time.time() - t0) * 1000), "prompt": prompt,
                      "prompt_chars": len(prompt),
                      "refs": [os.path.basename(str(r)) for r in refs],
                      "n_images_attached": len(refs),
                      "reply": str(out), "reply_chars": 0,
                      "status": 200 if out else 0, "max_tokens": None})
        return out

    g.generate_with_gemini = t2i
    g.generate_gemini_img2img = i2i


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=4)
    ap.add_argument("--out", default="playtest_results/prompt_probe")
    ap.add_argument("--session", default="promptprobe")
    args = ap.parse_args()

    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)

    # Same key discovery play.py uses, so the probe never runs half-offline.
    import run_local
    for cand in (ROOT / ".env", Path.cwd() / ".env"):
        if cand.is_file():
            run_local._load_env_file(cand)
            print(f"[probe] keys from {cand}")
            break

    install_recorder()
    import engine
    install_image_recorder()
    engine.GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    import choices as _choices
    _choices.GEMINI_API_KEY = engine.GEMINI_API_KEY

    sess = args.session
    shutil.rmtree(engine._get_session_root(sess), ignore_errors=True)
    engine.set_active_session(sess)

    print(f"[probe] reset {sess!r}", flush=True)
    engine.reset_state(sess)
    TURN["n"] = 0
    engine.generate_intro_turn(sess)

    transcript = []
    for t in range(1, args.turns + 1):
        TURN["n"] = t
        st = engine._load_state(sess)
        hist = engine._load_history(sess)
        slate = st.get("last_choices") or []
        pick = slate[0] if slate else "Move deeper into the facility"
        print(f"[probe] === turn {t}: {pick!r} ===", flush=True)
        dyn = engine.advance_story_dynamics(session_id=sess)
        p1 = engine.advance_turn_image_fast(
            pick, fate=dyn.get("fate", "NORMAL"), session_id=sess,
            skip_image=True, skip_evolve=False, local_only=True)
        img = engine._generate_and_append_scene_image(
            p1.get("vision_dispatch", ""), p1.get("dispatch", ""), pick,
            p1.get("frame_idx", len(hist) + 1),
            st.get("world_prompt", ""), session_id=sess,
            hard_transition=p1.get("hard_transition", False), write_history=True)
        p2 = engine.advance_turn_choices_deferred(
            (img or {}).get("web_url"), p1.get("dispatch", ""),
            p1.get("vision_dispatch", ""), pick,
            (img or {}).get("image_prompt", ""), p1.get("hard_transition", False),
            session_id=sess, local_only=True,
            pregenerated_choices=p1.get("provisional_choices") or None)
        st2 = engine._load_state(sess)
        h2 = engine._load_history(sess)
        transcript.append({
            "turn": t, "choice": pick,
            "dispatch": p1.get("dispatch"),
            "visual_scene": p1.get("vision_dispatch"),
            "degraded": p1.get("degraded"),
            "phase": p1.get("phase"), "threat": st2.get("threat_level"),
            "choices": p2.get("choices"),
            "hard_transition": p1.get("hard_transition"),
            "world_prompt_chars": len(st2.get("world_prompt") or ""),
            "evolution_summary": st2.get("evolution_summary"),
            "seen_elements": st2.get("seen_elements"),
            "image": (img or {}).get("web_url"),
            "hist_image": (h2[-1].get("image") if h2 else None),
            "hist_guide_image": (h2[-1].get("guide_image") if h2 else None),
            "hist_vision_analysis": (h2[-1].get("vision_analysis") if h2 else None),
        })

    (out / "calls.json").write_text(json.dumps(CALLS, indent=2), encoding="utf-8")
    (out / "transcript.json").write_text(json.dumps(transcript, indent=2), encoding="utf-8")

    wp = (engine._load_state(sess).get("world_prompt") or "")
    try:
        import experience_store
        lore_chars = len(experience_store.lore_brief())
    except Exception:
        lore_chars = 0

    L = []
    L.append("=" * 80)
    L.append("PER-TURN SURFACE ORDER (prompt size / output budget / attachments)")
    L.append("=" * 80)
    for t in range(0, args.turns + 1):
        tc = [c for c in CALLS if c["turn"] == t]
        if not tc:
            continue
        L.append(f"\n--- turn {t} ({len(tc)} calls) ---")
        for c in tc:
            extra = f"  refs={c.get('refs')}" if c["kind"].startswith("image") else ""
            L.append(f"  {c['kind']:<17} prompt={c['prompt_chars']:>6}c "
                     f"out_budget={str(c.get('max_tokens')):>5} reply={c['reply_chars']:>5}c "
                     f"imgs={c['n_images_attached']} {c['ms']:>6}ms{extra}")

    L.append("\n" + "=" * 80)
    L.append("WORLD DOCUMENT COMPOSITION")
    L.append("=" * 80)
    L.append(f"  world_prompt total      : {len(wp)} chars")
    L.append(f"  static lore brief       : {lore_chars} chars "
             f"({(100*lore_chars/len(wp)) if wp else 0:.0f}% of the document)")
    L.append(f"  'Jason' mentions        : {wp.lower().count('jason')}")
    L.append(f"  'Wren' mentions         : {wp.lower().count('wren')}")

    L.append("\n" + "=" * 80)
    L.append("TRANSCRIPT - what the player reads")
    L.append("=" * 80)
    for e in transcript:
        L.append(f"\n[turn {e['turn']}] > {e['choice']}")
        L.append(f"  phase={e['phase']} threat={e['threat']} hard_cut={e['hard_transition']} "
                 f"degraded={e['degraded']}")
        L.append(f"  dispatch : {e['dispatch']}")
        L.append(f"  visual   : {e['visual_scene']}")
        L.append(f"  choices  : {e['choices']}")
        L.append(f"  evo      : {e['evolution_summary']}")
        L.append(f"  world_prompt={e['world_prompt_chars']}c")
        L.append(f"  hist.image      ={e['hist_image']}")
        L.append(f"  hist.guide_image={e['hist_guide_image']}")
        L.append(f"  vision   : {(e['hist_vision_analysis'] or '')[:220]}")

    (out / "report.txt").write_text("\n".join(L), encoding="utf-8")

    pd = out / "prompts"
    shutil.rmtree(pd, ignore_errors=True)
    pd.mkdir(parents=True)
    for i, c in enumerate(CALLS):
        (pd / f"t{c['turn']:02d}_{i:03d}_{c['kind']}.txt").write_text(
            f"### PROMPT ({c['prompt_chars']}c, out_budget={c.get('max_tokens')}, "
            f"images_attached={c['n_images_attached']})\n{c['prompt']}\n\n"
            f"### REPLY ({c['reply_chars']}c)\n{c.get('reply','')}\n", encoding="utf-8")

    print("\n".join(L))
    print(f"\n[probe] wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
