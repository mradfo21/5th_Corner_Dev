"""Print the clause scores so the tie-break can be reasoned about."""

import os
import re
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

import encounter as E  # noqa: E402

PLATE = ("A man in a green quilted vest and a dark baseball cap faces an "
         "older man in a plaid shirt holding a shotgun.")
INVENTED = "an older man wearing a baseball cap and plaid shirt"

with mock.patch.object(E, "observed_player_look", return_value=""):
    E._LOOK_READ_CACHE.clear()
    wanted = E.wardrobe_tokens_from_text(INVENTED)
    wanted_words = E._look_words(INVENTED)
    owned = E.player_wardrobe_tokens()
    print("wanted garments:", sorted(wanted))
    print("wanted words   :", sorted(wanted_words))
    print("owned          :", sorted(owned))
    print()
    stripped = E._strip_player_clauses(PLATE)
    for clause in E._PERSON_SPLIT_RE.split(stripped):
        if not clause or not clause.strip():
            continue
        c = re.sub(
            r"^(?:(?:and|but|while|whilst|with|facing|faces|confronting|"
            r"confronts|opposite|before|behind|beside|another|the other|"
            r"across from|in front of)\s+)+", "", clause.strip(), flags=re.I)
        c = re.sub(
            r"\s+\b(?:stands?|standing|stood|occupy|occupies|occupying|is|are|"
            r"was|were|sits?|sitting|steps?|stepping|moves?|moving|walks?|"
            r"walking|advances?|advancing|lunges?|lunging|blocks?|blocking|"
            r"raises?|raising|swings?|swinging|reaches?|reaching)\b.*$",
            "", c, flags=re.I).strip(" ,.;:")
        if not c:
            continue
        worn = E.wardrobe_tokens_from_text(c)
        words = E._look_words(c)
        clones = E.look_clones_player(c)
        noun = E._first_person_noun(c)
        score = (2 * len(worn & wanted) + len(words & wanted_words)
                 - 3 * len(worn & owned) - 2 * len(words & owned))
        bonus = 1 if re.search(r"\b(?:wearing|dressed|in|with)\b", c, re.I) else 0
        print(f"clause : {c}")
        print(f"  garments {sorted(worn)}")
        print(f"  words    {sorted(words)}")
        print(f"  g-hit {len(worn & wanted)}  w-hit {len(words & wanted_words)}"
              f"  clones={clones} noun={noun!r}")
        print(f"  SCORE {score + bonus}")
        print()
    print("picked:", E.plate_stranger_look(PLATE, fallback=INVENTED))
