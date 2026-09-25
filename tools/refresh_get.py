#!/usr/bin/env python3
"""Re-shoot the /get page's clips from the game as it is today.

    python tools/refresh_get.py              # film, find the moments, cut, open a review page
    python tools/refresh_get.py --publish    # put the last candidate on the site
    python tools/refresh_get.py --no-shoot   # re-find and re-cut from the last films
    python tools/refresh_get.py --films key  # re-film only these (the rest reuse the last films)
    python tools/refresh_get.py --parallel   # two films at a time (more at once makes choppy footage)

The shot list is tools/get_shoot.json: which films to shoot (a run in each
Experience — The FIFTH CORNER, SWAT, CYBER HORROR — a character made on
camera, a first launch with no key) and
which moment of which film makes each clip. Nothing here names a second of a
recording, so nothing goes stale when the game changes:

  1. FILM   tools/film_run.py plays the real game in a sandbox (never your
            roster, Worlds, prompts or `default` session) and records a 30 fps
            master plus timeline.json: the page's state every 250 ms and the
            harness's play-by-play, on the video's clock.
  2. FIND   each clip's moment is read off that timeline — the body classes
            the game sets (moment-encounter, char-open, keys-open,
            moment-cutscene, turn-active …) and the harness's lines (who the
            fight was with and what it did, which turns drew a flipbook).
  3. CUT    tools/cut_clips.py cuts them into a candidate folder, skipping
            every still stretch (loading, a picture developing) so they play
            as continuous action.
  4. LOOK   _claude_get/refresh/<stamp>/review.html: each new clip beside the
            one on the site now, with what was picked and why. A clip whose
            moment never happened in this shoot keeps the one on the site.
  5. PUBLISH --publish copies the candidate into static/video/get, the link
            preview into static/img/get/og.jpg, and the spec it was cut from
            into tools/get_clips.json. Commit those and deploy.

Needs a Gemini key in .env (a mock run would film canned text; film_run
refuses). Films run one at a time by default, two with --parallel: each is a
server, a headless Chrome and a live 1080p encode, and five at once dropped
this PC to ~13 painted frames a second, which films as stutter.
Films live in _claude_film/get-<name>-<stamp>/; ones no longer used by the
site or the latest candidate are deleted after a publish.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOOT = ROOT / "tools" / "get_shoot.json"
PUBLISHED_SPEC = ROOT / "tools" / "get_clips.json"
SITE = ROOT / "static" / "video" / "get"
OG = ROOT / "static" / "img" / "get" / "og.jpg"
WORK = ROOT / "_claude_get" / "refresh"
FILMS_INDEX = WORK / "films.json"
FILM_ROOT = ROOT / "_claude_film"

PORTS = {"run": (5041, 9371), "create": (5042, 9372), "key": (5043, 9373),
         "swat": (5044, 9374), "cyber": (5045, 9375), "wear": (5046, 9376)}


def say(msg: str) -> None:
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


def rel(p: Path) -> str:
    try:
        return p.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(p)


# ── 1. film ────────────────────────────────────────────────────────────

def film_cmd(name: str, opts: dict, out: Path, port: int, cdp: int) -> list:
    cmd = [sys.executable, "-u", str(ROOT / "tools" / "film_run.py"), "--out", str(out),
           "--port", str(port), "--cdp", str(cdp), "--session", f"get_{name}"]
    flags = {"plan": "--plan", "turns": "--turns", "create": "--create", "character": "--character",
             "linger": "--linger", "max_minutes": "--max-minutes", "experience": "--experience",
             "first_launch": "--first-launch", "act_lines": "--act-lines"}
    for key, flag in flags.items():
        if opts.get(key) not in (None, ""):
            cmd += [flag, str(opts[key])]
    if opts.get("give"):
        cmd.append("--give")
    # A kit: things put in the played character's pack (the copy) so one
    # character can be seen suiting up piece by piece.
    if opts.get("kit"):
        cmd += ["--kit", str(ROOT / opts["kit"])]
    # Characters left out of the film's roster copy (a starter who is someone
    # else's character never appears in the marketing, even in the list).
    if opts.get("hide"):
        cmd += ["--hide", ",".join(opts["hide"])]
    return cmd


def shoot(names: list, films: dict, jobs: int) -> dict:
    """Film `names`, at most `jobs` at a time. Each film is a server, a headless
    Chrome and a live 1080p encode: five at once took this PC's painted rate
    from ~60 to ~13 frames a second (choppy clips) and timed a harness out."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    running = {}
    outs = {}
    queue = list(names)
    while queue or running:
        while queue and len(running) < max(1, jobs):
            name = queue.pop(0)
            out = FILM_ROOT / f"get-{name}-{stamp}"
            port, cdp = PORTS.get(name, (5050 + len(outs), 9380 + len(outs)))
            cmd = film_cmd(name, films[name], out, port, cdp)
            log = open(WORK / f"film-{name}.log", "w", encoding="utf-8")
            say(f"filming {name} into {rel(out)}")
            running[name] = (subprocess.Popen(cmd, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT), log)
            outs[name] = out
        time.sleep(3)
        for name, (p, log) in list(running.items()):
            if p.poll() is not None:
                log.close()
                del running[name]
    done = {}
    for name, out in outs.items():
        ok = (out / "master.mp4").is_file() and (out / "timeline.json").is_file()
        say(f"  {name}: {'filmed' if ok else 'NO FILM — see ' + rel(WORK / f'film-{name}.log')}")
        if ok:
            done[name] = rel(out)
    return done


