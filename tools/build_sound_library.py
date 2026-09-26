"""Make the shipped sound library, once, and check it.

    python tools/build_sound_library.py --env ../5th_Corner_Dev/.env \
        --stock-dir ../5th_Corner_Dev/assets/music/stock           # generate what is missing
    python tools/build_sound_library.py --dry-run                  # what would be made, and its cost
    python tools/build_sound_library.py --force steps_metal,csq_fall   # a new take of these
    python tools/build_sound_library.py --reprocess                # re-run processing on the raw takes
    python tools/build_sound_library.py --check [--gemini]         # measure every file; ask Gemini what it hears
    python tools/build_sound_library.py --check --quiz --model gemini-3.8-flash   # can it tell them apart

Why this exists: the game used to generate every sound under the picture as it
played, on ElevenLabs — a second account, a second bill, 38% of a run's cost.
When the game moved to ONE key (2026-09-25; docs/plans/ONE_KEY_AUDIO_PLAN.md)
nothing on that key could make sound effects, and Matt decided: *"precache them
FROM my 11 labs and ship precached whatever you need."* So this is the one
place ElevenLabs is still called: a dev tool, run by us, on our account. The
game only reads what it writes (sound_library.py); test_sound_library holds
that no runtime module knows ElevenLabs exists.

The source is tools/sound_library_spec.json. For each entry:

  1. a RAW take — from ElevenLabs (sound-generation for ambience, foley,
     consequence and stingers; the music endpoint, force_instrumental, for
     music) or copied from an earlier stock clip (``source: stock:<file>``, the
     19 Matt had already paid for). Raw takes are kept in
     .cache/sound_library_raw/ so reprocessing never pays twice.
  2. PROCESSED — decoded to float, one-shots trimmed of the silence the
     generator pads them with, loops trimmed of a fade at either end (a fade
     at the loop point is a dip every 25 seconds), loudness-normalised to the
     lane's target with a -1 dBFS peak ceiling, and encoded mp3: mono for
     sound, stereo for music.
  3. CATALOGUED — static/audio/library/catalog.json gets the entry with its
     real duration and level, the whole prompt that made it, and its tags.

Idempotent: an entry whose file exists and whose prompt has not changed is
left alone. A changed prompt is reported as stale and regenerated only with
--stale. Every generation is priced before it is made (old pricing.json:
sound $0.12/min of output, music $0.15/min) and the run stops at --budget.
The key is read from the environment or the --env file, never printed and
never written anywhere.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "tools" / "sound_library_spec.json"
LIB = ROOT / "static" / "audio" / "library"
CATALOG = LIB / "catalog.json"
RAW = ROOT / ".cache" / "sound_library_raw"
CHECK_OUT = ROOT / "_claude_pull" / "sound_library_check.json"

SR = 44100
ELEVEN_SFX_URL = "https://api.elevenlabs.io/v1/sound-generation"
ELEVEN_MUSIC_URL = "https://api.elevenlabs.io/v1/music"
ELEVEN_SUB_URL = "https://api.elevenlabs.io/v1/user/subscription"
SFX_MODEL = "eleven_text_to_sound_v2"
MUSIC_MODEL = "music_v2"
SFX_TEXT_MAX = 450          # the sound endpoint 400s past this rather than truncating
# pricing.json as it stood before ElevenLabs left (git show 424c3cc:pricing.json)
RATE_SFX_PER_S = 0.002      # $0.12 per minute of output
RATE_MUSIC_PER_S = 0.0025   # $0.15 per minute
PEAK_CEIL_DB = -1.0
LIMIT_DB = 6.0         # default for the most gain reduction the limiter may do to reach the
                       # target; lanes and entries set their own (`limit_db`)
# The stock room tone arrived at -59 dBFS RMS (peak -53): quiet by design, and
# still the right sound (Gemini: "a low-frequency electrical hum and subtle
# room tone"). It needs ~35 dB to sit with the rest, so the ceiling allows it.
MAX_BOOST_DB = 36.0
GEMINI_MODEL = "gemini-3.1-flash-lite"

_print_lock = threading.Lock()


def say(*a):
    with _print_lock:
        print(*a, flush=True)


# ─────────────────────────────── keys ──────────────────────────────────────

def read_env_key(name: str, env_file: Path | None) -> str:
    """From the environment, else from a dotenv file. Never printed."""
    val = (os.environ.get(name) or "").strip()
    if val:
        return val
    if env_file and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\s*(?:export\s+)?" + re.escape(name) + r"\s*=\s*(.*)$", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    return ""


# ─────────────────────────────── ffmpeg ────────────────────────────────────

def ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        exe = shutil.which("ffmpeg")
        if not exe:
            raise SystemExit("ffmpeg not found (pip install imageio-ffmpeg)")
        return exe


def decode(path: Path, channels: int) -> np.ndarray:
    """Float32 samples, shape (n, channels), at SR."""
    p = subprocess.run(
        [ffmpeg(), "-v", "error", "-i", str(path), "-f", "f32le", "-acodec", "pcm_f32le",
         "-ac", str(channels), "-ar", str(SR), "-"],
        capture_output=True, check=True)
    return np.frombuffer(p.stdout, dtype=np.float32).reshape(-1, channels).copy()


def encode(samples: np.ndarray, out: Path, bitrate: str) -> None:
    channels = samples.shape[1]
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part.mp3")
    subprocess.run(
        [ffmpeg(), "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", str(channels),
         "-i", "-", "-map_metadata", "-1", "-c:a", "libmp3lame", "-b:a", bitrate,
         "-ac", str(channels), str(tmp)],
        input=np.ascontiguousarray(samples, dtype=np.float32).tobytes(), check=True)
    os.replace(tmp, out)


def db(x: float) -> float:
    return 20.0 * np.log10(max(float(x), 1e-9))


def window_rms_db(samples: np.ndarray, win: int = 2205) -> np.ndarray:
    mono = samples.mean(axis=1)
    n = len(mono) // win
    if n == 0:
        return np.array([db(np.sqrt(np.mean(mono ** 2)) if len(mono) else 0.0)])
    frames = mono[: n * win].reshape(n, win)
    return 20.0 * np.log10(np.sqrt(np.mean(frames ** 2, axis=1)) + 1e-9)


def measure(samples: np.ndarray, loop: bool) -> dict:
    """What the catalog and the check report. Levels in dBFS."""
    win = 2205
    w = window_rms_db(samples, win)
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    active = w[w > max(-60.0, db(peak) - 45.0)] if w.size else w
    lin = 10 ** (active / 20.0) if active.size else np.array([0.0])
    out = {
        "seconds": round(len(samples) / SR, 2),
        "rms_db": round(db(np.sqrt(np.mean(lin ** 2))), 1),
        "peak_db": round(db(peak), 1),
        "silent": bool(peak < 10 ** (-70 / 20.0)),
    }
    if loop and w.size > 40:
        # Medians, of the body and of each edge second. A mean of dB read a
        # pulsing bed (a heartbeat under a cello: near-silent windows between
        # beats) as 10 dB down at a perfectly level edge; a mean of power read
        # a cave's room tone as 24 dB down because the body's average is its
        # drips. The median of either is the floor the loop actually sits on.
        body = float(np.median(w[len(w) // 10: -len(w) // 10]))
        head = float(np.median(w[:20]))     # first second
        tail = float(np.median(w[-20:]))    # last second
        out["loop_edge_db"] = round(float(min(head, tail) - body), 1)
    return out


def limit(x: np.ndarray, ceil_db: float = PEAK_CEIL_DB) -> np.ndarray:
    """A plain look-ahead peak limiter: gain comes down just before a peak that
    would pass the ceiling and eases back over ~50 ms. Used for at most
    LIMIT_DB of reduction, so it catches transients rather than squashing."""
    from scipy.ndimage import maximum_filter1d, minimum_filter1d, uniform_filter1d
    ceil = 10 ** (ceil_db / 20.0)
    env = maximum_filter1d(np.max(np.abs(x), axis=1), size=int(0.006 * SR))
    g = np.minimum(1.0, ceil / np.maximum(env, 1e-9))
    g = minimum_filter1d(g, size=int(0.02 * SR))
    g = uniform_filter1d(g, size=int(0.01 * SR))
    y = (x * g[:, None]).astype(np.float32)
    return np.clip(y, -ceil, ceil)


def _smoothed_db(x: np.ndarray, win: int = 2205, span: int = 20) -> np.ndarray:
    """Per-window level, averaged over `span` windows (~1 s) in power."""
    mono = x.mean(axis=1)
    n = len(mono) // win
    if n == 0:
        return np.array([])
    pw = np.mean(mono[: n * win].reshape(n, win) ** 2, axis=1)
    k = np.ones(min(span, n)) / min(span, n)
    return 10.0 * np.log10(np.convolve(pw, k, mode="same") + 1e-12)


def process(raw: Path, lane_cfg: dict, entry: dict) -> tuple[np.ndarray, dict]:
    channels = int(entry.get("channels") or lane_cfg["channels"])
    loop = bool(entry.get("loop", lane_cfg["loop"]))
    target = float(entry.get("target_db", lane_cfg["target_db"]))
    x = decode(raw, channels)
    win = 2205
    w = window_rms_db(x, win)
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    notes = []
    if db(peak) < -30.0:
        # csq_discovery's first take peaked at -48.8 dBFS: "a shimmer blooming
        # out of silence" came back as mostly silence. Say so; retake it.
        notes.append(f"QUIET TAKE (peak {db(peak):.1f} dBFS)")

    if not loop:
        # Generated one-shots arrive padded to the duration asked for; the
        # silence after a two-second slam is dead air before the next sound.
        thresh = max(-55.0, db(peak) - 42.0)
        loud = np.where(w > thresh)[0]
        if loud.size:
            start = max(0, loud[0] * win - int(0.02 * SR))
            end = min(len(x), (loud[-1] + 1) * win + int(0.15 * SR))
            if start > 0 or end < len(x):
                notes.append(f"trim {start / SR:.2f}-{end / SR:.2f}s of {len(x) / SR:.2f}s")
            x = x[start:end]
        fin = min(len(x), int(0.003 * SR))
        fout = min(len(x), int(0.03 * SR))
        if fin:
            x[:fin] *= np.linspace(0.0, 1.0, fin, dtype=np.float32)[:, None]
        if fout:
            x[-fout:] *= np.linspace(1.0, 0.0, fout, dtype=np.float32)[:, None]
    elif w.size > 40:
        # A loop that fades in or out dips at the seam every time round. Cut
        # back to where the body level starts and ends, read on a one-second
        # average (a sparse texture's single 50 ms windows spike through a
        # fade: mus_convo_critical's first cut kept four seconds of it).
        # Never more than 30%.
        sm = _smoothed_db(x, win)
        body = float(np.median(sm))
        ok = np.where(sm >= body - 6.0)[0]
        if ok.size:
            # A loop that is already at body level at both ends (every
            # ElevenLabs `loop: true` take) must come through sample-exact:
            # cutting even the ragged last window off breaks its seam.
            s = 0 if ok[0] == 0 else ok[0] * win
            e = len(x) if ok[-1] == len(sm) - 1 else (ok[-1] + 1) * win
            if (s + (len(x) - e)) <= 0.3 * len(x) and (s > 0 or e < len(x)):
                notes.append(f"loop trim {s / SR:.2f}-{e / SR:.2f}s of {len(x) / SR:.2f}s")
                x = x[s:e]

    m = measure(x, loop)
    if not m["silent"]:
        # To the lane's loudness, letting the limiter take up to LIMIT_DB off
        # the peaks: peak-normalising alone left a ticking-percussion bed 12 dB
        # under every other track (mus_scene_escalating_desert, -32.5 dBFS).
        lim = float(entry.get("limit_db", lane_cfg.get("limit_db", LIMIT_DB)))
        gain_db = min(target - m["rms_db"], PEAK_CEIL_DB - m["peak_db"] + lim, MAX_BOOST_DB)
        x = (x * (10 ** (gain_db / 20.0))).astype(np.float32)
        if float(np.max(np.abs(x))) > 10 ** (PEAK_CEIL_DB / 20.0):
            x = limit(x)
            notes.append(f"gain {gain_db:+.1f} dB, limited")
        else:
            notes.append(f"gain {gain_db:+.1f} dB")
    return x, {"notes": notes}


# ─────────────────────────────── generation ────────────────────────────────

class Budget:
    def __init__(self, cap: float):
        self.cap = cap
        self.spent = 0.0
        self.lock = threading.Lock()

    def reserve(self, dollars: float) -> bool:
        with self.lock:
            if self.spent + dollars > self.cap:
                return False
            self.spent += dollars
            return True

    def refund(self, dollars: float) -> None:
        with self.lock:
            self.spent -= dollars


def full_prompt(entry: dict, lane_cfg: dict) -> str:
    text = str(entry["prompt"]).strip()
    suffix = str(lane_cfg.get("suffix") or "").strip()
    if suffix and suffix.lower() not in text.lower():
        text = f"{text} {suffix}"
    return text


def estimate(entry: dict, lane_cfg: dict) -> float:
    secs = float(entry.get("seconds") or lane_cfg["seconds"])
    return secs * (RATE_MUSIC_PER_S if entry["lane"] == "music" else RATE_SFX_PER_S)


def eleven_generate(entry: dict, lane_cfg: dict, key: str) -> bytes:
    import requests
    secs = float(entry.get("seconds") or lane_cfg["seconds"])
    prompt = full_prompt(entry, lane_cfg)
    headers = {"xi-api-key": key, "Content-Type": "application/json"}
    if entry["lane"] == "music":
        body = {"prompt": prompt[:2000], "music_length_ms": int(secs * 1000),
                "model_id": MUSIC_MODEL, "force_instrumental": True}
        url, timeout = ELEVEN_MUSIC_URL, 240
    else:
        if len(prompt) > SFX_TEXT_MAX:
            raise ValueError(f"{entry['id']}: prompt is {len(prompt)} chars, max {SFX_TEXT_MAX}")
        body = {"text": prompt, "model_id": SFX_MODEL,
                "duration_seconds": max(0.5, min(30.0, secs)),
                "prompt_influence": float(entry.get("prompt_influence",
                                                    lane_cfg.get("prompt_influence", 0.4))),
                "loop": bool(entry.get("loop", lane_cfg["loop"]))}
        url, timeout = ELEVEN_SFX_URL, 120
    last = None
    for attempt in range(3):
        try:
            r = requests.post(url, headers=headers, json=body,
                              params={"output_format": "mp3_44100_192"}, timeout=timeout)
        except Exception as e:          # network: retry
            last = f"{type(e).__name__}: {e}"
            time.sleep(4 * (attempt + 1))
            continue
        if r.status_code == 200 and r.content and len(r.content) > 1000:
            return r.content
        last = f"http {r.status_code}: {(r.text or '')[:200]}"
        if r.status_code in (400, 401, 403, 422):
            break
        time.sleep(6 * (attempt + 1))
    raise RuntimeError(f"{entry['id']}: {last}")


def subscription_credits(key: str) -> int | None:
    try:
        import requests
        r = requests.get(ELEVEN_SUB_URL, headers={"xi-api-key": key}, timeout=30)
        return int(r.json().get("character_count")) if r.ok else None
    except Exception:
        return None


# ─────────────────────────────── the build ─────────────────────────────────

def load_spec() -> dict:
    return json.loads(SPEC.read_text(encoding="utf-8"))


def load_catalog() -> dict:
    try:
        return json.loads(CATALOG.read_text(encoding="utf-8"))
    except Exception:
        return {"entries": []}


def out_rel(entry: dict) -> str:
    return f"{entry['lane']}/{entry['id']}.mp3"


def raw_path(entry: dict) -> Path:
    return RAW / f"{entry['id']}.mp3"


def raw_meta(entry: dict) -> dict:
    try:
        return json.loads(raw_path(entry).with_suffix(".json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def catalog_row(entry: dict, lane_cfg: dict, meas: dict, prompt: str, source: str) -> dict:
    row = {
        "id": entry["id"],
        "lane": entry["lane"],
        "file": out_rel(entry),
        "seconds": meas["seconds"],
        "loop": bool(entry.get("loop", lane_cfg["loop"])),
        "tags": list(entry.get("tags") or []),
    }
    if entry.get("where"):
        row["where"] = list(entry["where"])
    for k in ("mode", "phase", "flavor", "generic", "fallback"):
        if k in entry:
            row[k] = entry[k]
    if entry["lane"] == "music":
        row["instrumental"] = True
    row["prompt"] = prompt
    row["source"] = source
    row["level_db"] = meas["rms_db"]
    if entry.get("gain") is not None:
        row["gain"] = entry["gain"]
    return row


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", type=Path, default=ROOT / ".env")
    ap.add_argument("--stock-dir", type=Path, default=ROOT / "assets" / "music" / "stock")
    ap.add_argument("--budget", type=float, default=25.0)
    ap.add_argument("--only", default="", help="comma list of lanes or ids")
    ap.add_argument("--force", default="", help="comma list of ids to take again")
    ap.add_argument("--stale", action="store_true", help="retake entries whose prompt changed")
    ap.add_argument("--reprocess", action="store_true", help="re-run processing from raw takes")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--check", action="store_true", help="measure every file (and --gemini: describe)")
    ap.add_argument("--gemini", action="store_true")
    ap.add_argument("--quiz", action="store_true",
                    help="forced choice: can Gemini tell each clip from three others in its lane")
    ap.add_argument("--model", default="", help=f"Gemini model for the check (default {GEMINI_MODEL})")
    args = ap.parse_args(argv)

    spec = load_spec()
    lanes = spec["lanes"]
    entries = spec["entries"]
    ids = [e["id"] for e in entries]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate ids in the spec")
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    force = {s.strip() for s in args.force.split(",") if s.strip()}
    if only:
        entries = [e for e in entries if e["id"] in only or e["lane"] in only]

    if args.check:
        return check(spec, args)

    old = {e["id"]: e for e in load_catalog().get("entries", [])}
    todo_gen, todo_proc = [], []
    for e in entries:
        cfg = lanes[e["lane"]]
        prompt = full_prompt(e, cfg) if not str(e.get("source", "")).startswith("stock:") else e["prompt"]
        out = LIB / out_rel(e)
        have_raw = raw_path(e).is_file()
        stale = bool(raw_meta(e).get("prompt")) and raw_meta(e).get("prompt") != prompt
        if e["id"] in force or (stale and args.stale) or not have_raw:
            todo_gen.append(e)
        elif args.reprocess or not out.is_file() or old.get(e["id"], {}).get("prompt") != prompt:
            todo_proc.append(e)
        if stale and not args.stale and e["id"] not in force:
            say(f"stale   {e['id']}: prompt changed since its take (use --stale to retake)")

    paid = [e for e in todo_gen if not str(e.get("source", "")).startswith("stock:")]
    est = sum(estimate(e, lanes[e["lane"]]) for e in paid)
    say(f"{len(entries)} entries: {len(todo_gen)} to take ({len(paid)} paid, est ${est:.2f}), "
        f"{len(todo_proc)} to (re)process, budget ${args.budget:.2f}")
    if args.dry_run:
        for e in todo_gen:
            say(f"  take  {e['id']:<30} {e['lane']:<11} "
                f"{'stock' if e not in paid else '$%.3f' % estimate(e, lanes[e['lane']])}")
        return 0

    key = ""
    credits_before = None
    if paid:
        key = read_env_key("ELEVENLABS_API_KEY", args.env)
        if not key:
            raise SystemExit(f"ELEVENLABS_API_KEY is not in the environment or {args.env}")
        credits_before = subscription_credits(key)

    RAW.mkdir(parents=True, exist_ok=True)
    budget = Budget(args.budget)
    failed = []

    def take(e: dict) -> None:
        cfg = lanes[e["lane"]]
        src = str(e.get("source") or "")
        rp = raw_path(e)
        if src.startswith("stock:"):
            f = args.stock_dir / src.split(":", 1)[1]
            if not f.is_file():
                failed.append(e["id"])
                say(f"MISSING {e['id']}: stock file {f} not found")
                return
            shutil.copyfile(f, rp)
            rp.with_suffix(".json").write_text(json.dumps(
                {"prompt": e["prompt"], "source": "stock", "from": f.name}, indent=1), encoding="utf-8")
            say(f"stock   {e['id']:<30} <- {f.name}")
            return
        cost = estimate(e, cfg)
        if not budget.reserve(cost):
            failed.append(e["id"])
            say(f"BUDGET  {e['id']}: would pass ${args.budget:.2f}; not generated")
            return
        t0 = time.time()
        try:
            data = eleven_generate(e, cfg, key)
        except Exception as ex:
            budget.refund(cost)
            failed.append(e["id"])
            say(f"FAILED  {e['id']}: {ex}")
            return
        rp.write_bytes(data)
        rp.with_suffix(".json").write_text(json.dumps({
            "prompt": full_prompt(e, cfg), "source": "elevenlabs",
            "model": MUSIC_MODEL if e["lane"] == "music" else SFX_MODEL,
            "seconds": float(e.get("seconds") or cfg["seconds"]),
            "made": _dt.datetime.now().isoformat(timespec="seconds"),
            "bytes": len(data)}, indent=1), encoding="utf-8")
        say(f"made    {e['id']:<30} {len(data) // 1024:>5} KB in {time.time() - t0:4.1f}s   "
            f"~${cost:.3f}  running ~${budget.spent:.2f}")

    music = [e for e in todo_gen if e["lane"] == "music"]
    sounds = [e for e in todo_gen if e["lane"] != "music"]
    with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        list(pool.map(take, sounds))
    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(take, music))

    # Process everything that has a raw take and needs it.
    need = {e["id"] for e in todo_gen + todo_proc} - set(failed)
    catalog = load_catalog()
    rows = {r["id"]: r for r in catalog.get("entries", [])}
    for e in spec["entries"]:
        if e["id"] not in need:
            continue
        cfg = lanes[e["lane"]]
        rp = raw_path(e)
        if not rp.is_file():
            continue
        x, info = process(rp, cfg, e)
        out = LIB / out_rel(e)
        encode(x, out, str(e.get("bitrate") or cfg["bitrate"]))
        meas = measure(decode(out, x.shape[1]), bool(e.get("loop", cfg["loop"])))
        src = "stock" if str(e.get("source", "")).startswith("stock:") else "elevenlabs"
        prompt = e["prompt"] if src == "stock" else full_prompt(e, cfg)
        rows[e["id"]] = catalog_row(e, cfg, meas, prompt, src)
        say(f"wrote   {out.relative_to(ROOT)}  {meas['seconds']:5.2f}s  "
            f"{meas['rms_db']:6.1f} dB  {out.stat().st_size // 1024:>4} KB  {'; '.join(info['notes'])}")

    # Tags, places and modes are the spec's to change without a new take: an
    # entry that was not reprocessed keeps its measured seconds, level and
    # prompt, and takes everything else from the spec as it is now.
    for e in spec["entries"]:
        old_row = rows.get(e["id"])
        if not old_row or e["id"] in need:
            continue
        cfg = lanes[e["lane"]]
        rows[e["id"]] = catalog_row(
            e, cfg, {"seconds": old_row.get("seconds"), "rms_db": old_row.get("level_db")},
            old_row.get("prompt") or e["prompt"], old_row.get("source") or "elevenlabs")

    # The catalog follows the spec's order, and names nothing that is not on disk.
    order = [e["id"] for e in spec["entries"]]
    final = [rows[i] for i in order if i in rows and (LIB / rows[i]["file"]).is_file()]
    total_bytes = sum((LIB / r["file"]).stat().st_size for r in final)
    CATALOG.write_text(json.dumps({
        "version": 1,
        "about": ("The shipped sound library. Made once on 5th Corner's ElevenLabs account by "
                  "tools/build_sound_library.py from tools/sound_library_spec.json; the game "
                  "picks from it with sound_library.pick and never generates sound."),
        "made": _dt.date.today().isoformat(),
        "counts": {lane: sum(1 for r in final if r["lane"] == lane) for lane in lanes},
        "bytes": total_bytes,
        "entries": final,
    }, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    extra = sorted(p.relative_to(LIB).as_posix() for p in LIB.rglob("*.mp3")
                   if p.relative_to(LIB).as_posix() not in {r["file"] for r in final})
    for p in extra:
        say(f"note    {p} is on disk but not in the catalog")
    credits_after = subscription_credits(key) if key else None
    say(f"\ncatalog: {len(final)} entries, {total_bytes / 1e6:.1f} MB   "
        f"estimated spend this run ${budget.spent:.2f}")
    if credits_before is not None and credits_after is not None:
        say(f"ElevenLabs credits used this run: {credits_after - credits_before:,}")
    if failed:
        say("not made: " + ", ".join(failed))
        return 1
    return 0


# ─────────────────────────────── the check ─────────────────────────────────

_ASK = (
    "You are checking a sound effect for a video game. It was generated from this "
    "description:\n\n{prompt}\n\nListen and answer in JSON with keys: "
    "\"heard\" (what you actually hear, under 20 words), "
    "\"speech_or_singing\" (true if any human voice speaks words or sings), "
    "\"music\" (true if there is musical melody, harmony or rhythm), "
    "\"matches\" (true if what you hear is recognisably the described sound), "
    "\"why\" (under 15 words)."
)


# The quiz. The primed question above came back "matches" 171 of 171: a
# listener handed the answer hears it. Asking for a free description with no
# answer given was worse — the same clip before and after its mp3 re-encode
# (waveform correlation 0.98) came back "a heavy metal door latching shut" and
# "a bright sparkling synth arpeggio". So the check that can catch a "gunshot"
# that is really a door is forced choice: the clip's own label among three
# from other sounds in its lane, in an order the model cannot learn from.
_ASK_QUIZ = (
    "This is a short sound from a video game. Which ONE of these descriptions "
    "fits it best?\n\n{options}\n\nAnswer in JSON: {{\"choice\": \"A\"|\"B\"|\"C\"|\"D\"}}."
)


def _label(entry: dict, lanes: dict) -> str:
    """The words that made it, without the lane's boilerplate suffix."""
    text = str(entry.get("prompt") or "")
    suffix = str((lanes.get(entry["lane"]) or {}).get("suffix") or "")
    if suffix and text.endswith(suffix):
        text = text[: -len(suffix)]
    return " ".join(text.split())[:160]


