"""Play the REAL app through its real controls and report what a player sees.

The turn interface is the action wheel plus scanned hotspots, NOT a list of
text buttons: SCAN paints tags on the picture, clicking a tag opens its
sub-actions (MOVE TO / INTERACT / TALK), and the sub-action is what commits
the turn. A harness that only looks at #choices thinks a healthy run is stuck.

Per turn it records: how long the turn took, whether the picture went black,
whether the prose actually advanced, and any client console error.
"""
import io
import json
import os
import re
import sys
import time

from PIL import Image
from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SHOTS = "_playthrough"
os.makedirs(SHOTS, exist_ok=True)
TURNS = int(os.environ.get("PT_TURNS", "8"))
TURN_TIMEOUT = int(os.environ.get("PT_TURN_TIMEOUT", "90"))


def a(s):
    return "".join(c if 32 <= ord(c) < 127 else "?" for c in (s or ""))


STATE = r"""
() => {
  const vis = (sel) => {
    const n = document.querySelector(sel);
    if (!n) return false;
    const cs = getComputedStyle(n);
    if (cs.display === "none" || cs.visibility === "hidden") return false;
    return parseFloat(cs.opacity || "1") > 0.05;
  };
  const tags = Array.from(document.querySelectorAll(".scan-tag")).map((n, i) => ({
    i: i,
    label: (n._label || n.innerText || "").trim().replace(/\s+/g, " ").slice(0, 40),
    open: n.className.indexOf("acting") >= 0,
  }));
  return {
    cls: document.body.className,
    gated: document.body.classList.contains("awaiting-first-scene"),
    turnActive: document.body.classList.contains("turn-active"),
    gameOver: !!(document.querySelector("#death-overlay") &&
                 !document.querySelector("#death-overlay").classList.contains("hidden")),
    mode: (window.Renderer || {}).mode,
    tags: tags,
    // Every tag carries its own hidden action bar, so only the OPEN tag's
    // actions are real. Querying all of them offers buttons you cannot click.
    scanActions: Array.from(
      document.querySelectorAll(".scan-tag.acting .scan-action"))
      .map((n) => (n.className.match(/scan-action-(\w+)/) || [])[1]).filter(Boolean),
    // SCAN is a click on the scene, not a button, so "can I scan" is "is there
    // a scene up and no turn in flight" rather than "is #scan-btn visible".
    scanReady: !document.body.classList.contains("awaiting-first-scene")
               && !document.body.classList.contains("turn-active"),
    prose: (document.querySelector("#prose-feed") || {}).innerText
           ? document.querySelector("#prose-feed").innerText.trim() : "",
    toast: (document.querySelector(".renderer-toast, #renderer-toast") || {}).textContent || null,
  };
}
"""


CHOICE_STOP = {
    "the", "a", "an", "into", "out", "onto", "toward", "towards", "up", "down",
    "your", "yours", "his", "her", "its", "their", "them", "this", "that",
    "and", "for", "with", "from", "then", "over", "under", "through", "back",
    "something", "someone", "else", "type",
}


MOVE_WORDS = (
    "move", "walk", "run", "sprint", "head", "go ", "enter", "exit", "leave",
    "step", "slip", "climb into", "cross", "follow", "approach", "retreat",
    "deeper", "outside", "out into", "back to", "toward", "towards", "into the",
)


def is_move_choice(text):
    """A choice that should RELOCATE the player, not just poke the room."""
    low = (text or "").lower()
    return any(w in low for w in MOVE_WORDS)


def choice_words(text):
    """The load-bearing words of a chosen action."""
    return [w for w in re.findall(r"[a-z]+", (text or "").lower())
            if len(w) > 3 and w not in CHOICE_STOP]


def fidelity(choice_text, new_prose):
    """Did the world do what the choice said?

    Not a language model, just a blunt check that the nouns and verbs the
    player picked actually turn up in the prose that followed. Clicking "move
    out into the fog" and getting a scene deeper inside the same warehouse is
    the failure this is looking for.
    """
    words = choice_words(choice_text)
    if not words:
        return None, [], []
    low = (new_prose or "").lower()
    hit = [w for w in words if w in low]
    return len(hit) / float(len(words)), hit, [w for w in words if w not in hit]