# ── 2. find the moments ────────────────────────────────────────────────

class Film:
    def __init__(self, folder: Path):
        self.folder = folder
        t = json.loads((folder / "timeline.json").read_text(encoding="utf-8"))
        self.samples = t.get("samples") or []
        self.log = t.get("log") or []
        self.end = self.samples[-1][0] if self.samples else 0.0

    def spans(self, cls: str) -> list:
        out, start = [], None
        for s in self.samples:
            on = cls in (s[1] or "").split()
            if on and start is None:
                start = s[0]
            elif not on and start is not None:
                out.append((start, s[0]))
                start = None
        if start is not None:
            out.append((start, self.end))
        return out

    def lines(self, a: float = 0.0, b: float = 1e9) -> list:
        return [(t, line) for t, line in self.log if a <= t <= b]

    def first_line(self, pattern: str):
        rx = re.compile(pattern)
        return next(((t, line) for t, line in self.log if rx.search(line)), None)

    def clamp(self, a: float, b: float) -> tuple:
        return max(0.0, a), min(self.end, b)


def _fights(film: Film) -> list:
    out = []
    for i, (a, b) in enumerate(film.spans("moment-encounter"), 1):
        who, dealt, taken, rounds = "", 0, 0, 0
        for _, line in film.lines(a - 2, b + 5):
            m = re.search(r"bars: \S+ \d+/\d+ \| (.+?) \d+/\d+", line)
            if m and not who:
                who = m.group(1).title()
            m = re.search(r"battle r\d+: .*you (\d+)->(\d+) them (\d+)->(\d+)", line)
            if m:
                rounds += 1
                taken += int(m.group(1)) - int(m.group(2))
                dealt += int(m.group(3)) - int(m.group(4))
        out.append({"n": i, "a": a, "b": b, "who": who or "someone", "dealt": dealt,
                    "taken": taken, "rounds": rounds})
    return out


def _turns(film: Film) -> list:
    """Every turn the harness played: kind, what was done, its span, its flipbook."""
    heads = [(t, int(m.group(1)), m.group(2)) for t, line in film.log
             for m in [re.match(r"--- turn (\d+) \((\w+)\)", line)] if m]
    motion = {}
    for _, line in film.log:
        m = re.match(r"\s*turn (\d+): (\d+) frames, (\d+) painted, motion \[([^\]]*)\]", line)
        if m:
            vals = [float(v) for v in m.group(4).split(",") if v.strip()]
            motion[int(m.group(1))] = (int(m.group(3)), min(vals) if vals else 1.0)
    active = film.spans("turn-active")
    out = []
    for i, (t, n, kind) in enumerate(heads):
        nxt = heads[i + 1][0] if i + 1 < len(heads) else film.end
        did = next((line.split("committed:", 1)[1].strip() for tt, line in film.lines(t, nxt)
                    if "committed:" in line), "")
        commit = next((tt for tt, line in film.lines(t, nxt) if "committed:" in line), None)
        span = None
        if commit is not None:
            span = next(((a, b) for a, b in active if a - 1.5 <= commit <= b + 0.5), None)
        painted, low = motion.get(n, (0, 1.0))
        out.append({"n": n, "kind": kind, "did": did, "span": span, "painted": painted, "motion": low})
    return out


