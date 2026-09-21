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


FIST_STATE = """() => {
  const b = document.getElementById('fist-btn');
  if (!b) return 'none';
  if (b.classList.contains('ready')) return 'ready';
  if (b.classList.contains('waiting')) return 'waiting';
  return document.querySelector('.choice-btn') ? 'open' : 'idle';
}"""


# Why a green fist would not take a click: what sits on top of it, and
# which ancestor hides it.
FIST_BLOCKER = """() => {
  const b = document.getElementById('fist-btn');
  const r = b.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const top = document.elementFromPoint(x, y);
  const chain = [];
  for (let n = b; n && n !== document.documentElement; n = n.parentElement) {
    const cs = getComputedStyle(n);
    chain.push({ id: n.id, cls: String(n.className).slice(0, 120), op: cs.opacity,
                 pe: cs.pointerEvents, vis: cs.visibility, disp: cs.display });
  }
  return { rect: [r.left, r.top, r.width, r.height], disabled: b.disabled,
           top: top ? (top.id || top.tagName) + '.' + String(top.className).slice(0, 120) : null,
           body: document.body.className, chain };
}"""


def open_fist(page, log, wait_s=30.0):
    """The rows wait behind the FIST (Fist, standalone.js): the turn releases
    when its picture lands, the fist appears greyed while the slate is still
    being written, turns green when it is ready, and pressing it reveals the
    choices + Custom. A player presses it; so does the harness — and the wait
    from "picture up" to "fist green" is logged, because that number is the
    slate's own cost, which used to be buried inside the turn."""
    t0 = time.time()
    waited_grey = 0.0
    tag = os.environ.get("PT_TAG_INDEX", "0")
    shot_grey = False
    while time.time() - t0 < wait_s:
        try:
            st = page.evaluate(FIST_STATE)
        except Exception:
            st = "idle"
        if st in ("open", "none"):
            return True
        if st == "ready":
            snap(page, f"turn_{int(tag):02d}_fist_green.png")
            try:
                page.click("#fist-btn", timeout=4000)
            except Exception as exc:
                log(f"    the fist would not press: {a(str(exc))[:80]}")
                try:
                    log("    fist blocker: " + a(json.dumps(page.evaluate(FIST_BLOCKER))))
                except Exception as exc2:
                    log(f"    fist blocker probe failed: {a(str(exc2))[:80]}")
                return False
            log(f"    fist: green after {waited_grey:.1f}s grey; pressed")
            for _ in range(20):
                time.sleep(0.15)
                try:
                    if page.evaluate("() => !!document.querySelector('.choice-btn')"):
                        time.sleep(0.5)  # the rows pop in staggered
                        snap(page, f"turn_{int(tag):02d}_fist_open.png")
                        return True
                except Exception:
                    pass
            log("    fist pressed but no rows appeared")
            return False
        if st == "waiting":
            waited_grey = time.time() - t0
            if not shot_grey:
                shot_grey = True
                snap(page, f"turn_{int(tag):02d}_fist_grey.png")
        time.sleep(0.25)
    log(f"    the fist never turned green in {wait_s:.0f}s (last state: {st})")
    return False


def do_choice(page, log, turn):
    """Click one of the WRITTEN choices — the thing a player actually does."""
    open_fist(page, log)
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


# ── TYPED ACTIONS ───────────────────────────────────────────────────────────
# The free-will input is the part of this game with the fewest rails, and every
# gate it passes through has at some point quietly refused it: a prompt that
# asked for "the ATTEMPT" and got a failure, a world bible that forbade leaving
# the valley, a camera contract that stood a driver back up on his feet.
#
# Each entry is (what the player types, the words the WORLD should still be
# using once the turn lands). The second half is the check that matters. A
# typed action can be written perfectly into the prose and then dropped by the
# frame that follows — and the choice slate, which is generated FROM that frame,
# then honestly describes a world the action never reached. That is what "the
# game isn't taking my custom action" looks like from the sofa.
# `holds` is the important distinction, and the first version of this got it
# wrong and filed three false alarms. An action that changes WHERE the player is
# or WHAT THEY ARE ON has to still be true on the next slate — you do not stop
# being in a truck because a turn went by. A one-off act does not: smashing a
# window is finished the moment it is done, and a slate that has moved on to
# what comes next is correct, not forgetful. Demanding persistence from both
# reports a working game as broken, which is worse than not checking.
CUSTOM_BATTERY = [
    # Travel that changes what the player is ON. The reported failure.
    ("get in the truck and drive",
     ("truck", "cab", "wheel", "driv", "engine", "tire", "windshield"), True),
    # Changing height — the other way to stop being a figure standing on dirt.
    ("climb up onto the roof",
     ("roof", "ledge", "edge", "above", "below", "rooftop", "down"), True),
    # Beyond a human body, and to somewhere the bible has never heard of.
    ("fly to antarctica",
     ("ice", "snow", "cold", "frozen", "antarc", "white", "freez"), True),
    # Destroying something in the frame. Over when it is over.
    ("smash the nearest window",
     ("glass", "window", "shard", "broke", "smash", "shatter"), False),
    # Talking at somebody: neither movement nor violence.
    ("shout for whoever is out there",
     ("shout", "voice", "call", "echo", "answer", "heard", "yell"), False),
]


def honoured(text, words):
    """Which of the expected words the world is using."""
    low = (text or "").lower()
    return [w for w in words if w in low]


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

# How long ONE exchange of a confrontation is allowed to take. A round is a
# roll, a resolve plate and an aftermath beat, so it costs about what a turn
# costs. The fight's whole budget is this times the rounds it has played.
ENCOUNTER_ROUND_S = int(os.environ.get("PT_ENCOUNTER_ROUND", "75"))