def do_choice(page, log, turn):
    """Click one of the WRITTEN choices — the thing a player actually does."""
    # Wait for the rows. #choices-container lives inside #action-wheel, whose
    # opacity is animated, and the buttons are rendered when the prompt item
    # lands — so a single immediate check finds nothing on a turn that is about
    # to be perfectly playable.
    btns = []
    for _ in range(20):
        try:
            btns = [b for b in page.evaluate(
                "() => Array.from(document.querySelectorAll("
                "'.choice-btn:not(.choice-btn-custom)'))"
                ".map(n => (n.dataset.choiceText || n.innerText || '').trim())") if b]
        except Exception:
            btns = []
        if btns:
            break
        time.sleep(1.0)
    if not btns:
        log("    no written choices on screen")
        return None
    log(f"    choices: {[a(b) for b in btns]}")
    # Prefer a choice that GOES somewhere. "Move out into the fog" landing you
    # deeper inside the same building is the reported bug, and it can only show
    # up on a locomotion choice — sampling "kick the barrel" never tests it.
    idx = next((i for i, b in enumerate(btns) if is_move_choice(b)), None)
    if idx is None:
        idx = (turn - 1) % len(btns)
    else:
        log(f"    (taking the movement choice: {a(btns[idx])})")
    picked = btns[idx]
    try:
        page.click(f".choice-btn:not(.choice-btn-custom) >> nth={idx}", timeout=8000)
    except Exception as exc:
        log(f"    could not click choice {idx + 1}: {a(str(exc))[:70]}")
        return None
    return picked


def continuity(path_a, path_b):
    """How much of the composition survived, 1.0 = same framing, 0.0 = unrelated.

    The claim being tested is that INTERACT refines the frame the player was
    already in (img2img, soft transition) while MOVE cuts to a fresh
    composition. Downscaled mean absolute difference is a fair proxy: a refined
    frame keeps its global structure even as details change, a new composition
    does not. Reported as a number rather than a pass/fail, because picking a
    threshold here is exactly the mistake the perceptual-hash check made.
    """
    try:
        a_im = Image.open(path_a).convert("L").resize((64, 64))
        b_im = Image.open(path_b).convert("L").resize((64, 64))
        pa, pb = list(a_im.getdata()), list(b_im.getdata())
        mad = sum(abs(x - y) for x, y in zip(pa, pb)) / float(len(pa))
        return round(1.0 - (mad / 255.0), 3)
    except Exception:
        return None


def analyse(png):
    im = Image.open(io.BytesIO(png)).convert("L")
    w, h = im.size
    box = im.crop((int(w * 0.08), int(h * 0.12), int(w * 0.92), int(h * 0.72)))
    px = list(box.getdata())
    return round(sum(px) / len(px), 1), round(sum(1 for p in px if p < 18) / len(px), 3)


def snap(page, name):
    png = page.screenshot()
    with open(os.path.join(SHOTS, name), "wb") as f:
        f.write(png)
    return analyse(png)


def is_black(mean, dark):
    return mean < 12 or dark > 0.92


CAMERA = r"""
() => {
  const txt = (sel) => {
    const n = document.querySelector(sel);
    if (!n) return "";
    const cs = getComputedStyle(n);
    if (parseFloat(cs.opacity || "1") < 0.05) return "";
    return (n.textContent || "").trim();
  };
  const cinema = document.querySelector("#capture-cinema");
  const filed = document.querySelector("#photo-filed");
  return {
    inCamera: document.body.classList.contains("camera-mode"),
    // "photo-focusing" is the camera up with no plate yet. Shooting into it
    // photographs nothing.
    raising: document.body.classList.contains("photo-focusing"),
    lock: txt("#touch-lock"),
    focusing: txt("#touch-focusing"),
    cinema: !!(cinema && !cinema.classList.contains("hidden")),
    // What the game said about the shot. This is the ONLY signal a photograph
    // produces: /api/photo is read-only and never advances a turn, so nothing
    // in the feed is owed to us here.
    filed: !!(filed && filed.classList.contains("show")),
    filedText: txt("#photo-filed .filed-text"),
  };
}
"""

# Verbs that deliberately do NOT advance the world. A photograph is appraised
# by a read-only endpoint (engine.api_photo: "never mutates world state,
# history, or choices"), so waiting for the prose to grow after one is waiting
# for something the game never promised. It passed for months only because the
# PREVIOUS turn's async tail — choices_revised, an ambient beat, the viewfinder
# raise — usually landed while the camera was up, which is why every photo turn
# "resolved in 1.5s" (one poll) and why moving the photo from turn 2 to turn 4
# turned it into a 153s STUCK on a run that was perfectly alive.
READ_ONLY_ACTIONS = {"photo"}