def find(moment: str, film: Film, clip: dict, taken: list) -> dict:
    """{'segments': [...], 'why': str} or {'missing': str}."""
    lead, tail = float(clip.get("lead", 0)), float(clip.get("tail", 0))
    hold = float(clip.get("hold", 0.6))
    pick = clip.get("pick") or {}
    crop = clip.get("crop")

    def seg(a, b, squeeze=True, crop_it=False):
        a, b = film.clamp(a, b)
        s = {"from": round(a, 2), "to": round(b, 2)}
        if squeeze:
            s.update(squeeze=True, hold=hold)
        if crop_it and crop:
            s["crop"] = crop
        return s

    if moment in ("last_fight", "hardest_fight"):
        fights = _fights(film)
        if not fights:
            return {"missing": "no fight happened in this film"}
        if pick.get("fight"):
            chosen = next((f for f in fights if f["n"] == int(pick["fight"])), None)
            if not chosen:
                return {"missing": f"there is no fight {pick['fight']}"}
        elif moment == "last_fight":
            # The last fight that is not already another clip's footage (the
            # one who steps out on the way into the goal belongs to that clip).
            free = [f for f in fights
                    if all(f["b"] < x["window"][0] or f["a"] > x["window"][1] for x in taken)]
            chosen = (free or fights)[-1]
        else:
            used = [x["span"] for x in taken]
            others = [f for f in fights if (f["a"], f["b"]) not in used] or fights
            chosen = max(others, key=lambda f: (f["dealt"] + f["taken"], f["rounds"], f["a"]))
        taken.append({"span": (chosen["a"], chosen["b"]),
                      "window": (chosen["a"] - lead, chosen["b"] + tail)})
        return {"segments": [seg(chosen["a"] - lead, chosen["b"] + tail)],
                "why": f"fight {chosen['n']} of {len(fights)}, with {chosen['who']}: "
                       f"dealt {chosen['dealt']}, took {chosen['taken']}, {chosen['rounds']} round(s)"}

    if moment == "move":
        turns = [t for t in _turns(film) if t["span"] and t["kind"] in ("choice", "act", "scan_move")]
        if pick.get("turn"):
            turns = [t for t in turns if t["n"] == int(pick["turn"])]
        busy = [x["window"] for x in taken]

        def clear(t):
            a, b = t["span"][0] - lead, t["span"][1] + tail
            return all(b < x or a > y for x, y in busy)
        drew = [t for t in turns if t["painted"] >= 3 and clear(t)] or [t for t in turns if clear(t)] or turns
        if not drew:
            return {"missing": "no turn was played to the end"}
        # Some way into the run (after the first fight), not the first step
        # off the start. The typed action if it drew a sequence, else the turn
        # that moved the most.
        fights = film.spans("moment-encounter")
        later = [t for t in drew if fights and t["span"][0] > fights[0][1]] or drew
        typed = [t for t in later if t["kind"] == "act" and t["painted"] >= 3]
        chosen = typed[0] if typed else min(later, key=lambda t: (t["motion"], -t["n"]))
        a, b = chosen["span"]
        taken.append({"span": (a, b), "window": (a - lead, b + tail)})
        return {"segments": [seg(a - lead, b + tail)],
                "why": f"turn {chosen['n']} ({chosen['kind']}): {chosen['did']!r}, "
                       f"{chosen['painted']} flipbook frames, least panel-to-panel similarity {chosen['motion']:.2f}"}

    if moment == "opening":
        cut = film.spans("moment-cutscene")
        if not cut:
            return {"missing": "the opening montage never played"}
        ca, cb = cut[0]
        over = next(((a, b) for a, b in film.spans("opening-overture") if a <= ca <= b + 5), None)
        segs = [seg(ca - 1.2, ca + 0.3, squeeze=False)]
        start = over[1] - 1.3 if over and over[1] > ca + 1 else ca + 0.3
        segs.append(seg(start, cb + 1.2))
        return {"segments": segs, "why": f"the title card at {ca:.0f}s, then the montage to the first frame"}

    if moment == "create":
        # Every character made on camera, one after another: the line that
        # describes them (a caption, typed out — the field is small), the
        # drawing, and who came back, held.
        made = [(t, line) for t, line in film.log if "created '" in line]
        spans = film.spans("char-open")
        if not made or not spans:
            return {"missing": "no character was created on camera"}
        said = [x for x in (clip.get("_lines") or [])]
        subs = [tt for tt, line in film.log if "CREATE: submitted" in line]
        segs, names = [], []
        for i, (t, line) in enumerate(made):
            m = re.search(r"created '([^']+)'.*playable in (\d+)s", line)
            if not m:
                continue
            names.append(m.group(1))
            # "playable in Ns" is timed from the Enter, and logged 2.5 s later.
            sent = subs[i] if i < len(subs) else t - float(m.group(2)) - 2.6
            what = said[i] if i < len(said) else ""
            # The line going in; the wait for the drawing, squeezed to its
            # last seconds; then the reveal played as it happened (squeezed,
            # a portrait that has landed is "still" and got under a second).
            first = seg(sent - 1.4, sent + 0.2, squeeze=False)
            ready = t - 2.6
            draw = seg(sent + 0.2, ready - 0.4)
            draw.update(keep="end", max=float(clip.get("max_each", 2.5)))
            reveal = seg(ready - 0.4, ready + float(clip.get("reveal_hold", 3.0)), squeeze=False)
            parts = [first, draw, reveal]
            if what:
                for part in parts:
                    part.update(caption=what)
                    if clip.get("caption_box"):
                        part["caption_box"] = clip["caption_box"]
                first["type_on"] = True
            segs += parts
        if not segs:
            a, b = next(((a, b) for a, b in spans if a <= made[0][0] + 2), spans[0])
            return {"segments": [seg(a + 0.3, b)], "why": "made on camera"}
        return {"segments": segs, "why": f"{len(names)} made on camera: " + ", ".join(names)}

    if moment == "wear":
        # The pack open on a fitting: the item picked and WEAR IT (squeezed),
        # then the new pose developing in, played as it happened. One span
        # squeezed and capped from the front lost the pose, which is the
        # point: it lands at the end of a fitting that takes half a minute.
        # "all": every fitting in the film, in order (one character suiting
        # up piece by piece), not just the first.
        fits = [(t, line) for t, line in film.log if "WEAR: putting on" in line]
        packs = film.spans("pack-open")
        if not fits or not packs:
            return {"missing": "nothing was worn on camera"}
        if clip.get("all"):
            # "fits": [1, 2, 3] keeps only those fittings (a fourth that the
            # image model drew without the armour it had just put on).
            keep = clip.get("fits")
            rows = [f for i, f in enumerate(fits, 1) if not keep or i in keep]
        else:
            rows = [fits[int(pick.get("wear", 1)) - 1] if pick.get("wear") else fits[0]]
        segs, put_on = [], []
        for t, line in rows:
            a, b = next(((a, b) for a, b in packs if a - 3 <= t <= b + 1), (t - 2, t + 60))
            item = re.search(r"putting on (.+?) \(", line)
            put_on.append(item.group(1) if item else "something")
            dev = next((tt for tt, ln in film.lines(t, b + 2) if "new pose developed" in ln), None)
            if dev is None or dev - t < 8:
                whole = seg(a + 0.2, b - 0.1)
                whole["keep"] = "end"
                segs.append(whole)
            else:
                pick_it = seg(a + 0.2, t + 2.5)
                pick_it["max"] = float(clip.get("pick_max", 3.5))
                segs += [pick_it, seg(dev - float(clip.get("reveal", 3.2)), b - 0.1, squeeze=False)]
        return {"segments": segs, "why": "put on " + ", then ".join(put_on)}

    if moment == "typed":
        # A typed action: the words going in, then what the world made of
        # them. The game's input is a sliver at the bottom that scrolls, so
        # the words ride over the top of the picture as a caption, typed out
        # while the real one is typed; then the wait is squeezed and the
        # LAST seconds kept (the payoff is the picture it ends on).
        typed = [(t, line.split("typing", 1)[1].strip().strip("'\"")) for t, line in film.log if "ACT: typing" in line]
        if not typed:
            return {"missing": "no action was typed on camera"}
        k = int(pick.get("typed", 0)) - 1 if pick.get("typed") else None
        if k is not None and k >= len(typed):
            return {"missing": f"there is no typed action {k + 1}"}
        rows = [typed[k]] if k is not None else typed
        segs, said = [], []
        for t, what in rows:
            sent = next((tt for tt, line in film.lines(t, t + 60) if "committed: ACT" in line), t + 3.5)
            done = next((tt for tt, line in film.lines(t, t + 400) if "resolved in" in line), None)
            if done is None:
                continue
            first = seg(sent - 1.9, sent + 0.2, squeeze=False)
            first.update(caption=what, type_on=True)
            # The picture it ends on is "still" too: at the clip's hold it
            # was on screen for half a second. The answer holds longer.
            rest = seg(sent + 0.2, done + tail)
            rest.update(caption=what, keep="end", max_less=round(first["to"] - first["from"], 2),
                        hold=float(clip.get("result_hold", 1.6)))
            segs += [first, rest]
            said.append(repr(what))
        if not segs:
            return {"missing": "no typed action resolved"}
        return {"segments": segs, "why": "typed " + " / ".join(said)}

    if moment == "goal":
        def at(pat):
            hit = film.first_line(pat)
            return hit[0] if hit else None
        t0, t1, t2, t3, t4 = (at(r"GOAL: walking to"), at(r"GOAL: at the way in"),
                              at(r"GOAL: reward cutscene"), at(r"GOAL: reward over"),
                              at(r"GOAL: took"))
        if t0 is None or t1 is None:
            return {"missing": "the goal was not reached on camera"}
        name = re.search(r"walking to '([^']+)'", film.first_line(r"GOAL: walking to")[1])
        walk = seg(t0 - 1.0, t1 + 1.5)
        walk["max"] = float(clip.get("walk_max", 9))
        segs = [walk]
        if t2 is not None:
            reward = seg(t1 - 0.3, (t3 if t3 is not None else t2 + 40) + 1.5)
            reward["max"] = float(clip.get("reward_max", 14))
            segs.append(reward)
        if t4 is not None:
            segs.append(seg(t4 - 2.5, t4 + 5.0))
        took = film.first_line(r"GOAL: took")
        end = (t4 + 5.0) if t4 is not None else (t3 or t1) + 2
        taken.append({"span": (t0, end), "window": (t0 - 1, end)})
        return {"segments": segs,
                "why": f"walked to {name.group(1) if name else 'the goal'}"
                       + (", the reward" if t2 is not None else "")
                       + (f", {took[1].split('took', 1)[1].strip()}" if took else "")}

    if moment == "key":
        sheet = film.spans("keys-open")
        verdict = film.first_line(r"verdict: ")
        if not sheet or not verdict:
            return {"missing": "ACCOUNT was never set up on camera"}
        if "OK " not in verdict[1]:
            return {"missing": "the key check did not say Works: " + verdict[1].strip()[:120]}
        ka, kb = sheet[0]
        # From the first thing done on the sheet: the harness's own
        # screenshot of it can sit there for ten seconds of nothing. A film
        # from before that line was logged: the flow from the dropdown to
        # the verdict takes about 19 s.
        began = film.first_line(r"ACCOUNT: choosing")
        start = began[0] - 0.8 if began else max(ka + 0.5, verdict[0] - 19.5)
        segs = [seg(start, kb, crop_it=True)]
        chars = [s for s in film.spans("char-open") if s[0] > kb]
        if chars:
            segs.append(seg(chars[0][1] - 0.9, chars[0][1] - 0.1))
        playable = film.first_line(r"turn 1 playable")
        if playable:
            segs.append(seg(playable[0] - 1.3, playable[0] + 3.5))
        return {"segments": segs, "why": verdict[1].split("|")[0].replace("verdict:", "").strip()}

    return {"missing": f"unknown moment {moment!r}"}