def quiz_options(entry: dict, pool: list[dict], lanes: dict, salt: str = "") -> tuple[list[str], int]:
    """Its label and three distractors from other families in its lane,
    shuffled by a hash of its id (and a salt, so a second round asks a
    different question); returns (labels, index of the right one)."""
    import hashlib
    fam = entry["id"].split("_")[0]
    others = [e for e in pool if e["id"] != entry["id"] and e["id"].split("_")[0] != fam]
    others.sort(key=lambda e: hashlib.sha1((salt + entry["id"] + e["id"]).encode()).hexdigest())
    picks = [entry] + others[:3]
    order = sorted(range(len(picks)),
                   key=lambda i: hashlib.sha1(f"{salt}{entry['id']}|{i}".encode()).hexdigest())
    labels = [_label(picks[i], lanes) for i in order]
    return labels, order.index(0)


def _padded(path: Path, seconds: float) -> Path:
    """A sub-second hit, with half a second of silence either side, for the
    listener only: the first quiz missed most clips under a second."""
    if seconds >= 1.5:
        return path
    out = RAW.parent / "sound_library_quiz" / path.name
    out.parent.mkdir(parents=True, exist_ok=True)
    x = decode(path, 1)
    pad = np.zeros((int(0.5 * SR), 1), dtype=np.float32)
    encode(np.concatenate([pad, x, pad]), out, "128k")
    return out