# Console output that says something true about the machine or the account rather
# than about the code. Reactor answering 402 credits_depleted is the standing
# example: the client handles it correctly (terminal, straight to stills), but the
# browser logs the failed request regardless, so it turned up in every run's
# console-error list and trained the reader to ignore that list.
ENVIRONMENT_NOISE = re.compile(
    r"\b402\b|credits?[_\s-]?depleted|no available capacity|payment required",
    re.I,
)


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
    held = 0
    while time.time() - t0 < TURN_TIMEOUT:
        time.sleep(1.5)
        s = page.evaluate(STATE)
        # "Playable" has to mean the player can SEE something. Prose arriving is
        # not enough now that the opening deliberately holds a black screen until
        # the first picture is ready — at 4K that hold is a minute, and treating
        # the turn as playable during it made every run screenshot pure black and
        # file the intended behaviour as a black-screen fault.
        blacked_out = "opening-blackout" in (s.get("cls") or "")
        m, d = analyse(page.screenshot())
        # Prose, ungated, black lifted — AND a picture actually on screen. The
        # last clause matters: the montage paints into the Moment overlay and the
        # main scene layer paints when it pops, so there is a window where every
        # state flag says playable and the screen is still black. Sampling in that
        # window reported "black picture on an idle, playable turn" on runs whose
        # frames were measured healthy on disk (luma 78-105) — a false alarm
        # about the harness's own timing.
        if s["prose"] and not s["gated"] and not blacked_out and not is_black(m, d):
            log(f">>> turn 1 playable after {time.time() - t0:.1f}s"
                + (f" ({black} black samples while loading)" if black else "")
                + (f" [{held} of them the opening blackout, by design]" if held else ""))
            return True
        if is_black(m, d):
            black += 1
            # The opening is SUPPOSED to be black. PLAY fades down and holds it
            # until there is a picture to fade up to, so counting those samples
            # as a fault would file the intended behaviour as the bug — and a
            # harness that cries wolf about its own feature gets ignored.
            if blacked_out:
                held += 1
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
        """Check how the dive hands the world back.

        The dive is a TRANSITION and leaves on its own the moment the new
        frame paints (see createInteractDive in standalone.js: the SPEAK /
        ATTACK / LEAVE slate it used to carry "was the thing that made this
        feel slow"). This used to assert the opposite — that the dive must
        wait for the player to press LEAVE — and filed "the dive closed
        itself" against every INTERACT turn of a client working as designed.
        What is actually wrong is a dive that is STILL up once the turn is
        over: that is the player stuck behind a close-up."""
        try:
            state = page.evaluate(DIVE_STATE)
        except Exception:
            return
        if not (state["inDive"] or state["top"] == "interact"):
            log("    dive: handed the world back on its own (by design)")
            return
        # Still up. Give it the paint-to-hand-back window, then it is stuck.
        for _ in range(20):
            time.sleep(0.75)
            try:
                if not page.evaluate(DIVE_STATE)["inDive"]:
                    log("    dive: handed the world back after the frame painted")
                    return
            except Exception:
                continue
        findings.append(f"turn {turn}: the dive stayed up after the turn resolved "
                        f"- the player is stuck behind the close-up")
        return


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
    # Budget per ROUND, not per fight. Every exchange is a real generation
    # (~35s on the stills path) and a fight is not guaranteed to end in one:
    # the enemy has to be worn down, and the odds of settling only climb with
    # the round number. A flat 150s expired mid-generation on a fight the
    # server had ALREADY resolved on round four, and the run reported "never
    # resolved" for it — the harness timing out is not the game failing.
    while time.time() - t0 < ENCOUNTER_ROUND_S * max(1, committed + 1):
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
            # DO NOT compare these between rounds. What a confrontation slate
            # SHOWS is the lane — attack / flee / reason — and it is supposed to
            # read the same every round; the written verb behind it is
            # deliberately not put on screen (see LANE_WORDS in standalone.js:
            # "promise 'Shatter his skull against wall' and anything else is a
            # broken promise"). This check used to diff the visible text and
            # filed three findings a fight against a system working as designed.
            # What moving options actually look like from out here is a new
            # PLATE each round, which wait_plate already requires.
            log(f"    encounter choice: {a(enc['choices'][pick])}")
            try:
                page.click(f".moment-choice >> nth={pick}", timeout=6000)
                committed += 1
            except Exception:
                pass
            time.sleep(4.0)
            continue
        time.sleep(2.0)

    log(f"    !! encounter still going after {committed} round(s) and "
        f"{time.time() - t0:.0f}s ({black} black samples)")
    return None


FLIPBOOK_TAP = r"""
() => {
  if (window.__ptFlip) return true;
  const R = window.Renderer;
  if (!R || typeof R.applyScene !== "function") return false;
  window.__ptFlip = { beats: [], paints: [], t0: Date.now() };
  // What the SERVER delivered. A flipbook turn carries its in-between frames
  // on the beat (metadata.sequence); a still turn carries none, and the two
  // have to be told apart before anything is blamed on playback.
  const orig = R.applyScene.bind(R);
  R.applyScene = function (imageUrl, prompt, meta) {
    try {
      const seq = meta && (meta.sequence || meta.flipbook);
      const frames = seq ? (Array.isArray(seq) ? seq : (seq.frames || [])) : [];
      window.__ptFlip.beats.push({
        at: Date.now() - window.__ptFlip.t0,
        still: imageUrl || "",
        frames: (frames || []).slice(),
        frame_ms: (seq && !Array.isArray(seq)) ? (Number(seq.frame_ms) || 0) : 0,
      });
    } catch (e) {}
    return orig(imageUrl, prompt, meta);
  };
  // What the SCREEN actually showed. Playback swaps the background-image on
  // the two scene layers (paintSequenceFrame), so the style attribute is the
  // only honest record that a frame reached the player — a CDP screenshot at
  // 1.5s intervals cannot see 420ms frames go by.
  const seen = new MutationObserver((recs) => {
    for (const r of recs) {
      const t = r.target;
      const m = /url\(["']?([^"')]+)["']?\)/.exec(t.style.backgroundImage || "");
      if (!m) continue;
      const p = window.__ptFlip.paints;
      if (p.length && p[p.length - 1].url === m[1]) continue;
      p.push({ at: Date.now() - window.__ptFlip.t0, url: m[1], layer: t.id });
    }
  });
  for (const id of ["sceneA", "sceneB"]) {
    const n = document.getElementById(id);
    if (n) seen.observe(n, { attributes: true, attributeFilter: ["style"] });
  }
  return true;
}
"""