def experience_name(slug: str) -> str:
    try:
        return json.loads((ROOT / "experiences" / f"{slug}.json").read_text(encoding="utf-8")).get("name") or slug
    except Exception:
        return ""


def film_who(film: "Film") -> str:
    hit = film.first_line(r"PLAY as ")
    return hit[1].split("PLAY as", 1)[1].strip() if hit else ""


def sub_line(names: list, loaded: dict) -> str:
    """'SWAT · Luka 'Grizzly' Novak', or the worlds joined when a clip has several."""
    shoot = json.loads(SHOOT.read_text(encoding="utf-8"))["films"]
    worlds = []
    for n in names:
        exp = (shoot.get(n) or {}).get("experience") or ""
        w = experience_name(exp) if exp else ""
        if w and w not in worlds:
            worlds.append(w)
    if len(names) == 1 and names[0] in loaded:
        who = film_who(loaded[names[0]])
        return " · ".join(x for x in (worlds[0] if worlds else "", who) if x)
    return " · ".join(worlds)


def build_spec(shoot_spec: dict, films: dict) -> tuple:
    """(get_clips spec, moments report). Fights are found before the moves
    so a move never repeats a fight's footage."""
    loaded = {n: Film(ROOT / p) for n, p in films.items() if (ROOT / p / "timeline.json").is_file()}
    spec = {"note": "Written by tools/refresh_get.py from tools/get_shoot.json. Which seconds of which "
                    "recording make each /get clip; tools/cut_clips.py reads it.",
            "sources": {n: f"{p}/master.mp4" for n, p in films.items()},
            "clips": {}}
    report = {}
    taken = {}
    order = list(shoot_spec["clips"])
    first = ([n for n in order if shoot_spec["clips"][n]["moment"] == "goal"]
             + [n for n in order if shoot_spec["clips"][n]["moment"] == "last_fight"])
    then = [n for n in order if shoot_spec["clips"][n]["moment"] in ("hardest_fight",)]
    rest = [n for n in order if n not in first and n not in then]
    for name in first + then + rest:
        clip = shoot_spec["clips"][name]
        # "film" is one film, or a list: the first one the moment happened in
        # (a fight did not roll in one world, take it from the next), or, with
        # "each": true, the moment from every one of them in turn (the same
        # beat in several worlds).
        # An entry can also be {"film": "swat", "pick": {"typed": 2}}: the
        # same film twice with different picks, in the order written (the
        # wildest typed action first, whichever world it was in).
        entries = clip["film"] if isinstance(clip["film"], list) else [clip["film"]]
        entries = [e if isinstance(e, dict) else {"film": e} for e in entries]
        names = [e["film"] for e in entries]
        segs, whys, misses, used = [], [], [], []
        for entry in entries:
            fname = entry["film"]
            film = loaded.get(fname)
            if film is None:
                misses.append(f"no {fname} film")
                continue
            this = dict(clip, pick=entry.get("pick") or clip.get("pick") or {})
            if clip["moment"] == "create":
                this["_lines"] = [x.strip() for x in (shoot_spec["films"].get(fname, {}).get("create") or "").split("||")
                                  if x.strip()]
            found = find(clip["moment"], film, this, taken.setdefault(fname, []))
            if "segments" not in found:
                misses.append(f"{fname}: {found['missing']}")
                continue
            parts = [dict(s, src=fname) for s in found["segments"]]
            if clip.get("max_each"):
                for s in parts:
                    if s.get("squeeze") and "max" not in s:
                        s["max"] = round(float(clip["max_each"]) - float(s.get("max_less", 0)), 2)
            for s in parts:
                s.pop("max_less", None)
            segs += parts
            used.append(fname)
            whys.append((f"{fname}: " if len(set(names)) > 1 else "") + found["why"])
            if not clip.get("each"):
                break
        report[name] = {"film": ", ".join(names), "moment": clip["moment"], "segments": segs}
        if segs:
            report[name]["why"] = " | ".join(whys)
            out = {k: v for k, v in clip.items() if k in ("label", "poster_at", "poster", "crf")}
            # The small line under the word: which world(s) and who. Shows the
            # page is not one game with one hero.
            if clip.get("sub") is not False:
                out["sub"] = clip.get("sub") or sub_line(list(dict.fromkeys(n for n in names if n in used)), loaded)
            out["segments"] = segs
            spec["clips"][name] = out
        else:
            report[name]["missing"] = "; ".join(misses)
    spec["clips"] = {n: spec["clips"][n] for n in order if n in spec["clips"]}
    return spec, report


