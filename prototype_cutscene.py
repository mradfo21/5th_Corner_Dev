"""
CLI prototype for the 4-shot cutscene montage.

Takes a source plate (a session still, encounter image, or any PNG) and
writes four cinematic shots plus a self-contained HTML preview you can
open in a browser.

  python prototype_cutscene.py path/to/plate.png
  python prototype_cutscene.py --offline sessions/default/images/some.png
  python prototype_cutscene.py --mood aftermath --gemini plate.png

Default is optical (no network) so the montage UX is testable immediately.
Pass --gemini to spend one img2img call on a 2×2 restage.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cutscene

ROOT = Path(__file__).parent.resolve()
OUT_DIR = ROOT / "sessions" / "_cutscene_proto"


def _html(shots: list, name: str, mood: str, source: str) -> str:
    urls = [Path(s["path"]).name for s in shots]
    labels = [s.get("label") or s.get("camera") or f"Shot {i+1}"
              for i, s in enumerate(shots)]
    tiles = "".join(
        f'<figure><img src="{u}" alt="{lab}"><figcaption>{lab}</figcaption></figure>'
        for u, lab in zip(urls, labels)
    )
    js_urls = json.dumps(urls)
    js_labels = json.dumps(labels)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{name} — cutscene preview</title>
<style>
  html, body {{ margin: 0; background: #050505; color: #e8dcc4; font: 14px/1.4 Georgia, serif; }}
  .stage {{ position: relative; width: min(960px, 100vw); height: min(72vh, 640px);
            margin: 4vh auto 0; overflow: hidden; background: #111; }}
  .stage img {{ position: absolute; inset: 0; width: 100%; height: 100%;
                object-fit: cover; opacity: 0; transition: opacity .45s ease;
                transform: scale(1); }}
  .stage img.on {{ opacity: 1; animation: push 1.6s ease-out forwards; }}
  @keyframes push {{ to {{ transform: scale(1.06); }} }}
  .letter {{ position: absolute; left: 0; right: 0; height: 9%; background: #000; z-index: 2; }}
  .letter.top {{ top: 0; }} .letter.bot {{ bottom: 0; }}
  .name {{ text-align: center; letter-spacing: .28em; text-transform: uppercase;
           margin-top: 1.2rem; font-size: 12px; opacity: .7; }}
  .sub {{ text-align: center; opacity: .5; font-size: 12px; min-height: 1.4em; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px;
           width: min(960px, 94vw); margin: 2rem auto; }}
  .grid figure {{ margin: 0; }}
  .grid img {{ width: 100%; display: block; }}
  .grid figcaption {{ font-size: 11px; letter-spacing: .14em; text-transform: uppercase;
                      opacity: .55; padding: .4rem 0; }}
  .hint {{ text-align: center; opacity: .4; font-size: 12px; margin-bottom: 2rem; }}
</style>
</head>
<body>
  <div class="name">{name} · {mood} · {source}</div>
  <div class="sub" id="sub">playing</div>
  <div class="stage" id="stage">
    <div class="letter top"></div>
    <div class="letter bot"></div>
  </div>
  <p class="hint">Click or press space to advance. Esc skips to the end.</p>
  <div class="grid">{tiles}</div>
<script>
const urls = {js_urls};
const labels = {js_labels};
const stage = document.getElementById("stage");
const sub = document.getElementById("sub");
const imgs = urls.map((u) => {{
  const im = new Image();
  im.src = u;
  stage.appendChild(im);
  return im;
}});
let i = 0, t = 0;
function show(n) {{
  i = n;
  imgs.forEach((im, k) => im.classList.toggle("on", k === n));
  sub.textContent = labels[n] || "";
}}
function next() {{
  if (i + 1 >= imgs.length) {{ show(imgs.length - 1); sub.textContent = "hold"; return; }}
  show(i + 1);
  arm();
}}
function arm() {{
  clearTimeout(t);
  t = setTimeout(next, 1600);
}}
document.addEventListener("keydown", (e) => {{
  if (e.key === " " || e.key === "Enter" || e.key === "ArrowRight") {{ e.preventDefault(); next(); }}
  if (e.key === "Escape") {{ show(imgs.length - 1); clearTimeout(t); }}
}});
stage.addEventListener("click", next);
show(0); arm();
</script>
</body>
</html>
"""


def main() -> int:
    p = argparse.ArgumentParser(description="Prototype a 4-shot cutscene from a plate.")
    p.add_argument("image", nargs="?", help="Source plate (PNG/JPG).")
    p.add_argument("--mood", default="threshold", choices=list(cutscene.MOOD_IDS))
    p.add_argument("--name", default="Cutscene")
    p.add_argument("--offline", action="store_true", default=True,
                   help="Optical crops only (default).")
    p.add_argument("--gemini", action="store_true",
                   help="Spend one Gemini img2img call on a 2×2 restage.")
    p.add_argument("--out", default=str(OUT_DIR), help="Output directory.")
    args = p.parse_args()

    src = Path(args.image) if args.image else None
    if src is None:
        # Prefer a recent session still so the prototype has something to show.
        images = sorted(
            (ROOT / "sessions" / "default" / "images").glob("*.png"),
            key=lambda x: x.stat().st_mtime, reverse=True,
        )
        images = [i for i in images if "small" not in i.name and "flipbook" not in i.name]
        if not images:
            print("No source image. Pass a PNG path.")
            return 2
        src = images[0]
        print(f"[proto] using latest session still: {src}")
    if not src.exists():
        print(f"Not found: {src}")
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    offline = not args.gemini
    result = cutscene.generate_shots(
        src,
        session_id="default",
        mood=args.mood,
        name=args.name,
        offline=offline,
        output_dir=out,
    )
    html_path = out / "preview.html"
    html_path.write_text(
        _html(result["shots"], args.name, result["mood"], result["source"]),
        encoding="utf-8",
    )
    print(f"[proto] source={result['source']} mood={result['mood']} "
          f"shots={len(result['shots'])} ({result.get('elapsed_ms')}ms)")
    for s in result["shots"]:
        print(f"  {s['camera']:10} {s['path']}")
    print(f"[proto] preview: {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