FLIPBOOK_DRAIN = r"""
() => {
  const f = window.__ptFlip;
  if (!f) return null;
  const out = { beats: f.beats, paints: f.paints };
  f.beats = [];
  f.paints = [];
  return out;
}
"""


def frame_bytes(origin, url):
    """Fetch one generated frame off the running server."""
    from urllib.request import urlopen
    from urllib.parse import urljoin
    try:
        with urlopen(urljoin(origin + "/", url), timeout=20) as r:
            return r.read()
    except Exception:
        return None


def panel_motion(origin, urls, cache):
    """How much each panel differs from the one before it.

    A flipbook is only worth the wait if the panels are the in-between frames
    of one motion. Four panels of the same pose animate as a freeze, which
    looks to a player exactly like the turn didn't happen — and no state flag
    anywhere reports it, because the turn resolved and the frames exist.

    Reported as continuity numbers (1.0 = the same picture) rather than a
    verdict, for the reason continuity() gives.
    """
    ims = []
    for u in urls:
        if u not in cache:
            raw = frame_bytes(origin, u)
            try:
                cache[u] = Image.open(io.BytesIO(raw)).convert("L").resize((64, 64)) \
                    if raw else None
            except Exception:
                cache[u] = None
        ims.append(cache[u])
    if any(i is None for i in ims):
        return None, ims.count(None)
    scores = []
    for a_im, b_im in zip(ims, ims[1:]):
        pa, pb = list(a_im.getdata()), list(b_im.getdata())
        mad = sum(abs(x - y) for x, y in zip(pa, pb)) / float(len(pa))
        scores.append(round(1.0 - (mad / 255.0), 3))
    return scores, 0


# PT_TRACE=1 records what every system in the loop knew at each turn, so the
# handoffs can be read across a run rather than inferred from one frame: the
# goal the run was staged with, the LEAD the HUD shows, the pacing dials, the
# detection dial and which sensor moved it, the encounter record, what the
# narrator said, what the consequence model wrote and what the frame became.
# It exists because "every turn committed and resolved" says nothing about
# whether the opening, the narrator, the encounters and the objectives are
# talking to each other — and reading the state file by hand after a run is
# how that question kept going unanswered.
TRACE_ON = os.environ.get("PT_TRACE") == "1"


def _clip(s, n):
    return a(str(s or ""))[:n]


def _label(o):
    """A detection can be stored as a dict or as its bare label; read either."""
    if isinstance(o, dict):
        return o.get("label") or o.get("text") or ""
    return str(o or "")


