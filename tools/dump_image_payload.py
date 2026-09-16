"""Print the full text payload one image call sends, for prompt auditing."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine  # noqa: E402
import game_identity  # noqa: E402
import prompts_store  # noqa: E402

SCENE = (
    "Jagged sandstone ledge sits in deep twilight, a swirl of red dust closing "
    "from the left, the crate now split open at your feet."
)


def report(label: str, text: str) -> None:
    words = len(text.split())
    print("=" * 78)
    print(f"{label}  ({len(text)} chars / {words} words)")
    print("=" * 78)
    print(text)
    print()


def audit(text: str) -> None:
    print("-" * 78)
    print("leak audit")
    print("-" * 78)
    for term in (
        "camcorder", "VHS", "viewfinder", "tape", "HUD", "timecode",
        "REC", "1993", "photograph", "render", "CGI", "grain",
    ):
        hits = len(re.findall(re.escape(term), text, re.I))
        if hits:
            print(f"{term:<12} {hits}")
    print()


def main() -> None:
    # Every prompt surface reads the saved spec, so switching perspective means
    # writing to it. Put the author's camera back afterwards — this is a probe,
    # not an edit.
    saved = dict(game_identity.get_spec()[game_identity.CAMERA_KEY])
    try:
        for mode in ("first_person", "third_person"):
            game_identity.save_spec({game_identity.CAMERA_KEY: {"mode": mode}})
            base = engine.build_image_prompt(
                player_choice="Bind leg with torn shirt",
                dispatch=SCENE,
                narrative_dispatch="Your hands shake as you knot the fabric.",
            )
            full = engine._build_vhs_prompt(base, use_img2img=True)
            report(f"[{mode}] full payload (img2img)", full)
            audit(full)
    finally:
        game_identity.save_spec({game_identity.CAMERA_KEY: saved})


if __name__ == "__main__":
    main()