def do_photo(page, log, findings=None, attempts=2):
    """Actually take a photograph: raise, aim, and click the shutter.

    Pressing PHOTO only raises the camera. The shot is a real mouse click on a
    framed subject ("frame a NEW subject & click / tap to shoot"), so a harness
    that stops at the button press photographs nothing and the turn never
    fires.

    Reports what the game said about the shot, because that line is the whole
    result: a photograph is appraised read-only and changes nothing. A frame
    with nothing legible in it ("Nothing came out — try again, closer") is a
    real answer the game gives on purpose and does NOT spend the subject, so we
    take it at its word and line the shot up again before calling it a finding.
    """
    for shot in range(1, attempts + 1):
        if shot > 1:
            log(f"    re-framing and shooting again ({shot} of {attempts})")
        said = _one_photo(page, log, wider=shot > 1)
        if said is None:
            return None
        if said:
            log(f"    the game said: {a(said)}")
        if said and "nothing came out" not in said.lower():
            return f"PHOTO: {a(said)}"
        if not said:
            log("    !! no receipt line after the shot — the appraisal never landed")
    if findings is not None:
        findings.append(
            "photo: nothing legible came out of "
            f"{attempts} shots — the frame was too dark or too empty to photograph"
        )
    return "PHOTO: aimed and fired the shutter (nothing legible came out)"


def _one_photo(page, log, wider=False):
    """One raise-aim-shoot cycle. Returns the game's line about the shot,
    "" if no receipt appeared, or None if the camera never came up."""
    try:
        page.click("#realtime-btn", timeout=8000)
    except Exception as exc:
        log(f"    PHOTO failed to raise: {a(str(exc))[:80]}")
        return None

    # The plate is a real render; do not shoot until the viewfinder has one.
    t0 = time.time()
    while time.time() - t0 < 90:
        cam = page.evaluate(CAMERA)
        if not cam["inCamera"]:
            log("    camera closed itself while raising")
            return None
        if not cam["raising"]:
            break
        time.sleep(1.0)
    else:
        log(f"    !! viewfinder never came up ({a(page.evaluate(CAMERA)['focusing'])})")
        page.evaluate("() => document.querySelector('#realtime-btn').click()")
        return None

    log(f"    viewfinder up after {time.time() - t0:.1f}s")
    box = page.viewport_size or {"width": 1280, "height": 800}
    w, h = box["width"], box["height"]

    # Sweep a few framings and prefer one the game says is a subject, so the
    # shot is a real photograph of something rather than a wall. A second
    # attempt sweeps different ground rather than re-shooting the frame that
    # already came back empty.
    aims = ([(0.50, 0.62), (0.28, 0.55), (0.72, 0.55), (0.50, 0.44), (0.38, 0.40)]
            if wider else
            [(0.50, 0.50), (0.34, 0.46), (0.66, 0.48), (0.50, 0.38), (0.42, 0.58)])
    target, locked = None, ""
    for fx, fy in aims:
        page.mouse.move(w * fx, h * fy, steps=12)
        time.sleep(0.7)
        lock = page.evaluate(CAMERA)["lock"]
        if lock:
            target, locked = (w * fx, h * fy), lock
            break
        if target is None:
            target = (w * fx, h * fy)

    log(f"    aiming at {int(target[0])},{int(target[1])}"
        f"{f' — locked on {a(locked)[:30]}' if locked else ' (no lock-on offered)'}")

    page.mouse.move(*target, steps=6)
    time.sleep(0.3)
    page.mouse.click(*target)          # <-- the shutter
    log("    >>> SHUTTER (mouse click)")

    # A capture plays a cinema hold; tap through it so the receipt can land.
    t1 = time.time()
    while time.time() - t1 < 25:
        cam = page.evaluate(CAMERA)
        if cam["cinema"]:
            time.sleep(1.5)
            page.mouse.click(w * 0.5, h * 0.5)   # skip the hold
            log("    capture cinema shown — tapped through")
            break
        time.sleep(0.7)

    # The appraisal is the shot's only result, and the line it prints holds for
    # about two seconds (Photo.HOLD_MS) before it fades — so read it while it
    # is on screen rather than looking for it afterwards.
    said = ""
    t2 = time.time()
    while time.time() - t2 < 40:
        cam = page.evaluate(CAMERA)
        if cam["filed"] and cam["filedText"]:
            said = cam["filedText"]
            break
        time.sleep(0.4)

    # One shot per raise: the game lowers the camera itself once the capture
    # reveal is done. Clicking PHOTO here as well just toggles it back UP with
    # no plate, which is a black screen of the harness's own making — so wait
    # for the game, and only intervene if it never happens.
    for _ in range(20):
        if not page.evaluate(CAMERA)["inCamera"]:
            log("    camera lowered itself after the shot (one shot per raise)")
            break
        time.sleep(0.75)
    else:
        log("    !! camera stayed up after the shot — putting it away manually")
        try:
            page.click("#realtime-btn", timeout=5000)
        except Exception:
            pass
    return said