class LoopTrace:
    """One row per turn of what each system saw. Written to
    _playthrough/loop_trace.json and summarised at the end of the run."""

    def __init__(self, origin, session_id="default"):
        self.origin = origin
        self.sid = session_id
        self.rows = []
        self.last_feed_id = 0
        self.goal = ""
        self.last_slate = []

    def _get(self, url):
        raw = frame_bytes(self.origin, url)
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    def _disk(self, name):
        # The server renames state.json.tmp over state.json between turns and
        # Windows refuses the read for a beat while it does; retry, don't file.
        path = os.path.join("sessions", self.sid, name)
        for _ in range(6):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                time.sleep(0.3)
        return {} if name.endswith("state.json") else []

    def _client(self, page):
        try:
            return page.evaluate(r"""() => {
              const get = (k) => { try { return JSON.parse(localStorage.getItem(k) || 'null'); } catch (e) { return null; } };
              const ev = get('evidence_v1') || {};
              const ob = get('objectives_v1') || {};
              const items = Array.isArray(ob.items) ? ob.items : (ob.items ? Object.values(ob.items) : []);
              return {
                evidence_unique: Array.isArray(ev.seen) ? ev.seen.length : null,
                film: ev.film,
                objectives: items.map((o) => ({id: o.id, kind: o.kind, status: o.status,
                  title: String(o.title || '').slice(0, 80), count: o.count, goal: o.goal})),
                lead_text: (document.querySelector('#objectives-panel, #objectives, .objectives') || {}).innerText
                  ? (document.querySelector('#objectives-panel, #objectives, .objectives').innerText || '').replace(/\s+/g, ' ').slice(0, 200) : '',
              };
            }""")
        except Exception:
            return {}

    def snapshot(self, page, label, log=None):
        # The trace must never end the run it is watching: a shape it did not
        # expect is a note in the log, not a traceback over the playtest.
        try:
            return self._snapshot(page, label, log)
        except Exception as exc:
            if log:
                log(f"  [trace] could not snapshot {label}: {a(str(exc))[:120]}")
            return None

    def _snapshot(self, page, label, log=None):
        st = self._disk("state.json") or {}
        hist = self._disk("history.json") or []
        last = hist[-1] if hist else {}
        status = self._get("/api/status") or {}
        status = status.get("data") or status
        lead = self._get("/api/objectives") or {}
        feed = self._get(f"/api/feed?since_id={self.last_feed_id}") or {}
        items = feed.get("items") if isinstance(feed, dict) else feed
        items = items if isinstance(items, list) else []
        if items:
            try:
                self.last_feed_id = max(int(i.get("id") or 0) for i in items)
            except Exception:
                pass
            # The slate the player was shown. `state["choices"]` is only
            # written by the observe reground, so it still held the OPENING
            # slate nine turns in; the feed item is what the client rendered.
            for i in items:
                if i.get("type") in ("player_choice_prompt", "choices_revised") and i.get("choices"):
                    self.last_slate = [_clip(_label(c), 80) for c in i["choices"]]
        enc = st.get("encounter") or {}
        enc_out = st.get("encounter_outcome") or {}
        if not self.goal:
            self.goal = str(st.get("level_goal") or "")
        row = {
            "label": label,
            "turn": st.get("turn_count"),
            "phase": st.get("current_phase"),
            "threat": st.get("threat_level"),
            "chaos": st.get("chaos_level"),
            "time_of_day": st.get("time_of_day"),
            "detection": status.get("detection"),
            "detection_heat": status.get("detection_heat"),
            "detection_source": status.get("detection_source"),
            "alive": (st.get("player_state") or {}).get("alive"),
            "condition": (st.get("player_state") or {}).get("condition"),
            "fate_in_state": st.get("fate"),
            "level_goal": st.get("level_goal"),
            "goal_reached_turn": st.get("goal_reached_turn"),
            "pending_cutscene": bool(st.get("pending_cutscene")),
            "lead": {"lead": lead.get("lead"), "detail": _clip(lead.get("detail"), 160),
                     "generated": lead.get("generated")},
            "environment_streak": st.get("environment_streak"),
            "seen_elements": len(st.get("seen_elements") or []),
            "seen_tail": [_clip(x, 40) for x in (st.get("seen_elements") or [])[-4:]],
            "scene_objects": [_clip(_label(o), 30) for o in (st.get("scene_objects") or [])],
            "scene_objects_turn": st.get("scene_objects_turn"),
            "recent_events_tail": [_clip(x, 120) for x in (st.get("recent_events") or [])[-1:]],
            "narrator_recent": [_clip(x, 140) for x in (st.get("narrator_recent") or [])[-2:]],
            "narrator_beat": st.get("narrator_beat"),
            "inventory": st.get("inventory"),
            "companions": list((st.get("companions") or {}).keys())[:6],
            "encounter": {
                "open": bool(enc),
                "label": _clip(((enc.get("character") or {}).get("label")), 40),
                "kind": (enc.get("character") or {}).get("kind"),
                "stance": (enc.get("character") or {}).get("stance"),
                "round_no": enc.get("round_no"),
                "opened_at_detection": enc.get("detection"),
                "enemy_state": enc.get("enemy_state"),
                "setting_kept": bool(enc.get("setting")),
                "travel_remain": st.get("encounter_travel_remain"),
                "last_turn": st.get("encounter_last_turn"),
                "last_label": _clip(st.get("encounter_last_label"), 40),
                # The figure the frame rolled this turn (engine.api_detect's
                # sighting), if any — the trigger the encounter now has.
                "sighting": ({
                    "label": _clip(sight.get("label"), 40), "kind": sight.get("kind"),
                    "distance": sight.get("distance"), "figures": sight.get("figures"),
                    "turn": sight.get("turn"), "crop": bool(sight.get("crop_path")),
                } if isinstance((sight := (st.get("encounter_sighting") or {})), dict) and sight else None),
                "outcome": {k: enc_out.get(k) for k in ("outcome", "lane", "alive", "condition", "enemy_state", "round_no", "fate") if k in enc_out},
            },
            "last": {
                "choice": _clip(last.get("choice"), 120),
                "source_flags": {k: last.get(k) for k in ("is_custom_action", "hard_transition", "encounter", "cached_opening", "live_capture") if k in last},
                "dispatch": _clip(last.get("dispatch"), 360),
                "vision_dispatch": _clip(last.get("vision_dispatch"), 240),
                "vision_analysis": _clip(last.get("vision_analysis"), 240),
                "setting_type": last.get("setting_type"),
                "spatial": _clip(last.get("spatial_compass"), 80),
                "image": os.path.basename(str(last.get("image") or "")),
            },
            "feed_since_last": [{"id": i.get("id"), "type": i.get("type"),
                                 "hard": ((i.get("metadata") or {}).get("hard_transition")),
                                 "text": _clip(i.get("content"), 80)} for i in items][-12:],
            "choices": list(self.last_slate),
            "client": self._client(page),
            "world": {"id": st.get("experience_world_id"), "turns": st.get("world_turn_count"),
                      "pending_transition": st.get("pending_world_transition")},
        }
        self.rows.append(row)
        if log:
            log(f"  [trace] t={row['turn']} {row['phase']}/threat={row['threat']} "
                f"det={row['detection']}({row['detection_heat']},{row['detection_source'] or '-'}) "
                f"tod={row['time_of_day']} lead={_clip(row['lead']['lead'], 60)!r} "
                f"enc={'OPEN ' + str(row['encounter']['label']) if row['encounter']['open'] else 'closed'} "
                f"hard={row['last']['source_flags'].get('hard_transition')} img={row['last']['image']}")
        self.save()
        return row

    def save(self):
        try:
            with open(os.path.join(SHOTS, "loop_trace.json"), "w", encoding="utf-8") as f:
                json.dump({"goal": self.goal, "rows": self.rows}, f, indent=1)
        except Exception:
            pass

    def report(self, log, findings):
        """What the run says about the systems talking to each other."""
        if not self.rows:
            return
        log("\n=========== LOOP FLOW ============")
        log(f"  run goal (state.level_goal): {_clip(self.goal, 120)!r}")
        goal_words = [w for w in re.findall(r"[a-z]+", (self.goal or "").lower())
                      if len(w) > 3 and w not in CHOICE_STOP and w not in
                      ("marked", "faint", "glowing", "reinforced", "end", "hall", "with", "that", "which")]
        goal_hits_prose, goal_hits_choice, goal_hits_lead, goal_hits_narr = 0, 0, 0, 0
        turns = 0
        first_esc, first_crit = None, None
        det_track = []
        for r in self.rows:
            if r["label"] == "start":
                continue
            turns += 1
            low = (r["last"]["dispatch"] + " " + r["last"]["vision_dispatch"]).lower()
            if any(w in low for w in goal_words):
                goal_hits_prose += 1
            if any(w in " ".join(r["choices"]).lower() for w in goal_words):
                goal_hits_choice += 1
            if any(w in ((r["lead"]["lead"] or "") + " " + (r["lead"]["detail"] or "")).lower() for w in goal_words):
                goal_hits_lead += 1
            if any(w in " ".join(r["narrator_recent"]).lower() for w in goal_words):
                goal_hits_narr += 1
            if r["phase"] == "escalating" and first_esc is None:
                first_esc = r["turn"]
            if r["phase"] == "critical" and first_crit is None:
                first_crit = r["turn"]
            det_track.append((r["turn"], r["detection"], r["detection_heat"], r["detection_source"]))
        log(f"  turns traced: {turns}   escalating at turn {first_esc}   critical at turn {first_crit}")
        reached = next((r["goal_reached_turn"] for r in self.rows if r.get("goal_reached_turn")), None)
        log(f"  goal reached: {'turn ' + str(reached) if reached else 'never (no goal_reached_turn on the run)'}")
        log(f"  goal words {goal_words} appeared in: prose {goal_hits_prose}/{turns} turns, "
            f"choices {goal_hits_choice}/{turns}, HUD lead {goal_hits_lead}/{turns}, narrator {goal_hits_narr}/{turns}")
        log("  detection by turn: " + ", ".join(f"t{t}:{d}({h},{s or '-'})" for t, d, h, s in det_track))
        leads = []
        for r in self.rows:
            l = r["lead"]["lead"]
            if l and (not leads or leads[-1] != l):
                leads.append(l)
        log("  HUD leads in order: " + " -> ".join(_clip(l, 50) for l in leads))
        for r in self.rows:
            if r["encounter"]["outcome"]:
                o = r["encounter"]["outcome"]
                log(f"  encounter at turn {r['turn']}: {r['encounter']['label']!r} outcome={o.get('outcome')} "
                    f"lane={o.get('lane')} fate_in_state={r['fate_in_state']!r} "
                    f"opened_at_detection={r['encounter']['opened_at_detection']} now={r['detection']}({r['detection_heat']})")
                break
        tods = [r["time_of_day"] for r in self.rows]
        log(f"  time_of_day across the run: {sorted(set(str(t) for t in tods))}")
        # The findings are the handoffs that did NOT happen.
        if self.goal and turns >= 4 and goal_hits_prose == 0 and goal_hits_choice == 0:
            findings.append(
                f"the run's goal ({_clip(self.goal, 60)!r}) never appeared in any "
                f"dispatch or choice across {turns} turns - the goal is not reaching the simulation")
        # The LEAD is a durable category by design ("Document A Specimen"),
        # so the goal is not expected there — it has its own GOAL row, fed by
        # /api/objectives. Its absence is the two systems not talking.
        shown = [o for r in self.rows for o in ((r.get("client") or {}).get("objectives") or [])
                 if (o or {}).get("kind") == "destination"]
        if self.goal and turns >= 2 and not shown:
            findings.append(
                "the objectives tracker never showed the run's goal (no GOAL row) - "
                "the objectives HUD and the level goal are two unrelated systems")
        elif shown:
            last = shown[-1]
            log(f"  GOAL row on the tracker: {_clip(last.get('title'), 70)!r} status={last.get('status')}")
        if any(r["encounter"]["outcome"] for r in self.rows) and all(r["fate_in_state"] is None for r in self.rows):
            findings.append(
                "an encounter resolved but state['fate'] was never written - every fight rolls NORMAL "
                "regardless of the turn's luck")
        # The product clock is 3 / 6 (critical by turn 3-6 by design); what
        # burns out is a clock that is over before the story starts.
        if first_crit is not None and first_crit <= 2:
            findings.append(
                f"the story reached CRITICAL on turn {first_crit} and can never leave it - "
                f"the experience's threat marks burn the arc out before the story starts")


