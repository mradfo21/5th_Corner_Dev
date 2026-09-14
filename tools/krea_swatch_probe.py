"""Does handing Krea a blurred style swatch make Krea draw a blurred image?

The hard-cut path builds a `make_style_swatch` — a frame downsampled to a
handful of cells and Gaussian blurred until no geometry survives — and passes
it as the sole reference. For Gemini that is loose palette conditioning. Krea
uploads it into `image_style_references`, which means "adopt this look".

Renders the same prompt three ways and writes them side by side.

    python -B tools/krea_swatch_probe.py --source <a_real_frame.png>
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for line in Path(".env").read_text(encoding="utf-8", errors="replace").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

OUT = Path("logs/krea_swatch")
PROMPT = (
    "A man in an olive field jacket stands at the mouth of a scorched basin, "
    "cracked earth underfoot, rusted industrial wreckage on the horizon, "
    "late afternoon light raking across the ground."
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="an existing rendered frame")
    ap.add_argument("--aspect", default="2.35:1", help="a ratio Krea accepts")
    args = ap.parse_args()

    import krea_image_utils as k
    k._krea_aspect = lambda requested=None: args.aspect
    print(f"forcing a Krea-supported aspect ratio: {args.aspect}\n")

    OUT.mkdir(parents=True, exist_ok=True)
    from gemini_image_utils import make_style_swatch
    from krea_image_utils import generate_with_krea, generate_krea_img2img

    swatch = make_style_swatch(args.source, output_dir=OUT)
    print(f"swatch: {swatch}")
    print()

    print("A. text-to-image, no reference (what a hard cut SHOULD look like)")
    a = generate_with_krea(prompt=PROMPT, caption="a_no_reference", world_prompt="",
                           time_of_day="", is_first_frame=False, action_context="",
                           hd_mode=False, output_dir=OUT)
    print(f"   -> {a}\n")

    print("B. style reference = the blurred swatch (what a hard cut DOES today)")
    b = generate_krea_img2img(prompt=PROMPT, caption="b_blurred_swatch_ref",
                              reference_image_path=[swatch], world_prompt="",
                              time_of_day="", action_context="", hd_mode=False,
                              output_dir=OUT)
    print(f"   -> {b}\n")

    print("C. style reference = the real previous frame (sharp)")
    c = generate_krea_img2img(prompt=PROMPT, caption="c_sharp_frame_ref",
                              reference_image_path=[args.source], world_prompt="",
                              time_of_day="", action_context="", hd_mode=False,
                              output_dir=OUT)
    print(f"   -> {c}\n")

    print(f"written to {OUT.resolve()}")


if __name__ == "__main__":
    main()