def start_run(page, log):
    s = page.evaluate(STATE)
    if "mode-play" in s["cls"] and s["prose"]:
        log("already in a live run")
        return True
    if "xp-open" not in s["cls"]:
        log(">>> start menu: pressing PLAY")
        page.click("#start-play", timeout=10000)
        for _ in range(30):
            time.sleep(0.5)
            if "xp-ready" in page.evaluate("() => document.body.className"):
                break
    log(">>> confirming the run (Enter)")
    page.keyboard.press("Enter")
    for _ in range(20):
        time.sleep(0.5)
        if "mode-play" in page.evaluate("() => document.body.className"):
            break
    else:
        log("!! PLAY never started a run")
        return False

    t0 = time.time()
    black = 0
    while time.time() - t0 < TURN_TIMEOUT:
        time.sleep(1.5)
        s = page.evaluate(STATE)
        if s["prose"] and not s["gated"]:
            log(f">>> turn 1 playable after {time.time() - t0:.1f}s"
                f"{f' ({black} black samples while loading)' if black else ''}")
            return True
        m, d = analyse(page.screenshot())
        if is_black(m, d):
            black += 1
    log(f"!! no playable turn after {TURN_TIMEOUT}s")
    snap(page, "start_FAILED.png")
    return False


def wait_advance(page, before_prose, timeout, log, observe=None, findings=None):
    """A turn is done when the prose has grown and the turn is no longer active.

    ``observe`` is sampled on every poll, for checks that are only true DURING
    a turn (an INTERACT dive is open exactly while the turn resolves, so
    looking for it after the fact always finds nothing).
    """
    t0 = time.time()
    black = 0
    checks = 0
    while time.time() - t0 < timeout:
        time.sleep(1.5)
        checks += 1
        try:
            s = page.evaluate(STATE)
        except Exception:
            continue
        if observe is not None:
            observe(page)
        if s["gameOver"]:
            return "gameover", time.time() - t0, black
        # An encounter can interrupt ANY turn, and the turn then cannot finish
        # until the fight does. Waiting for prose alone froze here for the whole
        # timeout on a run that was perfectly alive — mid-confrontation.
        try:
            interrupted = page.evaluate(ENCOUNTER_STATE)["inEncounter"]
        except Exception:
            interrupted = False
        if interrupted:
            log("    !! an encounter interrupted this turn — playing it out")
            played = play_out_encounter(page, log, findings)
            log(f"    interrupting encounter: {played or 'never resolved'}")
            t0 = time.time()  # the interrupted turn gets its own clock back
            continue
        if checks % 3 == 0:
            m, d = analyse(page.screenshot())
            if is_black(m, d):
                black += 1
        if len(s["prose"]) > len(before_prose) and not s["turnActive"]:
            return "ok", time.time() - t0, black
    return "stuck", time.time() - t0, black


DIVE_STATE = r"""
() => {
  const p = document.getElementById('moment-portrait');
  const img = document.getElementById('moment-portrait-img');
  const src = img ? (img.getAttribute('src') || '') : '';
  return {
    top: (window.Moments && window.Moments.topType) ? window.Moments.topType() : null,
    inDive: /\bmoment-interact\b/.test(document.body.className),
    // 'developing' IS the glowing shimmer. It only clears when a portrait
    // actually loads, so a dive stuck on it is a dive with no close-up.
    developing: !!(p && p.classList.contains('developing')),
    ready: !!(p && p.classList.contains('ready')),
    // A data: URL is the live SCAN crop pinned on entry; an /images/ path is
    // the generated close-up that should replace it.
    plate: src.startsWith('data:') ? 'crop' : (src ? 'generated' : 'none'),
    painted: img ? (img.naturalWidth || 0) : 0,
  };
}
"""


