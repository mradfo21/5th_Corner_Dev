#!/usr/bin/env python3
"""Film a real run of the game for the /get page, smoothly.

    python tools/film_run.py --smoke                  # 1 min: menu + character screen, checks the camera
    python tools/film_run.py                          # a whole run: new character, turns, fights, the pack
    python tools/film_run.py --plan scan_move,encounter,choice --turns 6 --create "a line"
    python tools/film_run.py --first-launch gemini    # a friend's first launch: no key, ACCOUNT, paste, play

--first-launch <gemini|openai> boots the server with NO key and an empty key
store of its own (never the player's %APPDATA% one), so the start menu opens
ACCOUNT by itself; the harness (PT_ACCOUNT) picks the provider in the
dropdown, types that key (read from .env into the harness's environment
only; never logged, and blurred in the film) and plays on it.

Everything the game shows is filmed off Chrome's compositor
(Page.startScreencast): each frame Chrome paints arrives with its timestamp,
and a pacing thread writes the newest one into ffmpeg thirty times a second,
so the master is a steady 30 fps in real time, with nothing dropped by a
page recorder that could not keep up (Playwright's record_video is VP8 at a
low bitrate and stutters; that is what made the first clips look cheap).

It plays the way the other harness runs do (_claude_char_run.py): the
authoring sandbox engaged, the roster copied, a server of its own
(run_local.py, real models), installed Chrome headless on a port of its own,
and playtest_app.py doing the playing against it — PT_CREATE to make a
character on camera, PT_PLAN for the verbs, Shift+N encounters. Nothing it
does reaches the player's own roster, Worlds or `default` session.

Writes _claude_film/<stamp>/:
    master.mp4       the whole run, 30 fps, x264 crf 15
    timeline.json    every 250 ms: video time, the body classes, and the
                     harness's own log lines, so moments can be found and
                     the waiting cut (tools/cut_clips.py reads it)
    playtest_out.txt, server.log, log.txt
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

W, H, FPS = 1920, 1080, 30

STATE_JS = r"""() => {
  const b = document.body, q = (s) => document.querySelector(s);
  return {
    cls: b ? b.className : "",
    enc: !!q("#moment-choices .moment-choice"),
    tags: document.querySelectorAll(".scan-tag").length,
    prose: ((q("#prose") || q(".prose") || {}).innerText || "").slice(0, 90),
  };
}"""


def log_to(path: Path):
    def log(msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return log


def wait_up(url: str, secs: float) -> bool:
    end = time.time() + secs
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=3).read()
            return True
        except Exception:
            time.sleep(1)
    return False


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


class Camera:
    """Newest screencast frame in, a steady FPS stream of JPEGs out to ffmpeg."""

    def __init__(self, out: Path):
        self.out = out
        self.latest: bytes | None = None
        self.frames_in = 0
        self.frames_out = 0
        self.t0 = None
        self._stop = threading.Event()
        self.proc = subprocess.Popen(
            [ffmpeg_exe(), "-v", "error", "-y", "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", str(FPS),
             "-i", "-", "-vf", f"scale={W}:{H}:flags=lanczos,format=yuv420p",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "15", "-g", str(FPS * 2),
             "-movflags", "+faststart", str(out)],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        self.thread = threading.Thread(target=self._pace, daemon=True)

    def feed(self, jpeg: bytes) -> None:
        self.latest = jpeg
        self.frames_in += 1
        if self.t0 is None:
            self.t0 = time.monotonic()
            self.thread.start()

    def now(self) -> float:
        return 0.0 if self.t0 is None else time.monotonic() - self.t0

    def _pace(self) -> None:
        n = 0
        while not self._stop.is_set():
            due = self.t0 + n / FPS
            wait = due - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                self.proc.stdin.write(self.latest)
            except Exception:
                return
            n += 1
            self.frames_out = n

    def close(self) -> str:
        self._stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        err = self.proc.stderr.read().decode(errors="replace") if self.proc.stderr else ""
        self.proc.wait(timeout=120)
        return err.strip()


def read_env_keys(path: Path) -> dict:
    """GEMINI_API_KEY / OPENAI_API_KEY from a .env, for the harness to type."""
    import re
    found = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\s*(GEMINI_API_KEY|OPENAI_API_KEY)\s*=\s*['\"]?([^'\"\s#]+)", line)
            if m and m.group(1) not in found:
                found[m.group(1)] = m.group(2)
    except OSError:
        pass
    return found


def isolate(out: Path, experience: str, log, give: bool = False, give_to: str = "",
            kit: str = "", hide: str = "") -> dict:
    import authoring_sandbox
    log(f"authoring sandbox: {authoring_sandbox.engage('film_run')}")
    import experience_store
    if experience:
        (experience_store.EXPERIENCES_DIR / ".active").write_text(experience, encoding="utf-8")
        log(f"experience in the sandbox: {experience}")
    roster = out / "roster"
    if (ROOT / "characters").is_dir():
        shutil.copytree(ROOT / "characters", roster, ignore=shutil.ignore_patterns("_deleted"))
    else:
        roster.mkdir(parents=True)
    os.environ["SOMEWHERE_CHARACTERS_DIR"] = str(roster)
    try:
        import characters
        characters.CHARACTERS_DIR = roster
        # --hide: out of the copy altogether, so not even the list on the
        # character screen shows them (a starter who is someone else's
        # character stays out of the marketing).
        for word in [w.strip().lower() for w in (hide or "").split(",") if w.strip()]:
            for r in characters.list_all():
                if word in (r.get("name") or "").lower() or word == r["id"]:
                    shutil.rmtree(roster / r["id"], ignore_errors=True)
                    log(f"left out of the roster copy: {r['name']}")
        rows = characters.list_all()
        log("roster copy: " + ", ".join(f"{r['name']} ({r['status']})" for r in rows))
        # --give: something in the played character's pack, so WEAR has a thing
        # to put on. Only ever into the copy.
        want = (give_to or "").lower()
        pick = next((r for r in rows if want and (want in (r.get("name") or "").lower() or want == r["id"])), None)
        plate = ROOT / "_claude_chars" / "helmet.png"
        if give and pick and plate.is_file() and not any(i.get("slot") for i in pick.get("inventory") or []):
            it = characters.add_item(pick["id"], {
                "name": "Riot Helmet", "kind": "armor", "tier": "spoil", "plate": str(plate),
                "power": "Survives one killing blow.", "from": "the riot line", "source": "encounter",
                "look": "a black police riot helmet with a clear perspex visor raised, the visor cracked"})
            log(f"in {pick['name']}'s pack (the copy): {it and it['name']}")
        # --kit FILE: a JSON list of items (plates relative to the repo) put
        # into the played character's pack copy, in order — WEAR takes the
        # first thing not yet worn, so the film suits them up in this order.
        if kit and pick:
            for item in json.loads(Path(kit).read_text(encoding="utf-8")):
                item = dict(item)
                if item.get("plate"):
                    item["plate"] = str(ROOT / item["plate"])
                it = characters.add_item(pick["id"], item)
                log(f"kit into {pick['name']}'s pack (the copy): {it and it['name']} ({it and it.get('slot')})")
    except Exception as exc:  # the roster is optional for filming
        log(f"roster copy: {exc}")
    return dict(os.environ)


async def film(args, out: Path, log) -> None:
    from playwright.async_api import async_playwright

    env = isolate(out, args.experience, log, give=args.give, give_to=args.character,
                  kit=args.kit, hide=args.hide)
    env["PYTHONUNBUFFERED"] = "1"
    if not args.realtime:
        # Film the stills game. With REACTOR_API_KEY in .env the client
        # defaults to the live-video layer, and a stream that "connects" and
        # presents black sits above perfectly good frames: the Township 12
        # film (2026-09-25) played a whole opening to a black screen while
        # every still on disk was bright. Empty wins over .env (run_local
        # never overrides a set variable).
        env["REACTOR_API_KEY"] = ""
    harness_keys = {}
    cmd = [sys.executable, "-u", "run_local.py", "--no-browser", "--host", "127.0.0.1", "--port", str(args.port)]
    if args.first_launch:
        # A machine that has never had a key: nothing in the server's
        # environment, a key store of its own. The keys go to the harness only.
        # Outside the repo: keys_store refuses to write keys inside the tree.
        import tempfile
        keys_dir = Path(tempfile.mkdtemp(prefix="somewhere-first-launch-"))
        env["SOMEWHERE_KEYS_PATH"] = str(keys_dir / "keys.env")
        env["SOMEWHERE_ANALYTICS_DIR"] = str(keys_dir / "analytics")
        for name in ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_BASE_URL", "CUSTOM_TEXT_MODEL"):
            env.pop(name, None)
        harness_keys = read_env_keys(ROOT / ".env")
        log(f"first launch on {args.first_launch}: no key in the server; keys for the harness: "
            + ", ".join(k for k in harness_keys) or "none")
    else:
        cmd += ["--config", str(ROOT / ".env")]
    srv_log = open(out / "server.log", "w", encoding="utf-8")
    srv = subprocess.Popen(cmd, cwd=str(ROOT), stdout=srv_log, stderr=subprocess.STDOUT, env=env,
                           stdin=subprocess.DEVNULL)
    harness = None
    cam = None
    timeline = {"fps": FPS, "size": [W, H], "samples": [], "log": [], "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        if not wait_up(f"http://127.0.0.1:{args.port}/api/status", 180):
            log("server never came up")
            return
        await asyncio.sleep(2)
        if ("Backend:      mock" in (out / "server.log").read_text(encoding="utf-8", errors="replace")
                and not args.allow_mock and not args.first_launch):
            log("ABORT: the server came up in MOCK mode (no key), which would film canned text")
            return
        log(f"server up on {args.port}")

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                channel="chrome", headless=True,
                args=[f"--remote-debugging-port={args.cdp}", "--autoplay-policy=no-user-gesture-required",
                      "--force-device-scale-factor=1", f"--window-size={W},{H}",
                      "--disable-background-timer-throttling", "--disable-renderer-backgrounding"])
            ctx = await browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=1)
            page = await ctx.new_page()
            url = f"http://127.0.0.1:{args.port}/standalone?session={args.session}"
            await page.goto(url, wait_until="load")
            await page.wait_for_function("() => !!window.Renderer", timeout=90000)
            log(f"client loaded: {url}")
            if args.first_launch:
                # The film is published: the stored key's last four are blurred.
                await page.add_style_tag(content=".acct-dots { filter: blur(6px); }")

            cam = Camera(out / "master.mp4")
            cdp = await ctx.new_cdp_session(page)

            async def on_frame(ev):
                cam.feed(base64.b64decode(ev["data"]))
                try:
                    await cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})
                except Exception:
                    pass

            cdp.on("Page.screencastFrame", lambda ev: asyncio.ensure_future(on_frame(ev)))

            async def start_cast():
                await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92,
                                                        "maxWidth": W, "maxHeight": H, "everyNthFrame": 1})

            await start_cast()
            page.on("load", lambda _p: asyncio.ensure_future(start_cast()))

            if args.smoke:
                t_end = time.time() + 70
                pressed = False
            else:
                penv = dict(env, PT_CDP=f"http://127.0.0.1:{args.cdp}", PT_PLAN=args.plan,
                            PT_TURNS=str(args.turns), PT_TURN_TIMEOUT="170", PT_START_TIMEOUT="480",
                            PT_CHARACTER=args.character or "", PT_CREATE=args.create or "",
                            PT_CREATE_LINGER=str(args.linger), PYTHONIOENCODING="utf-8")
                if args.act_lines:
                    penv["PT_ACT_LINES"] = args.act_lines
                if "wear" in (args.plan or ""):
                    penv["PT_WEAR_PORTRAIT"] = "1"   # stay in the pack until the new pose is drawn
                if args.first_launch:
                    penv.update(harness_keys)
                    penv.update(PT_ACCOUNT=args.first_launch, PT_ACCOUNT_LINGER=str(args.linger),
                                PT_ACCOUNT_TOUR="1")
                harness = await asyncio.create_subprocess_exec(
                    sys.executable, "-u", "playtest_app.py", cwd=str(ROOT), env=penv,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    stdin=asyncio.subprocess.DEVNULL)

                async def pump():
                    with open(out / "playtest_out.txt", "w", encoding="utf-8") as f:
                        while True:
                            line = await harness.stdout.readline()
                            if not line:
                                break
                            text = line.decode("utf-8", errors="replace").rstrip()
                            f.write(text + "\n"); f.flush()
                            timeline["log"].append([round(cam.now(), 2), text])
                asyncio.ensure_future(pump())
                t_end = time.time() + args.max_minutes * 60

            last_stat = time.time()
            last_in = 0
            while time.time() < t_end:
                if harness is not None and harness.returncode is not None:
                    log(f"harness finished (exit {harness.returncode})")
                    await asyncio.sleep(4)          # the last picture, held
                    break
                if (out / "STOP").exists():
                    log("STOP file: finishing")
                    break
                try:
                    st = await page.evaluate(STATE_JS)
                    timeline["samples"].append([round(cam.now(), 2), st["cls"], st["enc"], st["tags"], st["prose"]])
                except Exception:
                    pass
                if args.smoke and not pressed and cam.now() > 25:
                    try:
                        await page.click("#start-play", timeout=5000)
                        log("smoke: pressed PLAY (the character screen)")
                    except Exception as exc:
                        log(f"smoke: PLAY click failed: {exc}")
                    pressed = True
                if time.time() - last_stat > 30:
                    fps_in = (cam.frames_in - last_in) / (time.time() - last_stat)
                    log(f"camera: {cam.now():6.0f}s filmed, {fps_in:4.1f} painted frames/s coming in")
                    last_stat, last_in = time.time(), cam.frames_in
                await asyncio.sleep(0.25)
            else:
                log("smoke done" if args.smoke else f"time limit ({args.max_minutes} min) reached")

            try:
                await cdp.send("Page.stopScreencast")
            except Exception:
                pass
            if harness is not None and harness.returncode is None:
                harness.terminate()
            await browser.close()
    finally:
        if cam is not None:
            err = cam.close()
            log(f"master.mp4: {cam.now():.0f}s, {cam.frames_in} painted frames in, {cam.frames_out} written"
                + (f" (ffmpeg: {err[:200]})" if err else ""))
        (out / "timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
        try:
            srv.terminate(); srv.wait(timeout=15)
        except Exception:
            srv.kill()
        srv_log.close()
        log("DONE")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--smoke", action="store_true", help="one minute of menu and character screen, no harness")
    ap.add_argument("--experience", default="somewhere", help="experience slug to play (default: the shipped one)")
    ap.add_argument("--plan", default="scan_move,choice,encounter,choice,scan_interact,wear,encounter,act,scan_move,choice")
    ap.add_argument("--turns", type=int, default=10)
    ap.add_argument("--create", default="", help="make a new character on camera from this line")
    ap.add_argument("--character", default="", help="or play an existing one (id or part of the name)")
    ap.add_argument("--give", action="store_true", help="put a riot helmet in the played character's pack (the copy)")
    ap.add_argument("--kit", default="", help="JSON list of items for the played character's pack (the copy), in order")
    ap.add_argument("--hide", default="", help="characters left out of the roster copy (names or ids, comma-separated)")
    ap.add_argument("--linger", type=float, default=4.0, help="seconds on the finished character")
    ap.add_argument("--max-minutes", type=float, default=45)
    ap.add_argument("--port", type=int, default=5021)
    ap.add_argument("--cdp", type=int, default=9341)
    ap.add_argument("--session", default="film")
    ap.add_argument("--allow-mock", action="store_true")
    ap.add_argument("--realtime", action="store_true", help="keep the Reactor live-video layer (default: stills)")
    ap.add_argument("--act-lines", default="", help='typed actions for the "act" turns, "a | b | c"')
    ap.add_argument("--out", default="", help="film into this folder (default _claude_film/<stamp>); emptied first")
    ap.add_argument("--first-launch", choices=["gemini", "openai"], default="",
                    help="no key anywhere; set the provider and key in ACCOUNT on camera, then play")
    args = ap.parse_args(argv)

    if args.out:
        out = Path(args.out).resolve()
        if out.is_dir():
            shutil.rmtree(out)
    else:
        out = ROOT / "_claude_film" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    os.chdir(ROOT)
    log = log_to(out / "log.txt")
    log(f"filming into {out}  ({'smoke' if args.smoke else 'run'}: {vars(args)})")
    asyncio.run(film(args, out, log))
    return 0


if __name__ == "__main__":
    sys.exit(main())