class FlipbookWatch:
    """Did this turn actually draw, deliver and PLAY a flipbook?

    Three separate things, and every one of them has its own failure that
    looks identical from the outside — a turn that resolved onto a still:

      · the engine never drew a grid (refused generation, still fallback),
      · the grid arrived but the client dropped it (the reactor branch used to
        eat every sequence before it reached the stills renderer),
      · the frames played but are the same picture (a grid of one pose).
    """

    def __init__(self, origin, enabled):
        self.origin = origin
        self.enabled = enabled
        self.turns = []          # (turn, frame_count, painted, motion)
        self._cache = {}

    def install(self, page, log):
        try:
            ok = page.evaluate(FLIPBOOK_TAP)
        except Exception as exc:
            log(f"!! could not watch flipbook playback: {a(str(exc))[:80]}")
            return False
        if not ok:
            log("!! Renderer not up yet — flipbook playback is going unwatched")
        return bool(ok)

    def turn(self, page, log, findings, turn, action):
        try:
            drained = page.evaluate(FLIPBOOK_DRAIN)
        except Exception:
            return
        if not drained:
            return
        beats = [b for b in drained["beats"] if b["frames"]]
        painted = [p["url"] for p in drained["paints"]]
        if not beats:
            if self.enabled and action not in READ_ONLY_ACTIONS:
                log("  flipbook: no sequence on this turn — it drew a plain still")
                self.turns.append((turn, 0, 0, None))
            return

        seq = beats[-1]
        urls = seq["frames"]
        # A frame counts as played when its own URL was painted onto a scene
        # layer. Substring, because the style URL is absolute and the beat's
        # is the server-relative path.
        hit = sum(1 for u in urls if any(u.split("/")[-1] in p for p in painted))
        motion, missing = panel_motion(self.origin, urls, self._cache)
        self.turns.append((turn, len(urls), hit, motion))

        log(f"  flipbook: {len(urls)} frames at {seq['frame_ms'] or 420}ms, "
            f"{hit} of them painted"
            + (f", panel-to-panel {motion}" if motion else ""))

        if len(urls) < 2:
            findings.append(f"turn {turn}: flipbook delivered {len(urls)} frame(s) "
                            f"- there is no motion in a one-frame sequence")
            return
        if missing:
            findings.append(f"turn {turn}: {missing} flipbook frame(s) would not "
                            f"load from the server")
        if hit < 2:
            findings.append(
                f"turn {turn}: the server sent {len(urls)} flipbook frames but "
                f"only {hit} reached the screen - the motion was dropped between "
                f"the beat and the scene layer")
        elif hit < len(urls):
            findings.append(
                f"turn {turn}: flipbook played {hit} of {len(urls)} frames - "
                f"playback did not reach the frame the action ends on")
        if seq["still"] and urls and urls[-1].split("/")[-1] not in seq["still"]:
            findings.append(
                f"turn {turn}: the turn's still is not the sequence's last panel "
                f"- the picture will jump when the motion stops")
        # Essentially the same picture, not merely similar. A real in-between
        # on this build sits around 0.90-0.98; 0.995 is a grid of one pose.
        if motion and min(motion) > 0.995:
            findings.append(
                f"turn {turn}: the flipbook panels are the same picture "
                f"(panel-to-panel {motion}) - the turn animates as a freeze")

    def report(self, log, findings):
        if not self.enabled:
            log("flipbook is OFF for this run")
            return
        drew = [t for t in self.turns if t[1] >= 2]
        log(f"\n=========== FLIPBOOK ============")
        log(f"  turns that drew a sequence: {len(drew)} of {len(self.turns)}")
        for turn, n, hit, motion in self.turns:
            log(f"  turn {turn}: {n} frames, {hit} painted"
                + (f", motion {motion}" if motion else ""))
        if self.turns and not drew:
            findings.append(
                "flipbook is ON but not one turn drew a sequence - every turn "
                "fell back to a plain still")