class DiveWatch:
    """Did INTERACT actually dive into a close-up of the object?

    The dive is only on screen WHILE the turn resolves, so this samples during
    the wait rather than after it. Records the beats the spec asks for: the
    Moment opening, the object's own crop pinned, a generated close-up
    replacing it, and whether the dive was still glowing once the new frame
    had painted (the overlay outliving its frame).
    """

    def __init__(self):
        self.opened = False
        self.crop = False
        self.generated = False
        self.last = None
        self.trace = []

    def __call__(self, page):
        try:
            d = page.evaluate(DIVE_STATE)
        except Exception:
            return
        self.last = d
        if d["inDive"] or d["top"] == "interact":
            self.opened = True
        if d["plate"] == "crop":
            self.crop = True
        if d["plate"] == "generated" and d["painted"] > 0:
            self.generated = True
        step = (d["top"], d["plate"], d["developing"])
        if not self.trace or self.trace[-1] != step:
            self.trace.append(step)

    def report(self, log, findings, turn):
        log(f"    dive: opened={self.opened} crop_pinned={self.crop} "
            f"close_up={self.generated}")
        log(f"    dive trace: {self.trace}")
        if not self.opened:
            findings.append(f"turn {turn}: INTERACT never opened the close-up dive")
            return
        if not self.generated:
            findings.append(
                f"turn {turn}: INTERACT dive never resolved into a close-up"
                f"{' (only the raw SCAN crop)' if self.crop else ' (empty developing shimmer)'}"
            )
    def leave(self, page, log, findings, turn):
        """Walk back out of the dive the way a player does, and check where it
        lands. The dive is deliberately not self-closing any more: exiting is
        the player's call, and it must hand off to the REGENERATED scene."""
        try:
            state = page.evaluate(DIVE_STATE)
        except Exception:
            return
        if not (state["inDive"] or state["top"] == "interact"):
            findings.append(f"turn {turn}: the dive closed itself - the player "
                            f"never got to leave it")
            return
        # The dive's slate is SPEAK / ATTACK / LEAVE, and only LEAVE goes back
        # to the scene. It is locked (disabled) until the INTERACT turn's frame
        # has painted, which is exactly the wait this check is about — so poll
        # for it to come live rather than clicking whatever is at the top.
        leave = "#moment-choices .moment-choice:not(.moment-choice-locked):has-text('LEAVE')"
        try:
            page.click(leave, timeout=45000)
        except Exception as exc:
            findings.append(f"turn {turn}: no way out of the dive "
                            f"({a(str(exc))[:60]})")
            return
        for _ in range(20):
            time.sleep(0.75)
            try:
                if not page.evaluate(DIVE_STATE)["inDive"]:
                    log("    dive: left on the X, landed back in the scene")
                    return
            except Exception:
                continue
        findings.append(f"turn {turn}: pressing the X did not leave the dive")


ENCOUNTER_STATE = r"""
() => {
  const shown = (sel) => {
    const n = document.querySelector(sel);
    return !!(n && !n.classList.contains('hidden'));
  };
  // An encounter is a MOMENT: it renders its own numbered choice list
  // (.moment-choice in #moment-choices), NOT the turn's .choice-btn wheel.
  // Watching .choice-btn found the 3 stale turn choices sitting behind the
  // letterbox and never clicked the encounter at all.
  const btns = Array.from(document.querySelectorAll('.moment-choice'))
    .filter((n) => {
      const cs = getComputedStyle(n);
      return cs.display !== 'none' && cs.pointerEvents !== 'none'
             && parseFloat(cs.opacity || '1') > 0.05 && !n.disabled;
    })
    .map((n) => (n.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 60));
  return {
    body: document.body.className,
    inEncounter: /encounter/.test(document.body.className)
                 || shown('#encounter-ceremony') || shown('#encounter-hud'),
    ceremony: shown('#encounter-ceremony'),
    choices: btns,
    dead: !!(document.querySelector('#death-overlay')
             && !document.querySelector('#death-overlay').classList.contains('hidden')),
  };
}
"""


def do_encounter(page, log, findings=None):
    """Force an encounter (Shift+N) and play it to a resolution.

    Encounters mint their own plates (the hitch, the title slam, the resolve
    still), which is the path most likely to strand a veil or paint a black
    frame, so it is worth driving deliberately rather than waiting for one to
    roll.
    """
    try:
        page.click("#encounter-debug-btn", timeout=8000)
    except Exception:
        try:
            page.keyboard.press("Shift+N")
        except Exception as exc:
            log(f"    could not trigger an encounter: {a(str(exc))[:70]}")
            return None

    for _ in range(20):
        time.sleep(1.0)
        if page.evaluate(ENCOUNTER_STATE)["inEncounter"]:
            break
    else:
        log("    !! encounter never started")
        return None
    log("    encounter started")
    return play_out_encounter(page, log, findings)


PLATE_STATE = r"""
() => {
  const sc = document.querySelector("#moment-scene");
  const img = document.querySelector("#moment-scene-img");
  const src = img ? (img.getAttribute("src") || "") : "";
  return {
    ready: !!(sc && sc.classList.contains("ready")),
    developing: !!(sc && sc.classList.contains("developing")),
    painted: !!src,
    frame: (src.match(/([^/\\]+\.(?:png|jpg|jpeg))/i) || ["", ""])[1],
  };
}
"""


