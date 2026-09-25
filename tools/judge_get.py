#!/usr/bin/env python3
"""Open /get in a clean copy of Chrome, and judge it.

    python tools/judge_get.py                 # desktop + tablet + phone, then leave Chrome open
    python tools/judge_get.py --only phone
    python tools/judge_get.py --no-shots      # just open the clean Chrome

1. Starts tools/serve_get.py if nothing answers on the port.
2. Launches the installed Google Chrome with a THROWAWAY profile
   (%TEMP%/somewhere-get-chrome): no extensions, no sign-ins, no cache, so
   what you see is what a friend opening the link for the first time sees.
   That window stays open on /get for you to judge by eye.
3. Drives the same Chrome over its debugging port (Playwright, already in
   requirements-dev.txt) in a separate window per device size, and writes to
   _claude_get/judge/<stamp>/:
     - <device>-<section>.png   the splash, the run, the build, the footer
     - <device>-full.png        the whole page
     - report.json / REPORT.md  what can be checked without eyes: console
       errors, failed requests, images that did not load, sideways overflow,
       fonts actually used, load timing and bytes, every recording loading and
       actually playing, the word count, DOWNLOAD's redirect, and the toast.

The mechanical checks are the floor, not the verdict — the same rule as the
game's harness: look at the screenshots.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
DEVICES = {
    "desktop": {"viewport": {"width": 1440, "height": 900}, "device_scale_factor": 1,
                "user_agent": None, "is_mobile": False, "has_touch": False},
    "tablet": {"viewport": {"width": 820, "height": 1180}, "device_scale_factor": 2,
               "user_agent": "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
               "is_mobile": True, "has_touch": True},
    "phone": {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 3,
              "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
              "is_mobile": True, "has_touch": True},
}


def listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_http(url: str, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status < 500:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def ensure_server(port: int) -> None:
    if listening(port):
        return
    print(f"starting tools/serve_get.py on {port} ...", flush=True)
    log = open(ROOT / "_claude_get" / "serve.log", "a", encoding="utf-8") if (ROOT / "_claude_get").is_dir() \
        else open(Path(tempfile.gettempdir()) / "serve_get.log", "a", encoding="utf-8")
    subprocess.Popen([sys.executable, str(ROOT / "tools" / "serve_get.py"), "--port", str(port)],
                     cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                     creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    if not wait_http(f"http://127.0.0.1:{port}/api/builds/latest", 90):
        sys.exit("the preview server did not come up; see _claude_get/serve.log")


def launch_chrome(url: str, cdp_port: int, fresh: bool) -> None:
    chrome = next((p for p in CHROME_PATHS if Path(p).is_file()), None)
    if not chrome:
        sys.exit("Google Chrome not found")
    profile = Path(tempfile.gettempdir()) / "somewhere-get-chrome"
    if listening(cdp_port):
        return                                   # the test Chrome is already up
    if fresh and profile.exists():
        shutil.rmtree(profile, ignore_errors=True)
    subprocess.Popen([chrome, f"--user-data-dir={profile}", f"--remote-debugging-port={cdp_port}",
                      "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                      "--window-size=1440,960", "--new-window", url],
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    if not wait_http(f"http://127.0.0.1:{cdp_port}/json/version", 30):
        sys.exit("Chrome started but its debugging port never answered")


PROBE = r"""
() => {
  const vw = innerWidth, doc = document.documentElement;
  const wide = [...document.querySelectorAll('body *')].filter(el => {
    const r = el.getBoundingClientRect();
    if (!r.width || getComputedStyle(el).position === 'fixed') return false;
    if (el.closest('.film')) return false;               // a recording bleeding inside its clipped frame
    return r.right > vw + 1 || r.left < -1;
  }).slice(0, 8).map(el => (el.id ? '#' + el.id : el.tagName.toLowerCase() + '.' + [...el.classList].join('.')));
  const imgs = [...document.images].map(i => ({src: i.currentSrc || i.src, ok: i.complete && i.naturalWidth > 0, alt: i.getAttribute('alt')}));
  const nav = performance.getEntriesByType('navigation')[0] || {};
  const res = performance.getEntriesByType('resource');
  const bytes = res.reduce((a, r) => a + (r.transferSize || r.encodedBodySize || 0), 0) + (nav.transferSize || 0);
  const font = f => document.fonts.check('16px "' + f + '"');
  const rect = e => { if (!e) return null; const r = e.getBoundingClientRect(); return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}; };
  const get = document.querySelector('#hero-get');
  const words = [...document.querySelectorAll('main, footer')].map(e => e.innerText).join(' ').split(/\s+/).filter(Boolean).length;
  return {
    viewport: [vw, innerHeight], scrollWidth: doc.scrollWidth, pageHeight: doc.scrollHeight,
    overflowX: doc.scrollWidth > vw + 1, wideElements: wide,
    brokenImages: imgs.filter(i => !i.ok).map(i => i.src),
    fonts: {bebas: font('Bebas Neue'), instrument: font('Instrument Sans'), mono: font('Share Tech Mono')},
    timing: {domContentLoaded: Math.round(nav.domContentLoadedEventEnd || 0), load: Math.round(nav.loadEventEnd || 0)},
    bytes, requests: res.length + 1, visibleWords: words,
    brand: rect(document.querySelector('.brand')), download: rect(get), downloadText: get ? get.textContent.trim() : null,
    downloadInView: !!(get && get.getBoundingClientRect().bottom <= innerHeight && get.getBoundingClientRect().top >= 0),
    meta: (document.querySelector('#meta') || {}).textContent || '',
    elsewhere: (() => { const e = document.querySelector('#elsewhere'); return e && !e.hidden ? e.textContent.trim() : null; })(),
    clips: document.querySelectorAll('.clip').length, news: document.querySelectorAll('#news li').length,
    title: document.title,
  };
}
"""

VIDEO = r"""
async (sel) => {
  const v = document.querySelector(sel);
  if (!v) return null;
  const t0 = v.currentTime;
  await new Promise(r => setTimeout(r, 1500));
  return {src: (v.currentSrc || v.src || '').split('/').pop(), ready: v.readyState, w: v.videoWidth, h: v.videoHeight,
          paused: v.paused, // a short loop may wrap while we wait: count the wrap as playing
          advanced: +(((v.currentTime - t0) % (v.duration || 1e9) + (v.duration || 1e9)) % (v.duration || 1e9)).toFixed(2), duration: +(v.duration || 0).toFixed(1),
          error: v.error ? v.error.code : null};
}
"""


def judge(browser, name: str, dev: dict, url: str, out: Path) -> dict:
    ctx_args = {k: v for k, v in dev.items() if v is not None}
    ctx = browser.new_context(**ctx_args)
    page = ctx.new_page()
    console, failed = [], []
    page.on("console", lambda m: console.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))
    # A browser cancels and re-asks for byte ranges of a video as it buffers
    # and pauses; that is net::ERR_ABORTED on a media request, not a failure.
    page.on("requestfailed", lambda r: None if (r.resource_type == "media" or r.url.split("?")[0].endswith((".mp4", ".webm")))
            and "ERR_ABORTED" in str(r.failure) else failed.append(f"{r.url} ({r.failure})"))
    page.on("response", lambda r: failed.append(f"{r.url} -> {r.status}") if r.status >= 400 else None)

    t0 = time.time()
    page.goto(url, wait_until="load")
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(2600)                          # the title settle + rises
    first = round(time.time() - t0, 2)
    report = page.evaluate(PROBE)
    report["secondsToSettled"] = first
    page.screenshot(path=str(out / f"{name}-1-splash.png"))

    report["heroVideo"] = page.evaluate(VIDEO, "#hero-video")

    # each clip: scrolled to, it should load, play and move; captured mid-play
    report["clipVideos"] = []
    for i in range(page.locator(".clip").count()):
        clip = page.locator(".clip").nth(i)
        clip.scroll_into_view_if_needed()
        page.evaluate(f"document.querySelectorAll('.clip')[{i}].scrollIntoView({{block: 'center'}})")
        page.wait_for_timeout(2200)
        state = page.evaluate(VIDEO, f".clip:nth-of-type({i + 1}) video")
        report["clipVideos"].append(state)
        page.wait_for_timeout(1500)
        page.screenshot(path=str(out / f"{name}-2-clip{i + 1}.png"))

    for i, (sel, label) in enumerate([("#get", "get"), (".sign", "footer")], 3):
        if page.locator(sel).count() and page.locator(sel).is_visible():
            page.evaluate(f"document.querySelector('{sel}').scrollIntoView({{block: 'start'}})")
            page.wait_for_timeout(900)
            page.screenshot(path=str(out / f"{name}-{i}-{label}.png"))
    report["topBarDownloadShown"] = page.evaluate("document.body.classList.contains('past-splash')")

    # DOWNLOAD: the toast shows, and the link resolves through /get/latest
    page.evaluate("scrollTo(0, 0)")
    page.wait_for_timeout(500)
    report["downloadHref"] = page.get_attribute("#hero-get", "href")
    if report["downloadHref"]:
        page.evaluate("document.querySelector('#hero-get').addEventListener('click', e => e.preventDefault(), {once: true})")
        page.click("#hero-get")
        page.wait_for_timeout(700)
        report["toast"] = page.evaluate("document.querySelector('#toast').classList.contains('on') ? document.querySelector('#toast').innerText : null")
        r = ctx.request.get(url.split("/get")[0] + report["downloadHref"], max_redirects=0)
        report["downloadResolves"] = {"status": r.status, "location": r.headers.get("location")}

    # the whole page, words forced in so nothing is caught mid-fade
    page.evaluate("document.querySelectorAll('.clip').forEach(e => e.classList.add('in'))")
    page.wait_for_timeout(1100)
    page.screenshot(path=str(out / f"{name}-full.png"), full_page=True)

    report["console"] = console
    report["failedRequests"] = failed
    ctx.close()
    return report


def verdicts(name: str, r: dict) -> list[str]:
    bad = []
    if r["console"]: bad.append(f"console: {r['console'][:3]}")
    if r["failedRequests"]: bad.append(f"failed requests: {r['failedRequests'][:3]}")
    if r["brokenImages"]: bad.append(f"broken images: {r['brokenImages'][:3]}")
    if r["overflowX"] or r["wideElements"]: bad.append(f"sideways overflow ({r['scrollWidth']}px wide): {r['wideElements']}")
    if not all(r["fonts"].values()): bad.append(f"fonts not loaded: {r['fonts']}")
    if not r["downloadInView"]: bad.append("DOWNLOAD is not fully on the first screen")
    for v in [r.get("heroVideo")] + list(r.get("clipVideos") or []):
        if v is None:
            bad.append("a video element is missing")
        elif v["error"] or not v["w"] or v["advanced"] <= 0.2:
            bad.append(f"video not playing: {v}")
    if r.get("downloadResolves") and r["downloadResolves"]["status"] not in (301, 302, 303, 307, 308):
        bad.append(f"/get/latest did not redirect: {r['downloadResolves']}")
    if r["secondsToSettled"] > 6: bad.append(f"slow to settle: {r['secondsToSettled']}s")
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=5077)
    ap.add_argument("--cdp-port", type=int, default=9334)
    ap.add_argument("--only", choices=list(DEVICES))
    ap.add_argument("--no-shots", action="store_true", help="only open the clean Chrome")
    ap.add_argument("--keep-profile", action="store_true", help="reuse the last test profile (warm cache)")
    args = ap.parse_args(argv)

    url = f"http://localhost:{args.port}/get"
    ensure_server(args.port)
    launch_chrome(url, args.cdp_port, fresh=not args.keep_profile)
    print(f"clean Chrome is open on {url}", flush=True)
    if args.no_shots:
        return 0

    from playwright.sync_api import sync_playwright
    out = ROOT / "_claude_get" / "judge" / _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    reports = {}
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{args.cdp_port}")
        for name, dev in DEVICES.items():
            if args.only and name != args.only:
                continue
            print(f"judging {name} ...", flush=True)
            try:
                reports[name] = judge(browser, name, dev, url, out)
            except Exception as exc:  # keep going; one size failing shouldn't hide the others
                reports[name] = {"error": repr(exc)}
        # leave the person's window where it was; disconnecting does not close Chrome
    (out / "report.json").write_text(json.dumps(reports, indent=2), encoding="utf-8")

    lines = [f"# /get judged {_dt.datetime.now():%Y-%m-%d %H:%M}", "", f"`{url}` in a clean Chrome profile.", ""]
    for name, r in reports.items():
        lines.append(f"## {name}")
        if "error" in r:
            lines += [f"- ERROR: {r['error']}", ""]
            continue
        bad = verdicts(name, r)
        lines.append("- **nothing mechanical wrong**" if not bad else "- **problems:**")
        lines += [f"  - {b}" for b in bad]
        lines += [
            f"- settled in {r['secondsToSettled']}s · load {r['timing']['load']} ms · {r['requests']} requests · {r['bytes'] / 1e6:.2f} MB",
            f"- viewport {r['viewport']} · page {r['pageHeight']}px tall · {r['clips']} clips · {r['visibleWords']} words on the page · what's new {r['news']} lines",
            f"- videos: hero {r.get('heroVideo')} · clips {[ (v or {}).get('src') + ' +' + str((v or {}).get('advanced')) + 's' for v in r.get('clipVideos') or []]}",
            f"- first screen: DOWNLOAD {'in view' if r['downloadInView'] else 'NOT in view'} at {r['download']} · title {r['brand']}",
            f"- meta: {r['meta']!r}",
            f"- non-Windows note: {r['elsewhere']!r}",
            f"- toast: {r.get('toast')!r} · /get/latest: {r.get('downloadResolves')}",
            "",
        ]
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\nscreenshots and report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