def do_custom(page, log, typed):
    """Type an action into the wheel's free-will box and commit it.

    Waits for the wheel to be BOTH present and idle first. Typing into a turn
    that is still resolving gets an HTTP 409 from /api/choose, and the first
    version of this did exactly that on turn one — then every later turn in the
    run "resolved" in 1.5s with no frames, and the report blamed the game for
    dropping four typed actions it had never actually been given.
    """
    open_fist(page, log)
    for _ in range(40):
        try:
            ready = page.evaluate(
                "() => !document.body.classList.contains('turn-active')"
                " && !!document.querySelector('.choice-btn-custom')")
        except Exception:
            ready = False
        if ready:
            break
        time.sleep(1.0)
    else:
        log("    the wheel never came back — nothing to type into")
        return None
    try:
        page.click(".choice-btn-custom", timeout=10000)
        page.wait_for_selector("#custom-input", state="visible", timeout=8000)
        page.fill("#custom-input", typed, timeout=8000)
        page.click("#custom-submit", timeout=8000)
    except Exception as exc:
        log(f"    could not type an action: {a(str(exc))[:90]}")
        return None

    # Wait for the turn to START before anyone waits for it to finish.
    #
    # The ACT line is appended to the prose feed the instant it is submitted,
    # and wait_advance's test is "prose grew AND the turn is not active" — so
    # in the gap between the submit and the ceremony setting `turn-active`,
    # both are true and the turn reads as already resolved. Measured: four
    # turns in a row "resolved in 1.5s" with no frames, the harness typed the
    # next action into a turn that was still running, /api/choose answered 409,
    # and the run reset itself mid-battery. Every one of those was filed
    # against the game.
    for _ in range(20):
        time.sleep(0.5)
        try:
            if page.evaluate(
                    "() => document.body.classList.contains('turn-active')"):
                break
        except Exception:
            continue
    return f"TYPED: {typed}"


