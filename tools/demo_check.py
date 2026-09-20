"""Make the FIRST run of a demo behave like the third one.

"When I try and demo the game, it always has bugs or is broken... after a few
rounds it seems to work normally."

That pattern is not superstition and it is not the model warming up. A first run
inherits things: the previous playthrough's last frame, a fight that never
closed, a World whose snapshot carries somebody else's character sheet, a
tunable a test run blanked. By round three all of it has been overwritten by the
live run, which is exactly why it "settles down".

So this checks the things that have actually broken a first run in this repo,
each one traceable to a real incident, then CLEARS the state a run inherits,
then BOOTS the game and watches the opening happen. A report that says PASS
without having seen a montage play is the kind of green light this project has
been burned by before (see docs/operations/TESTING_USE_THIS.md).

    python tools/demo_check.py              # check + clean, no boot
    python tools/demo_check.py --boot       # ...and prove it by playing it
    python tools/demo_check.py --no-clean   # report only, touch nothing

Exit code is 0 only when every check passed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []
FIXED: list[str] = []


def ok(msg):
    print(f"  [ ok ] {msg}")


def bad(msg):
    FAILED.append(msg)
    print(f"  [FAIL] {msg}")


def fixed(msg):
    FIXED.append(msg)
    print(f"  [fixd] {msg}")


def note(msg):
    print(f"         {msg}")


# ── 1. can it generate at all ────────────────────────────────────────────────
def check_keys():
    """No key is not a crash, it is MOCK MODE — the loop runs, the prose is
    canned and no images are generated. A demo of that looks broken."""
    print("\nKEYS")
    env = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    key = env.get("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY") or ""
    if len(key) > 20:
        ok(f"GEMINI_API_KEY present ({len(key)} chars)")
    else:
        bad("no GEMINI_API_KEY — the demo will run in mock mode: canned prose, "
            "no pictures")


# ── 2. who the player is ─────────────────────────────────────────────────────
def check_character():
    """The 09-18 demo drew a stranger: the run was bound to a World whose
    snapshot still carried the shipped default sheet, with no plate."""
    print("\nWHO THE PLAYER IS")
    import game_identity as gi

    spec = gi.get_spec()
    char = spec.get(gi.CHARACTER_KEY) or {}
    shipped = json.loads(
        (ROOT / "prompts" / "simulation_prompts.defaults.json")
        .read_text(encoding="utf-8")).get(gi.CHARACTER_KEY) or {}

    look = str(char.get("appearance") or "").strip()
    if not look:
        bad("the live Character sheet has no appearance")
    elif look == str(shipped.get("appearance") or "").strip():
        bad("the live Character sheet IS the shipped default — a World bind or "
            "a test run has overwritten it "
            "(tools/restore_polluted_blocks.py player_character --apply)")
    else:
        ok(f"character: {gi.display_name(spec)} — {look[:58]}")

    plates = gi.character_reference_paths(spec)
    if not plates:
        bad("no character reference plate — every frame is a guess at who they "
            "are")
    else:
        missing = [p for p in plates if not Path(p).exists()]
        if missing:
            bad(f"the character plate is NAMED but not on disk: "
                f"{[Path(p).name for p in missing]}")
        else:
            ok(f"character plate on disk ({Path(plates[0]).name})")


# ── 3. where the level is, and what it is for ────────────────────────────────
def check_level():
    """A blank Level sheet is what produced 'toward (no goal authored)' and a
    montage with shots=0."""
    print("\nWHERE THE LEVEL IS")
    import game_identity as gi

    setting = gi.authored_setting()
    if not setting.get("enabled"):
        bad("the Level sheet is switched OFF — the opening montage has nothing "
            "to establish")
        return
    for field in ("name", "summary"):
        if str(setting.get(field) or "").strip():
            ok(f"level {field}: {str(setting[field])[:56]}")
        else:
            bad(f"level {field} is empty")
    goal = str(gi.level_goal(fallback=False) or "").strip()
    if goal:
        ok(f"goal authored: {goal[:56]}")
    else:
        note("no goal authored — each run drafts its own (engine._goal_for_this_run)")


# ── 4. the Worlds you can pick from the menu ─────────────────────────────────
def check_worlds():
    """Binding a World installs ITS copy of the sheets as live. A World left
    carrying the factory character replaces yours on the way into the run —
    which is what happened mid-demo on 09-18."""
    print("\nTHE WORLDS THE PICKER OFFERS")
    import flipbook
    import game_identity as gi

    shipped = json.loads(
        (ROOT / "prompts" / "simulation_prompts.defaults.json")
        .read_text(encoding="utf-8"))
    ship_look = str((shipped.get(gi.CHARACTER_KEY) or {}).get("appearance") or "")

    stale_char, stale_flip = [], []
    for path in sorted((ROOT / "worlds").glob("*.json")):
        if path.name.endswith(".frame.json"):
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            bad(f"{path.name} is unreadable")
            continue
        prompts = doc.get("prompts") or {}
        char = prompts.get(gi.CHARACTER_KEY) or {}
        look = str(char.get("appearance") or "").strip()
        if look and look == ship_look.strip() and not char.get("reference_images"):
            stale_char.append(path.name)
        for key, text in prompts.items():
            if "flipbook" in key.lower() and isinstance(text, str):
                if any(flipbook.prefix_is_stale(text, n)
                       for n in flipbook.FRAME_COUNTS):
                    stale_flip.append(path.name)
                    break

    if stale_char:
        bad(f"{len(stale_char)} World(s) still carry the factory character and "
            f"would overwrite yours when picked: {', '.join(stale_char[:4])} "
            f"(tools/sync_character_into_worlds.py --apply)")
    else:
        ok("every World carries an authored character")
    if stale_flip:
        bad(f"{len(stale_flip)} World(s) carry a stale flipbook prompt: "
            f"{', '.join(stale_flip[:4])} "
            f"(tools/retire_stale_flipbook_prompts.py --apply)")
    else:
        ok("no stale flipbook prompts")


# ── 5. the settings a test run can blank ─────────────────────────────────────
def check_tunables():
    """tunables.json is gitignored, so when a test run wrote {} over it there
    was nothing to restore from — and Flipbook silently fell back to its schema
    default of OFF, turning every animated turn into a still with no error."""
    print("\nSETTINGS")
    path = ROOT / "tunables.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            bad("tunables.json is not readable JSON")
            return
    if not data:
        bad("tunables.json is empty — every knob is back to its schema default "
            "(Flipbook OFF)")
        return
    if data.get("flipbook_enabled"):
        ok("flipbook is ON")
    else:
        note("flipbook is off (fine if that is deliberate)")

    cfg = json.loads((ROOT / "ai_config.json").read_text(encoding="utf-8"))
    if cfg.get("image_provider") == "gemini":
        ok(f"image provider: {cfg.get('image_provider')}/{cfg.get('image_model')}")
    elif data.get("flipbook_enabled"):
        bad(f"flipbook is ON but the image provider is "
            f"{cfg.get('image_provider')!r} — flipbook is Gemini only, so every "
            f"turn will silently be a still")


# ── 6. what the next run would INHERIT ───────────────────────────────────────
def check_and_clean_sessions(clean: bool):
    """The actual "first run is broken, third run is fine" mechanism. A session
    carries the last playthrough's frame, feed, history — and, if a fight was
    interrupted, an `encounter` block that opens the new run inside it."""
    print("\nWHAT THE NEXT RUN WOULD INHERIT")
    sessions = ROOT / "sessions"
    if not sessions.is_dir():
        ok("no sessions on disk — the next run starts clean")
        return

    dirty = []
    for state_path in sessions.glob("*/state.json"):
        try:
            st = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            dirty.append((state_path.parent.name, "unreadable state"))
            continue
        name = state_path.parent.name
        if st.get("encounter"):
            dirty.append((name, "an encounter left open"))
        elif st.get("encounter_resolving"):
            dirty.append((name, "a fight mid-resolve"))
        elif st.get("turn_count"):
            dirty.append((name, f"{st['turn_count']} turns of a previous run"))
        elif st.get("feed_log"):
            dirty.append((name, "a previous run's feed"))

    if not dirty:
        ok("no session carries a previous run")
    else:
        for name, why in dirty:
            note(f"{name}: {why}")
        if not clean:
            bad(f"{len(dirty)} session(s) would be inherited by the demo "
                f"(re-run without --no-clean)")
        else:
            for name, _ in dirty:
                shutil.rmtree(sessions / name, ignore_errors=True)
            fixed(f"cleared {len(dirty)} session(s): "
                  f"{', '.join(n for n, _ in dirty)}")

    cache = ROOT / ".cache"
    if cache.is_dir() and clean:
        n = sum(1 for _ in cache.rglob("*") if _.is_file())
        if n:
            shutil.rmtree(cache, ignore_errors=True)
            fixed(f"cleared .cache ({n} file(s): vision reads, replays)")


# ── 7. prove it ──────────────────────────────────────────────────────────────
def boot_and_watch(port: int = 9333, timeout: int = 260) -> bool:
    """Start the real app and watch the opening actually happen.

    Everything above is a file check. This is the part that answers the question
    the demo asks: does a montage play, does a picture arrive, are there choices
    to press.
    """
    print("\nPLAYING IT")
    log = ROOT / "logs" / "demo_check.log"
    log.parent.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"--remote-debugging-port={port}"
    proc = subprocess.Popen(
        [sys.executable, "play.py"], cwd=str(ROOT), env=env,
        stdout=log.open("w", encoding="utf-8"), stderr=subprocess.STDOUT)
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        bad("playwright is not installed — cannot watch the boot")
        proc.terminate()
        return False

    try:
        with sync_playwright() as pw:
            page = None
            for _ in range(30):
                time.sleep(2.0)
                try:
                    b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                    pages = [p for p in b.contexts[0].pages if "standalone" in p.url]
                    if pages:
                        page = pages[0]
                        break
                except Exception:
                    continue
            if page is None:
                bad("the app never came up on the debug port")
                return False
            ok("the app is up")

            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)[:160]))
            sys.path.insert(0, str(ROOT))
            from playtest_app import STATE, analyse, is_black, start_run

            if not start_run(page, note):
                bad("the run never reached a playable turn")
                return False

            s = page.evaluate(STATE)
            mean, dark = analyse(page.screenshot())
            if is_black(mean, dark):
                bad(f"the first frame is BLACK (luma {mean})")
            else:
                ok(f"a picture is on screen (luma {mean})")
            if s["prose"]:
                ok("prose arrived")
            else:
                bad("no prose on the first turn")

            choices = page.evaluate(
                "() => document.querySelectorAll('.choice-btn:not(.choice-btn-custom)')"
                ".length")
            if choices:
                ok(f"{choices} choices to press")
            else:
                bad("no choices on the first turn — nothing to demo")

            text = log.read_text(encoding="utf-8", errors="replace")
            # The montage is unpeopled by instruction, so the first playable
            # frame is where the player first sees THEMSELVES — and it is drawn
            # from montage panels containing nobody. If the character plate does
            # not reach that render the model invents a person out of the
            # bible's "you are a photojournalist", and a stranger opens the
            # game. That was silent: the reference list showed two panels and a
            # layout guide, and nothing said the plate was missing.
            if "NO identity plate" in text:
                bad("the first playable frame was drawn with NO character "
                    "plate — the person in it is invented, not your character")
            elif "identity plate(s):" in text:
                ok("the character plate reached the first playable frame")

            shots = [ln for ln in text.splitlines() if "shots=" in ln]
            if any("shots=0" in ln for ln in shots):
                bad("the opening montage staged ZERO shots — it will not play")
            elif shots:
                ok("the opening montage played")
            if "[GOAL]" in text or "toward '" in text:
                for ln in text.splitlines():
                    if "establishing montage for" in ln:
                        if "(no goal authored)" in ln:
                            bad("the montage is heading toward nothing")
                        else:
                            ok("the montage has a goal to head toward")
                        break
            real = [e for e in errors if "402" not in e]
            if real:
                bad(f"client error on boot: {real[0]}")
            else:
                ok("no client errors")
            return not FAILED
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--boot", action="store_true",
                    help="start the game and watch the opening")
    ap.add_argument("--no-clean", action="store_true",
                    help="report only; do not delete anything")
    args = ap.parse_args()

    print("=" * 66)
    print("DEMO PREFLIGHT")
    print("=" * 66)
    for fn in (check_keys, check_character, check_level, check_worlds,
               check_tunables):
        try:
            fn()
        except Exception as err:
            bad(f"{fn.__name__} could not run: {err}")
    try:
        check_and_clean_sessions(clean=not args.no_clean)
    except Exception as err:
        bad(f"session check failed: {err}")

    if args.boot and not FAILED:
        try:
            boot_and_watch()
        except Exception as err:
            bad(f"the boot check could not run: {err}")
        # The proof run is itself a previous run now. Leaving it behind would
        # hand the demo exactly the inheritance this tool exists to remove —
        # the check would pass and then cause the thing it checked for.
        if not args.no_clean:
            left = [p.parent for p in (ROOT / "sessions").glob("*/state.json")]
            for path in left:
                shutil.rmtree(path, ignore_errors=True)
            if left:
                fixed(f"cleared the proof run ({', '.join(p.name for p in left)}) "
                      f"— the demo gets a genuinely first run")
    elif args.boot:
        print("\nPLAYING IT\n         skipped — fix the failures above first")

    print("\n" + "=" * 66)
    if FIXED:
        print(f"CLEANED: {len(FIXED)}")
        for f in FIXED:
            print(f"  - {f}")
    if FAILED:
        print(f"NOT READY — {len(FAILED)} problem(s):")
        for f in FAILED:
            print(f"  - {f}")
    else:
        print("READY" + ("" if args.boot else " (add --boot to prove it)"))
    print("=" * 66)
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