def wait_plate(page, log, seconds=45, since=""):
    """Let the confrontation plate land before choosing against it.

    The slate and the plate are different beats, and this loop used to click
    the instant a choice existed — so it decided the fight against a frame that
    had not been drawn yet, counted the un-drawn frame as a black sample, and
    reported both as the encounter's behaviour. A player reads the slate while
    the standoff breathes; the harness should at least let it arrive.
    """
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            p = page.evaluate(PLATE_STATE)
        except Exception:
            time.sleep(0.4)
            continue
        # `since` is the frame the LAST round held on. Without it a later round
        # is handed the previous plate — still marked ready — and waits for
        # nothing at all.
        if p["ready"] and p["painted"] and p["frame"] != since:
            return round(time.time() - t0, 1), p["frame"]
        time.sleep(0.4)
    try:
        p = page.evaluate(PLATE_STATE)
    except Exception:
        p = {"painted": False, "frame": "", "developing": False}
    log(f"    !! plate never became ready in {seconds}s "
        f"(painted={p['painted']} developing={p['developing']})")
    return None, p.get("frame") or ""


def play_out_encounter(page, log, findings=None):
    """Play an already-OPEN encounter through to its resolution.

    Split out of do_encounter because an encounter also fires on its own in the
    middle of an unrelated turn (after a photo, mid-move). The interrupted turn
    cannot finish until the fight does, so whoever is waiting on that turn has
    to play this out rather than sit there until the timeout.
    """
    black = 0
    committed = 0
    rounds = []  # the full slate offered each round, to catch a stale one
    last_frame = ""  # the plate this round must replace before we choose again
    t0 = time.time()
    while time.time() - t0 < 150:
        enc = page.evaluate(ENCOUNTER_STATE)
        mean, dark = analyse(page.screenshot())
        if is_black(mean, dark):
            black += 1
        if enc["dead"]:
            log(f"    encounter ended in DEATH after {committed} choice(s)"
                f"{f' — {black} black samples' if black else ''}")
            return f"ENCOUNTER: died after {committed} choice(s)"
        if not enc["inEncounter"] and committed:
            log(f"    encounter resolved after {committed} choice(s)"
                f"{f' — {black} black samples' if black else ''}"
                f" (luma={mean})")
            return f"ENCOUNTER: survived {committed} choice(s)"
        if enc["choices"]:
            # Do not decide the fight against a frame that has not been drawn.
            waited, frame = wait_plate(page, log, since=last_frame)
            if frame:
                last_frame = frame
            if waited is None:
                msg = (f"encounter round {len(rounds) + 1}: the plate never "
                       f"drew — choosing against a blank frame")
                if findings is not None and msg not in findings:
                    findings.append(msg)
            else:
                log(f"    plate ready after {waited}s{f' ({a(frame)})' if frame else ''}")
            # Keep one frame of the confrontation chrome as the player sees it:
            # the encounter treatment is what the styling complaints are about,
            # and it only exists while the choices are up.
            if not committed:
                snap(page, "encounter_ui.png")
                log(f"    encounter chrome: body={a(enc['body'])[:110]}")
            # Rotate lanes. Always taking choice 1 took the confront lane every
            # single beat, which is the most lethal option available — the
            # harness died in two rounds and reported that as the encounter
            # system's behaviour. The lanes are meant to be a real decision, so
            # sample them rather than one of them.
            pick = committed % len(enc["choices"])
            if pick == len(enc["choices"]) - 1:
                pick = 0  # last row is "do something else - type it"
            # Record the WHOLE slate, not just the row taken. A fight is only a
            # real decision if the options move with it, and logging one pick
            # per round made a repeated slate impossible to see.
            slate = [a(c) for c in enc["choices"]]
            rounds.append(slate)
            log(f"    round {len(rounds)} slate: {slate}")
            if len(rounds) > 1 and slate:
                # The last row is always "do something else - type it".
                prev, here = set(rounds[-2][:-1]), set(slate[:-1])
                if here and here == prev:
                    msg = (f"encounter round {len(rounds)}: identical slate to the "
                           f"previous round - the options did not move with the fight")
                    log(f"    !! {msg}")
                    if findings is not None and msg not in findings:
                        findings.append(msg)
                elif here & prev:
                    log(f"    note: {len(here & prev)} option(s) carried over "
                        f"from the previous round")
            log(f"    encounter choice: {a(enc['choices'][pick])}")
            try:
                page.click(f".moment-choice >> nth={pick}", timeout=6000)
                committed += 1
            except Exception:
                pass
            time.sleep(4.0)
            continue
        time.sleep(2.0)

    log(f"    !! encounter never resolved ({black} black samples)")
    return None


