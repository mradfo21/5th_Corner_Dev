"""Build the shipped starter character (assets/characters/<id>/) from a
turnaround and an idle drawn elsewhere — Jason Fleece from the 2026-09-23
character spike (_claude_chars/jason: the blue PRESS flak vest the runs are
written with, not the poster's plate carrier). Reads the garment words back
off the turnaround and draws the face sheet (needs a Gemini key).

    python tools/build_starter_character.py _claude_chars/jason
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import play, keys_store  # noqa: E402
play._load_keys(); keys_store.load_into_environ()

import characters  # noqa: E402

JASON = {
    "id": "jason-fleece",
    "name": "Jason Fleece", "pronouns": "he/him", "role": "Freelance Photojournalist",
    "tagline": "Freelance photojournalist.", "demeanor": "dry, stubborn, curious",
    "concept": ("A freelance photojournalist in his thirties who went over the fence "
                "for the picture nobody else would take."),
    "who": ("Jason Fleece, a freelance photojournalist in his thirties: sun-lightened "
            "shaggy brown hair, stubble. White crew-neck t-shirt; a denim-blue flak vest "
            "with PRESS in big white capitals across the chest and across the back; a red "
            "bandana knotted at the neck; a gas mask hanging at the collarbone; dark blue "
            "jeans; brown leather work boots; a 35mm camera on a strap across the body."),
    "held": "the camera held low in one hand",
}


def main(src: str) -> None:
    src_dir = Path(src)
    out = characters.STARTERS_DIR / JASON["id"]
    if out.exists():
        shutil.rmtree(out)
    # Built in the live store under a scratch id, then moved into assets/.
    rec = characters._new_record(JASON["name"], JASON["concept"], source="starter")
    rec.update({k: v for k, v in JASON.items() if k != "id"})
    rec["id"] = JASON["id"] + "-build"
    characters.save(rec)
    rec = characters.adopt_renders(rec, src_dir / "turnaround_key.png", src_dir / "idle_key.png")
    built = characters.char_dir(rec["id"])
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(built), str(out))
    data = json.loads((out / "character.json").read_text(encoding="utf-8"))
    data["id"] = JASON["id"]
    data["origin"] = "starter"
    data["record"] = {"runs": 0, "deaths": 0, "worlds": [], "turns": 0}
    data["last_played"] = 0
    (out / "character.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    look = json.loads((out / "looks" / data["base_look"] / "look.json").read_text(encoding="utf-8"))
    print("starter:", out)
    print("wardrobe:", look.get("wardrobe_line"))
    print("garments:", json.dumps(look.get("garments"), indent=1))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "_claude_chars" / "jason"))