def judge_custom(page, log, findings, turn, typed, words, fresh, holds):
    """Did the world DO it, and — when it relocated the player — is it still
    true a beat later?

    Two separate failures with one symptom. The prose can carry the action and
    the next frame drop it (the camera used to stand a driver back on his feet),
    or the prose can refuse it outright ("you attempt to take flight, but...").
    A player cannot tell those apart and should not have to.
    """
    did = honoured(fresh, words)
    log(f"    carried out: {did or 'NO — none of ' + str(list(words))}")
    if not did:
        findings.append(
            f"turn {turn}: typed '{a(typed)}' and the world did not do it — "
            f"none of {list(words)} appear in what followed")

    # The slate for the NEXT decision is generated by this turn, from the frame
    # this turn drew. If the action changed what the player is doing, standing
    # on, or holding, the slate is where that shows up — or does not.
    slate = []
    for _ in range(16):
        time.sleep(1.0)
        try:
            slate = [s for s in page.evaluate(
                "() => Array.from(document.querySelectorAll("
                "'.choice-btn:not(.choice-btn-custom)'))"
                ".map(n => (n.dataset.choiceText || n.innerText || '').trim())") if s]
        except Exception:
            slate = []
        if slate:
            break
    log(f"    next slate: {[a(s) for s in slate]}")
    if not slate or not holds:
        # A one-off act is finished. A slate that has moved on to what comes
        # next is correct, not forgetful.
        return
    kept = honoured(" ".join(slate), words)
    if kept:
        log(f"    the world is still in it: {kept}")
    else:
        findings.append(
            f"turn {turn}: '{a(typed)}' RELOCATED the player and was then "
            f"undone — the next slate ({[a(s) for s in slate]}) has no sign of "
            f"{list(words)}, so the world put them back where they were")


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
    # A tag can leave under the cursor: when the scene re-reads itself the
    # labels the new pass did not find are retired with a 420ms fade, and the
    # STATE snapshot above still listed them. Clicking one raised straight
    # out of main() and ended a run at turn 5 with no SUMMARY. Aim only at
    # tags that are staying, and if the one we wanted has gone, re-read the
    # frame and take whatever is actually on screen now.
    try:
        page.click(f".scan-tag:not(.leaving) >> nth={idx}", timeout=8000)
    except Exception as exc:
        log(f"    tag {idx} ({a(labels[idx])}) left before it could be clicked "
            f"({a(str(exc))[:60]}); re-reading the frame")
        time.sleep(1.5)
        s = page.evaluate(STATE)
        if not s["tags"]:
            log("    no tags left to click")
            return None
        labels = [t["label"] for t in s["tags"]]
        idx = idx % len(labels)
        page.click(f".scan-tag:not(.leaving) >> nth={idx}", timeout=8000)

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

        # PT_SESSION=<id> plays in a session of its own, so a run somebody is
        # in the middle of on `default` is left exactly where it was. The
        # page is put back on the plain URL when the harness is done.
        home_url = page.url
        if os.environ.get("PT_SESSION"):
            sid = os.environ["PT_SESSION"].strip()
            origin0 = page.evaluate("() => location.origin")
            log(f">>> playing in session {sid!r}; the default run is left alone")
            page.goto(f"{origin0}/standalone?session={sid}", wait_until="load")
            page.wait_for_function("() => !!window.Renderer", timeout=60000)
            time.sleep(3)
        elif os.environ.get("PT_RELOAD") == "1":
            log(">>> reloading client for current CSS/JS")
            page.reload(wait_until="load")
            page.wait_for_function("() => !!window.Renderer", timeout=60000)
            time.sleep(3)

        # Watch the motion before the run starts, so the opening beat is seen
        # too. Whether flipbook is ON is the server's answer, not an assumption:
        # a harness that decides for itself files "no sequences" against a build
        # that was never asked to draw any.
        origin = page.evaluate("() => location.origin")
        try:
            status = json.loads(frame_bytes(origin, "/api/status") or b"{}")
            status = status.get("data") or status
            fb_on = bool((status.get("flipbook") or {}).get("enabled"))
        except Exception:
            fb_on = False
        log(f">>> flipbook is {'ON' if fb_on else 'OFF'} on the server")
        flip = FlipbookWatch(origin, fb_on)
        flip.install(page, log)
        trace = LoopTrace(origin) if TRACE_ON else None
        if trace:
            log(">>> LOOP TRACE on: every turn's state, lead, dials and encounter go to "
                f"{SHOTS}/loop_trace.json")

        if not start_run(page, log):
            log("!! could not start a run")
            return
        if trace:
            trace.snapshot(page, "start", log)

        # Rotate the verbs a player would actually use.
        #
        # CAMP is deliberately absent. The hub reduction removed it from play
        # along with SCAN's button and PLAY ("the choice stack IS the turn
        # interface now"), and #camp-btn is hidden with !important at the end of
        # standalone.css. The server still has /api/camp/enter, but nothing in
        # the UI can reach it, so driving it here only ever produced a click
        # timeout filed as a game failure. `camp_is_still_unreachable` below
        # watches for the button coming back instead.
        plan = ["choice", "choice", "scan_move", "photo", "scan_interact",
                "encounter", "act"]
        # PT_CUSTOM=1 drives the typed-action battery instead of the verb
        # rotation. Free will is the part of the game with the fewest rails and
        # the most gates that can quietly refuse it, so it gets a run of its
        # own rather than one "act" turn in seven.
        if os.environ.get("PT_CUSTOM") == "1":
            plan = ["custom"] * len(CUSTOM_BATTERY)
            log(f">>> TYPED-ACTION RUN: {len(CUSTOM_BATTERY)} actions")
        # PT_PLAN=choice,scan_move,encounter,... plays a specific verb order.
        # A loop trace wants the encounter late (once detection has moved) as
        # well as early, which the fixed rotation never does.
        if os.environ.get("PT_PLAN"):
            wanted = [v.strip() for v in os.environ["PT_PLAN"].split(",") if v.strip()]
            known = {"choice", "scan_move", "scan_interact", "photo", "encounter", "act", "custom"}
            bad = [v for v in wanted if v not in known]
            if bad:
                log(f"!! PT_PLAN has unknown verbs {bad}; using the rotation")
            elif wanted:
                plan = wanted
                log(f">>> PT_PLAN: {plan}")

        sightings_played = 0
        for turn in range(1, TURNS + 1):
            s = page.evaluate(STATE)
            if s["gameOver"]:
                log(f"\n--- turn {turn}: GAME OVER — stopping")
                break

            # A SIGHTING opens a confrontation BETWEEN turns: the auto-scan on
            # the painted frame found a person and the client handed the
            # screen to the Encounter Moment (engine.api_detect →
            # encounter_with). Play it out before the planned verb, or the
            # click lands on a Moment and the turn is filed as "no action".
            # The sighting arrives ~1.6 s after the frame paints (the
            # auto-scan settles 0.7 s, detects, then waits one beat), and a
            # harness that acts the instant the prose lands is faster than
            # any player — Encounter.start() then declines on `processing`
            # and the turn is filed as "no action". Give the frame the time a
            # person would take to look at it.
            sighted = False
            for _ in range(6):
                time.sleep(0.5)
                try:
                    if page.evaluate(ENCOUNTER_STATE)["inEncounter"]:
                        sighted = True
                        break
                except Exception:
                    pass
            if sighted:
                log(f"\n--- before turn {turn}: a sighting opened an encounter — playing it out")
                played = play_out_encounter(page, log, findings)
                log(f"    sighting encounter: {played or 'never resolved'}")
                sightings_played += 1
                if trace:
                    trace.snapshot(page, f"turn{turn}:sighting", log)
                s = page.evaluate(STATE)
                if s["gameOver"]:
                    log(f"\n--- turn {turn}: GAME OVER in the sighting — stopping")
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

            typed, words, holds = "", (), False
            if action == "custom":
                typed, words, holds = CUSTOM_BATTERY[(turn - 1) % len(CUSTOM_BATTERY)]
                log(f"  typing: {typed!r}")
                did = do_custom(page, log, typed)
            elif action == "choice":
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
                # ACT is not a hub button any more. The hub reduction made the
                # choice stack the entire turn interface: the last row, labelled
                # "Custom", opens the same free-will input that #free-will-btn
                # used to. That button is now `display: none !important`
                # (standalone.css, end of file), so clicking it waits out the
                # timeout and files the *game* as broken for a UI change.
                try:
                    open_fist(page, log)
                    page.click(".choice-btn-custom", timeout=8000)
                    page.wait_for_selector("#custom-input", state="visible", timeout=8000)
                    typed = "Search the ground for tracks"
                    page.fill("#custom-input", typed, timeout=8000)
                    page.click("#custom-submit", timeout=8000)
                    did = f"ACT (typed): {typed}"
                except Exception as e:
                    log(f"    ACT failed: {a(str(e))[:120]}")

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
                flip.turn(page, log, findings, turn, action)
                if trace:
                    trace.snapshot(page, f"turn{turn}:{action}", log)
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
                if trace:
                    trace.snapshot(page, f"turn{turn}:{action}:gameover", log)
                break
            log(f"  resolved in {el:.1f}s{f' ({black} black samples)' if black else ''}")
            flip.turn(page, log, findings, turn, action)
            if trace:
                trace.snapshot(page, f"turn{turn}:{action}", log)

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

            if action == "custom" and did:
                judge_custom(page, log, findings, turn, typed, words, fresh,
                             holds)

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
            #
            # ESSENTIALLY UNCHANGED, not merely similar. This was 0.90 and cried
            # wolf on nearly every run, which is worse than not checking: three
            # consecutive runs flagged a turn, and on inspection the frames
            # showed the camera genuinely travelling. Sprinting twenty metres
            # down a road toward a building already on the horizon SHOULD score
            # high — same sky, same ground, same light, the same landmarks
            # getting closer. A score low enough to satisfy 0.90 would mean a
            # teleport, which is the opposite bug and the one this game has
            # spent months removing.
            #
            # Measured on this build: real failures (frame returned with only a
            # detail added) sat at 0.96+; correct forward moves came in at
            # 0.85-0.93. 0.95 separates them. Raised deliberately and with the
            # frames looked at, not to make a red light go green — see the
            # docstring on continuity(), which warns against exactly the
            # threshold-picking this line does.
            if prev_turn in move_choices and score > 0.95:
                findings.append(
                    f"turn {prev_turn}: chose to move "
                    f"('{a(move_choices[prev_turn])}') but the frame barely "
                    f"changed (continuity {score:.2f}) - the world did not "
                    f"actually take the player anywhere"
                )

        # CAMP was removed from the UI on purpose, so its absence is correct and
        # must not be reported as a fault. What IS worth knowing is the reverse:
        # if the button comes back, the plan above should drive it again. This
        # keeps the removal a recorded decision rather than something a future
        # run rediscovers as a mystery.
        try:
            camp_reachable = page.evaluate(
                """() => {
                  const b = document.getElementById('camp-btn');
                  if (!b) return false;
                  const cs = getComputedStyle(b);
                  return cs.display !== 'none' && cs.visibility !== 'hidden'
                         && parseFloat(cs.opacity || '1') > 0.05;
                }""")
            if camp_reachable:
                findings.append(
                    "CAMP is reachable again (#camp-btn is visible) - it was "
                    "removed by the hub reduction and the turn plan no longer "
                    "drives it, so this feature is now going untested")
        except Exception:
            pass

        flip.report(log, findings)
        log(f"\nSIGHTINGS: {sightings_played} encounter(s) opened by a person in "
            f"frame and played out between turns")
        if trace:
            trace.report(log, findings)
            # One frame of the objectives sheet as the player sees it — the
            # trace records what the tracker HOLDS; this is what it SHOWS.
            try:
                page.evaluate("() => { window.Objectives && Objectives.open(); }")
                time.sleep(0.8)
                snap(page, "objectives_open.png")
                page.evaluate("() => { window.Objectives && Objectives.close(); }")
                log(f"  objectives sheet captured: {SHOTS}/objectives_open.png")
            except Exception as exc:
                log(f"  could not capture the objectives sheet: {a(str(exc))[:80]}")

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
        # An empty Reactor balance is a real fact about the account, not a defect
        # in the build: the client already classifies it as terminal and falls
        # back to stills immediately. Reported separately because the browser's
        # own "Failed to load resource: 402" cannot be suppressed, and left in the
        # main list it appeared on every single run — which is exactly how a
        # genuine console error gets skimmed past.
        env = [e for e in uniq if ENVIRONMENT_NOISE.search(e or "")]
        real = [e for e in uniq if e not in env]
        if real:
            log("\nCLIENT CONSOLE ERRORS:")
            for e in real[:12]:
                log(f"  {a(e)}")
        else:
            log("no client console errors")
        if env:
            log("\nENVIRONMENT (not a code fault):")
            for e in env[:4]:
                log(f"  {a(e)[:200]}")

        if os.environ.get("PT_SESSION"):
            try:
                page.goto(home_url, wait_until="load")
                log(f">>> page put back on {home_url}")
            except Exception as exc:
                log(f"!! could not put the page back: {a(str(exc))[:80]}")

    with open(os.path.join(SHOTS, "REPORT.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    main()