def do_scan_action(page, log, prefer="move"):
    """SCAN, open a tag, then commit its sub-action. Returns a label or None."""
    s = page.evaluate(STATE)
    if not s["tags"]:
        # Neither tags nor a live SCAN button yet. This is not a dead end: a
        # fresh scene clears its tags and SCAN re-arms a moment later, and a
        # scene that is ALREADY scanned keeps SCAN disabled on purpose (one
        # scan per scene) while re-painting its tags. Bailing here reported
        # "SCAN not available" on turns that were about to be perfectly
        # playable. Wait for either road in.
        for _ in range(14):
            time.sleep(1.5)
            s = page.evaluate(STATE)
            if s["tags"] or s["scanReady"]:
                break
        if not s["tags"] and not s["scanReady"]:
            log("    no tags and SCAN never re-armed")
            return None
    if not s["tags"]:
        # SCAN has no button — clicking the scene performs it. Aim high and
        # centre: the choice stack owns the lower middle and the camera sits
        # centre-right, so a click down there would press those instead.
        box = page.viewport_size or {"width": 1280, "height": 800}
        page.mouse.click(box["width"] * 0.5, box["height"] * 0.3)
        for _ in range(10):
            time.sleep(1.5)
            s = page.evaluate(STATE)
            if s["tags"]:
                break
        else:
            log("    clicking the scene produced no scan tags")
            return None

    labels = [t["label"] for t in s["tags"]]
    log(f"    scanned: {[a(x) for x in labels]}")
    idx = int(os.environ.get("PT_TAG_INDEX", "0")) % len(s["tags"])
    page.click(f".scan-tag >> nth={idx}", timeout=8000)

    # The tag has to actually open before its sub-actions can be committed.
    for _ in range(8):
        time.sleep(0.5)
        s = page.evaluate(STATE)
        if s["scanActions"]:
            break
    else:
        log("    tag never opened its sub-actions")
        return None

    log(f"    sub-actions: {s['scanActions']}")
    want = prefer if prefer in s["scanActions"] else s["scanActions"][0]
    if want == "talk" and len(s["scanActions"]) > 1:
        want = [x for x in s["scanActions"] if x != "talk"][0]
    # Scope to the OPEN tag — the other tags' identical buttons are hidden.
    page.click(f".scan-tag.acting .scan-action-{want}", timeout=10000)
    return f"{a(labels[idx])} -> {want.upper()}"