def gemini_describe(path: Path, prompt: str, key: str, options: list[str] | None = None) -> dict:
    import requests
    if options:
        text = _ASK_QUIZ.format(options="\n".join(f"{'ABCD'[i]}) {o}" for i, o in enumerate(options)))
    else:
        text = _ASK.format(prompt=prompt)
    body = {
        "contents": [{"role": "user", "parts": [
            {"inlineData": {"mimeType": "audio/mpeg",
                            "data": base64.b64encode(path.read_bytes()).decode("ascii")}},
            {"text": text},
        ]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    for attempt in range(3):
        try:
            r = requests.post(url, headers={"x-goog-api-key": key}, json=body, timeout=90)
            if r.ok:
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
            last = f"http {r.status_code}: {r.text[:160]}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(3 * (attempt + 1))
    return {"error": last}


def check(spec: dict, args) -> int:
    """Duration, silence, loop seams on every file; Gemini's ear on request."""
    lanes = spec["lanes"]
    cat = load_catalog()
    rows_all = cat.get("entries", [])
    rows = rows_all
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    if only:
        rows = [r for r in rows if r["id"] in only or r["lane"] in only]
    if args.model:
        global GEMINI_MODEL
        GEMINI_MODEL = args.model
    args.gemini = args.gemini or args.quiz
    gkey = read_env_key("GEMINI_API_KEY", args.env) if args.gemini else ""
    if args.gemini and not gkey:
        raise SystemExit("GEMINI_API_KEY not found")
    report, flags = [], []

    def one(r: dict) -> dict:
        cfg = lanes[r["lane"]]
        f = LIB / r["file"]
        rec = {"id": r["id"], "lane": r["lane"]}
        if not f.is_file():
            rec["problems"] = ["missing file"]
            return rec
        m = measure(decode(f, int(cfg["channels"])), bool(r.get("loop")))
        rec.update(m)
        probs = []
        want = float(next((e.get("seconds") for e in spec["entries"] if e["id"] == r["id"]), 0)
                     or cfg["seconds"])
        if m["silent"] or m["rms_db"] < -45:
            probs.append(f"too quiet ({m['rms_db']} dB)")
        if r.get("loop") and m["seconds"] < 0.6 * want:
            probs.append(f"loop only {m['seconds']}s of {want}s")
        if not r.get("loop") and m["seconds"] < 0.15:
            probs.append(f"one-shot only {m['seconds']}s")
        if r["lane"] == "consequence" and m["seconds"] < 0.4 * want:
            probs.append(f"bed only {m['seconds']}s of {want}s")
        if r.get("loop") and m.get("loop_edge_db", 0) < -9:
            probs.append(f"loop edge {m['loop_edge_db']} dB below its body")
        if gkey and args.quiz:
            # Two rounds with different distractors; a clip is flagged only
            # when it loses both, since the listener is not perfect either.
            heard_as, rounds = [], []
            listen = _padded(f, m["seconds"])
            for salt in ("", "round2"):
                labels, right = quiz_options(r, [e for e in rows_all if e["lane"] == r["lane"]],
                                             lanes, salt)
                g = gemini_describe(listen, "", gkey, options=labels)
                if isinstance(g, list):      # the model sometimes wraps its answer in a list
                    g = next((x for x in g if isinstance(x, dict)), {"error": "empty answer"})
                choice = str(g.get("choice") or "").strip().upper()[:1]
                ok = choice == "ABCD"[right]
                rounds.append(ok)
                if not ok:
                    heard_as.append(labels["ABCD".index(choice)] if choice and choice in "ABCD"
                                    else str(g.get("error") or "?"))
            rec["quiz"] = {"right": sum(rounds), "of": len(rounds), "heard_as": heard_as}
            if not any(rounds):
                probs.append("quiz: heard as " + " / ".join(repr(h[:60]) for h in heard_as))
        elif gkey:
            g = gemini_describe(f, r["prompt"], gkey)
            if isinstance(g, list):      # the model sometimes wraps its answer in a list
                g = next((x for x in g if isinstance(x, dict)), {"error": "empty answer"})
            rec["gemini"] = g
            if g.get("error"):
                probs.append("gemini: " + g["error"])
            else:
                if g.get("speech_or_singing"):
                    probs.append("voice heard")
                if r["lane"] != "music" and g.get("music") and r["lane"] != "stinger":
                    probs.append("music heard in a sound effect")
                if not g.get("matches"):
                    probs.append("does not match: " + str(g.get("heard")))
        rec["problems"] = probs
        return rec

    with cf.ThreadPoolExecutor(max_workers=6 if gkey else 4) as pool:
        for rec in pool.map(one, rows):
            report.append(rec)
            tag = "FLAG" if rec.get("problems") else "ok  "
            heard = (rec.get("gemini") or {}).get("heard") or ""
            if rec.get("quiz"):
                heard = f"quiz {rec['quiz']['right']}/{rec['quiz']['of']}"
            say(f"{tag} {rec['id']:<30} {rec.get('seconds', 0):5.2f}s {rec.get('rms_db', 0):6.1f} dB"
                f"{'  edge %+.1f' % rec['loop_edge_db'] if 'loop_edge_db' in rec else ''}"
                f"{'  | ' + heard if heard else ''}"
                f"{'  <- ' + '; '.join(rec['problems']) if rec.get('problems') else ''}")
            if rec.get("problems"):
                flags.append(rec["id"])
    out = CHECK_OUT.with_name(CHECK_OUT.stem + ("_quiz" if args.quiz else "") + ".json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    say(f"\n{len(rows)} checked, {len(flags)} flagged: {', '.join(flags) or '-'}\n"
        f"report: {out.relative_to(ROOT)}")
    return 1 if flags else 0


if __name__ == "__main__":
    sys.exit(main())
