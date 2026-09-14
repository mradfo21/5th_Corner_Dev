"""Can the enemy inherit the player's clothes?

The player was rendered in a green vest and cap even though the sheet says
olive field jacket. This checks whether the clone guard recognises the
outfit the frames actually drew, and whether the cast lock still invents a
vest and a cap that the world does not have.
"""

import os
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

env = ROOT / ".env"
if env.exists():
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

import encounter as E  # noqa: E402

# What the last exploration frame said. One person in it: the player.
FRAME = ("A man in a green quilted vest and a dark baseball cap stands in a "
         "dirt yard, holding a camcorder, chain-link fence and a rusted "
         "pickup truck behind him.")

ENEMY_LOOKS = [
    "a man in a green quilted vest and a dark baseball cap",   # the bug
    "a man in a baseball cap",                                  # partial steal
    "a man in a grease-stained grey coverall",                  # legitimate
    "a woman in a torn red windbreaker",                        # legitimate
    "a man beside a chain-link fence near a rusted truck",      # scenery only
]


def rule(t):
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


rule("SHEET vs FRAME")
print("sheet wardrobe   :", E.player_wardrobe_text())
print("sheet tokens     :", sorted(
    t for t in E.player_wardrobe_tokens() if t not in E.observed_player_tokens()))

with mock.patch.object(E, "observed_player_look", return_value=FRAME):
    E._LOOK_READ_CACHE.clear()
    print("observed tokens  :", sorted(E.observed_player_tokens()))
    print("observed outfit  :", E.observed_player_wardrobe())

    rule("DOES THE GUARD CATCH A STOLEN OUTFIT?")
    for look in ENEMY_LOOKS:
        clone = E.look_clones_player(look)
        kept = E.distinct_enemy_look(look)
        stolen = "STOLEN" if clone else "ok"
        print(f"  [{stolen:6}] {look}")
        if clone:
            print(f"            -> replaced with: {kept}")

    rule("CAST LOCK")
    print(E.player_cast_lock())

rule("WITHOUT ANY FRAME TO LEARN FROM (must not over-block)")
with mock.patch.object(E, "observed_player_look", return_value=""):
    E._LOOK_READ_CACHE.clear()
    for look in ENEMY_LOOKS[2:]:
        print(f"  clones={E.look_clones_player(look)!s:5} {look}")