def main():
    findings = []
    lines = []
    verbs = []  # (turn, action), paired with frames for the continuity report
    move_choices = {}  # turn -> the locomotion choice taken, for the drift check

    def log(msg):
        print(msg, flush=True)
        lines.append(msg)

    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp("http://127.0.0.1:9333")
        pages = [p for p in b.contexts[0].pages if "standalone" in p.url]
        if not pages:
            log("no standalone page")
            return
        page = pages[0]

        errors = []
        page.on("console", lambda m: errors.append(m.text[:200])
                if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)[:200]))

        if os.environ.get("PT_RELOAD") == "1":
            log(">>> reloading client for current CSS/JS")
            page.reload(wait_until="load")
            page.wait_for_function("() => !!window.Renderer", timeout=60000)
            time.sleep(3)

        if not start_run(page, log):
            log("!! could not start a run")
            return

        # Rotate the verbs a player would actually use.
        plan = ["choice", "choice", "scan_move", "photo", "scan_interact",
                "encounter", "act", "camp"]

        for turn in range(1, TURNS + 1):
            s = page.evaluate(STATE)
            if s["gameOver"]:
                log(f"\n--- turn {turn}: GAME OVER — stopping")
                break

            mean, dark = snap(page, f"turn_{turn:02d}_view.png")
            log(f"\n--- turn {turn} ({plan[(turn - 1) % len(plan)]}) ---")
            log(f"  picture: luma={mean} dark={dark}"
                f"{'   <<< BLACK SCREEN' if is_black(mean, dark) else ''}")
            if is_black(mean, dark):
                findings.append(f"turn {turn}: black picture on an idle, playable turn")
            tail = a(s["prose"])[-160:]
            if tail:
                log(f"  prose: ...{tail}")

            before = s["prose"]
            action = plan[(turn - 1) % len(plan)]
            verbs.append((turn, action))
            os.environ["PT_TAG_INDEX"] = str(turn)
            did = None

            if action == "choice":
                did = do_choice(page, log, turn)
                if did and is_move_choice(did):
                    move_choices[turn] = did
            elif action == "scan_move":
                did = do_scan_action(page, log, prefer="move")
            elif action == "scan_interact":
                did = do_scan_action(page, log, prefer="interact")
            elif action == "photo":
                did = do_photo(page, log, findings)
            elif action == "encounter":
                did = do_encounter(page, log, findings)
            elif action == "act":
                try:
                    page.click("#free-will-btn", timeout=8000)
                    time.sleep(1)
                    page.fill("#custom-input, #free-will-input, textarea",
                              "Search the ground for tracks", timeout=8000)
                    page.click("#custom-submit", timeout=8000)
                    did = "ACT: search the ground for tracks"
                except Exception as e:
                    log(f"    ACT failed: {a(str(e))[:80]}")
            elif action == "camp":
                try:
                    page.click("#camp-btn", timeout=8000)
                    did = "CAMP"
                except Exception as e:
                    log(f"    CAMP failed: {a(str(e))[:80]}")

            if not did:
                findings.append(f"turn {turn}: could not commit a '{action}' action")
                log(f"  !! no action committed for '{action}'")
                snap(page, f"turn_{turn:02d}_NOACTION.png")
                continue

            log(f"  >>> committed: {did}")
            # A read-only verb has already delivered everything it is going to.
            # Putting it through wait_advance asks the world to move for an
            # action that is documented not to move it, and then blames the
            # game for the timeout.
            if action in READ_ONLY_ACTIONS:
                log("  read-only verb — the world does not advance on this one")
                continue

            # The INTERACT dive only exists during the turn, so watch it there.
            dive = DiveWatch() if action == "scan_interact" else None
            result, el, black = wait_advance(page, before, TURN_TIMEOUT, log,
                                             observe=dive, findings=findings)
            if dive is not None:
                dive.report(log, findings, turn)
                dive.leave(page, log, findings, turn)
            if black:
                findings.append(f"turn {turn}: {black} black sample(s) while resolving")
            if result == "stuck":
                findings.append(f"turn {turn}: never resolved after {el:.0f}s ({did})")
                log(f"  !! STUCK after {el:.0f}s")
                snap(page, f"turn_{turn:02d}_STUCK.png")
                break
            if result == "gameover":
                log(f"  GAME OVER after {el:.1f}s")
                break
            log(f"  resolved in {el:.1f}s{f' ({black} black samples)' if black else ''}")

            # A hesitation on a turn that then resolved is a FALSE ALARM, not a
            # stall — the recovery UI was dumped over a turn that was still on
            # its way. The old summary filed these under console noise and still
            # printed "every turn committed and resolved", which is how the bug
            # survived so long. It is a failure.
            # Only the prose ADDED this turn counts: an old hesitation stays in
            # the feed and would re-flag on every later turn.
            fresh = page.evaluate(STATE)["prose"]
            if fresh.startswith(before):
                fresh = fresh[len(before):]

            # Did the world do what was chosen? Only meaningful for a written
            # choice, where the player picked specific words.
            if action == "choice" and did:
                score, hit, missed = fidelity(did, fresh)
                if score is not None:
                    log(f"  fidelity: {score:.2f} kept={[a(w) for w in hit]} "
                        f"missing={[a(w) for w in missed]}")
                    if score < 0.34:
                        findings.append(
                            f"turn {turn}: chose '{a(did)}' but the world did not "
                            f"do it - none of {[a(w) for w in missed]} appear in "
                            f"what followed"
                        )
            if "world hesitated" in fresh.lower():
                findings.append(
                    f"turn {turn}: recovery UI fired on a turn that resolved in "
                    f"{el:.1f}s - watchdog interrupted a healthy turn"
                )

        # The verb claim, measured rather than asserted: INTERACT refines the
        # frame the player was already in, MOVE cuts to a new composition. Both
        # are compared against the frame of the turn BEFORE them.
        log("\n=========== FRAME CONTINUITY ============")
        for i, (turn, action) in enumerate(verbs):
            if i == 0:
                continue
            prev = os.path.join(SHOTS, f"turn_{verbs[i - 1][0]:02d}_view.png")
            here = os.path.join(SHOTS, f"turn_{turn:02d}_view.png")
            if not (os.path.exists(prev) and os.path.exists(here)):
                continue
            score = continuity(prev, here)
            if score is None:
                continue
            prev_turn, prev_action = verbs[i - 1]
            log(f"  turn {prev_turn} -> {turn} after {prev_action}: {score:.2f}")
            # A choice that says GO must land somewhere else. If the frame is
            # essentially unchanged the player was told they moved and wasn't:
            # the "walk out into the fog, appear deeper in the same warehouse"
            # bug. INTERACT is exempt — refining the same frame is its point.
            if prev_turn in move_choices and score > 0.90:
                findings.append(
                    f"turn {prev_turn}: chose to move "
                    f"('{a(move_choices[prev_turn])}') but the frame barely "
                    f"changed (continuity {score:.2f}) - the world did not "
                    f"actually take the player anywhere"
                )

        log("\n================ SUMMARY ================")
        if findings:
            log("PROBLEMS:")
            for f in findings:
                log(f"  - {f}")
        else:
            log("every turn committed and resolved; no black screens")
        uniq = []
        for e in errors:
            if e not in uniq:
                uniq.append(e)
        if uniq:
            log("\nCLIENT CONSOLE ERRORS:")
            for e in uniq[:12]:
                log(f"  {a(e)}")
        else:
            log("no client console errors")

    with open(os.path.join(SHOTS, "REPORT.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    main()