# ── 3/4. cut and review ────────────────────────────────────────────────

def cut(spec: dict, cand: Path) -> dict:
    (cand / "get_clips.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    subprocess.run([sys.executable, str(ROOT / "tools" / "cut_clips.py"), "--spec", str(cand / "get_clips.json"),
                    "--out", str(cand / "clips"), "--og", str(cand / "og.jpg")], cwd=str(ROOT), check=True)
    return json.loads((cand / "clips" / "clips.json").read_text(encoding="utf-8"))


def keep_published(shoot_spec: dict, cand: Path, report: dict) -> None:
    """A clip whose moment did not happen this time keeps the site's."""
    man_path = cand / "clips" / "clips.json"
    man = json.loads(man_path.read_text(encoding="utf-8"))
    have = {c["name"]: c for c in man["clips"]}
    site = {}
    if (SITE / "clips.json").is_file():
        site = {c["name"]: c for c in json.loads((SITE / "clips.json").read_text(encoding="utf-8")).get("clips", [])}
    for name in shoot_spec["clips"]:
        if name not in have and name in site:
            for f in SITE.glob(f"{name}*"):
                if f.suffix in (".mp4", ".jpg"):
                    shutil.copy2(f, cand / "clips" / f.name)
            have[name] = site[name]
            report[name]["kept"] = True
    man["clips"] = [have[n] for n in shoot_spec["clips"] if n in have]
    man_path.write_text(json.dumps(man, indent=2) + "\n", encoding="utf-8")


def strips(cand: Path, names: list) -> None:
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        from cut_clips import ffmpeg, duration
        ff = ffmpeg()
    except Exception:
        return
    for name in names:
        mp4 = cand / "clips" / f"{name}-720.mp4"
        if not mp4.is_file():
            continue
        d = max(0.5, duration(ff, mp4))
        subprocess.run([ff, "-v", "error", "-y", "-i", str(mp4), "-vf",
                        f"fps={10 / d:.4f},scale=240:-1,tile=10x1", "-frames:v", "1",
                        str(cand / f"{name}-strip.jpg")], capture_output=True)


def review(cand: Path, shoot_spec: dict, report: dict, manifest: dict, films: dict) -> Path:
    import html
    rows = []
    for c in manifest["clips"]:
        name = c["name"]
        r = report.get(name, {})
        new = f"clips/{name}.mp4"
        old = SITE / f"{name}.mp4"
        old_src = Path(__import__("os").path.relpath(old, cand)).as_posix() if old.is_file() else ""
        what = ("KEPT FROM THE SITE — " + r.get("missing", "")) if r.get("kept") else r.get("why", "")
        segs = ", ".join(f"{s['from']:.1f}–{s['to']:.1f}s" for s in r.get("segments", []))
        rows.append(f"""
<section>
  <h2>{html.escape(name)} <small>{html.escape(c.get('label') or '(hero)')} · {c.get('seconds', '?')} s · {c.get('bytes', 0) / 1e6:.1f} MB</small></h2>
  <p>{html.escape(what)}<br><span class="dim">{html.escape(r.get('film', ''))} film, {html.escape(r.get('moment', ''))}: {html.escape(segs)}</span></p>
  <div class="pair">
    <figure><video src="{new}" poster="clips/{name}.jpg" controls loop muted autoplay playsinline></video><figcaption>new</figcaption></figure>
    <figure>{f'<video src="{old_src}" controls loop muted playsinline></video>' if old_src else '<div class="none">not on the site yet</div>'}<figcaption>on the site now</figcaption></figure>
  </div>
</section>""")
    missing = [n for n in shoot_spec["clips"] if n not in {c["name"] for c in manifest["clips"]}]
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>/get clips — review</title>
<style>
body {{ margin: 0; padding: 32px; background: #07100d; color: #d8f0e4; font: 15px/1.5 system-ui, sans-serif; }}
h1 {{ font-weight: 300; letter-spacing: .08em; }} h2 {{ margin: 40px 0 6px; font-weight: 500; }}
small, .dim {{ color: #7d9589; font-weight: 400; }} code {{ background: #122; padding: 2px 6px; border-radius: 4px; }}
.pair {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
video, .none {{ width: 100%; aspect-ratio: 16/9; background: #000; border-radius: 6px; display: grid; place-items: center; color: #567; }}
figure {{ margin: 0; }} figcaption {{ color: #7d9589; font-size: 12px; letter-spacing: .2em; text-transform: uppercase; margin-top: 6px; }}
</style></head><body>
<h1>/get clips — candidate {cand.name}</h1>
<p>Films: {html.escape(', '.join(f'{k}: {v}' for k, v in films.items()))}</p>
<p>Happy? <code>python tools/refresh_get.py --publish</code> puts these on the site. Not happy with one?
Set <code>"pick"</code> on it in <code>tools/get_shoot.json</code> and run <code>python tools/refresh_get.py --no-shoot</code>,
or re-film just that film with <code>--films &lt;name&gt;</code>.</p>
{'<p>Missing (no moment, nothing on the site either): ' + html.escape(', '.join(missing)) + '</p>' if missing else ''}
{''.join(rows)}
</body></html>"""
    out = cand / "review.html"
    out.write_text(page, encoding="utf-8")
    return out


# ── 5. publish ─────────────────────────────────────────────────────────

def latest_candidate() -> Path | None:
    c = sorted([d for d in WORK.glob("20*") if (d / "clips" / "clips.json").is_file()])
    return c[-1] if c else None


def publish(cand: Path) -> None:
    man = json.loads((cand / "clips" / "clips.json").read_text(encoding="utf-8"))
    names = {c["name"] for c in man["clips"]}
    SITE.mkdir(parents=True, exist_ok=True)
    for f in SITE.iterdir():
        if f.suffix in (".mp4", ".jpg") and f.stem.replace("-720", "") not in names:
            f.unlink()
    for f in (cand / "clips").iterdir():
        if f.suffix in (".mp4", ".jpg", ".json"):
            shutil.copy2(f, SITE / f.name)
    if (cand / "og.jpg").is_file():
        shutil.copy2(cand / "og.jpg", OG)
    spec = json.loads((cand / "get_clips.json").read_text(encoding="utf-8"))
    PUBLISHED_SPEC.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    say(f"published {cand.name}: {', '.join(c['name'] for c in man['clips'])} "
        f"({sum(c.get('bytes', 0) for c in man['clips']) / 1e6:.1f} MB at 1080p)")
    say("check the page: python tools/judge_get.py   then commit static/video/get, "
        "static/img/get/og.jpg and tools/get_clips.json")
    sweep(spec)


def sweep(published: dict) -> None:
    """Delete get-* films that neither the site nor the latest films use."""
    keep = set()
    for src in published.get("sources", {}).values():
        keep.add((ROOT / src).parent.resolve())
    if FILMS_INDEX.is_file():
        for p in json.loads(FILMS_INDEX.read_text(encoding="utf-8")).values():
            keep.add((ROOT / p).resolve())
    for d in FILM_ROOT.glob("get-*"):
        if d.is_dir() and d.resolve() not in keep:
            shutil.rmtree(d, ignore_errors=True)
            say(f"  swept old film {d.name}")


# ── main ───────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--publish", nargs="?", const="latest", default=None,
                    help="publish the latest candidate (or the named one)")
    ap.add_argument("--no-shoot", action="store_true", help="reuse the last films")
    ap.add_argument("--films", default="", help="comma list of films to re-shoot (default: all)")
    ap.add_argument("--parallel", action="store_true", help="shoot two films at a time")
    ap.add_argument("--jobs", type=int, default=0, help="films at a time (default 1; --parallel = 2)")
    ap.add_argument("--use", default="", help='films to cut from, e.g. run=_claude_film/X,key=_claude_film/Y')
    ap.add_argument("--no-open", action="store_true", help="don't open the review page")
    args = ap.parse_args(argv)
    WORK.mkdir(parents=True, exist_ok=True)

    if args.publish:
        cand = latest_candidate() if args.publish == "latest" else WORK / args.publish
        if not cand or not (cand / "clips" / "clips.json").is_file():
            sys.exit("no candidate to publish; run tools/refresh_get.py first")
        publish(cand)
        return 0

    shoot_spec = json.loads(SHOOT.read_text(encoding="utf-8"))
    films = json.loads(FILMS_INDEX.read_text(encoding="utf-8")) if FILMS_INDEX.is_file() else {}
    for pair in filter(None, args.use.split(",")):
        k, _, v = pair.partition("=")
        films[k.strip()] = rel(ROOT / v.strip())
    if not args.no_shoot:
        wanted = [n.strip() for n in args.films.split(",") if n.strip()] or list(shoot_spec["films"])
        unknown = [n for n in wanted if n not in shoot_spec["films"]]
        if unknown:
            sys.exit(f"no such film in get_shoot.json: {', '.join(unknown)}")
        # "hide_characters" applies to every film.
        hide = shoot_spec.get("hide_characters") or []
        opts = {n: dict(o, hide=list(o.get("hide") or []) + hide) for n, o in shoot_spec["films"].items()}
        films.update(shoot(wanted, opts, args.jobs or (2 if args.parallel else 1)))
    FILMS_INDEX.write_text(json.dumps(films, indent=2) + "\n", encoding="utf-8")
    need = {(f["film"] if isinstance(f, dict) else f) for c in shoot_spec["clips"].values()
            for f in (c["film"] if isinstance(c["film"], list) else [c["film"]])}
    lacking = sorted(need - set(films))
    if lacking:
        say(f"no film yet for: {', '.join(lacking)} (those clips keep the site's)")

    spec, report = build_spec(shoot_spec, films)
    for name in shoot_spec["clips"]:
        r = report.get(name, {"missing": "no film"})
        say(f"  {name:<7} " + (r.get("why") or "MISSING: " + r.get("missing", "")))
    cand = WORK / time.strftime("%Y%m%d-%H%M%S")
    cand.mkdir(parents=True)
    if spec["clips"]:
        cut(spec, cand)
    else:
        (cand / "clips").mkdir()
        (cand / "clips" / "clips.json").write_text('{"clips": []}\n', encoding="utf-8")
        (cand / "get_clips.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    for name in shoot_spec["clips"]:
        report.setdefault(name, {"missing": "no film"})
    keep_published(shoot_spec, cand, report)
    manifest = json.loads((cand / "clips" / "clips.json").read_text(encoding="utf-8"))
    (cand / "moments.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    strips(cand, [c["name"] for c in manifest["clips"]])
    page = review(cand, shoot_spec, report, manifest, films)
    say(f"candidate: {rel(cand)}")
    say(f"review:    {rel(page)}")
    say("publish:   python tools/refresh_get.py --publish")
    if not args.no_open:
        try:
            webbrowser.open(page.resolve().as_uri())
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
