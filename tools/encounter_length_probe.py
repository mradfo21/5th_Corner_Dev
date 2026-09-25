"""How long is a fight, and how often does it kill you?

A fight is played by combat.play_round (d20 rules — see combat.py). Every
round of it is a real generation on screen, so the length of a fight is the
thing a player feels most; this plays thousands of fights through the real
rules instead of clicking through renders, and prints, per opponent and per
way of playing: median / p90 / worst rounds, how the fights ended, and the
death rate from a given starting HP.

    python tools/encounter_length_probe.py
    python tools/encounter_length_probe.py --hp 12      # start hurt
"""

import argparse
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import combat as C  # noqa: E402


def fight(rng, *, kind, stance, boss, detection, plan, hp, weapon, armor):
    char = {"label": "A foe", "kind": kind, "stance": stance}
    rec = C.open_combat(char, you_name="Isaac", you_hp=hp, you_max=C.PLAYER_MAX_HP,
                        boss=boss, detection=detection, rng=rng)
    you = C.player_block(name="Isaac", weapon="Baton" if weapon else "",
                         armor="Vest" if armor else "")
    armor_ready = armor
    for n in range(1, 40):
        lane = plan(n, rec)
        out = C.play_round(rec, lane=lane, verb="Hit him", you=you, you_hp=hp, rng=rng,
                           armor_ready=armor_ready)
        if out["armor_used"]:
            armor_ready = False
        hp = out["exchange"]["you"]["hp"]
        rec = out["after"]
        if out["end"]:
            return n, out["end"], hp
    return 40, "never", hp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hp", type=int, default=C.PLAYER_MAX_HP)
    ap.add_argument("--n", type=int, default=20000)
    args = ap.parse_args()
    rng = random.Random(1)
    plans = {
        "attack": lambda n, r: "confront",
        "reason": lambda n, r: "parley",
        "flee": lambda n, r: "evade",
        "hit, then talk": lambda n, r: "parley" if C.bloodied(r["foe"]) else "confront",
    }
    foes = [
        ("person hostile", dict(kind="person", stance="hostile", boss=False)),
        ("person opportun.", dict(kind="person", stance="opportunistic", boss=False)),
        ("person desperate", dict(kind="person", stance="desperate", boss=False)),
        ("creature", dict(kind="creature", stance="hostile", boss=False)),
        ("BOSS person", dict(kind="person", stance="hostile", boss=True)),
    ]
    print(f"start HP {args.hp}/{C.PLAYER_MAX_HP}, suspicious (rolled initiative), no gear unless noted\n")
    for fname, fk in foes:
        for pname, plan in plans.items():
            for gear in (False, True):
                if gear and pname != "attack":
                    continue
                lens, ends, hps = [], {}, []
                for _ in range(args.n):
                    n, end, hp = fight(rng, detection=1, plan=plan, hp=args.hp,
                                       weapon=gear, armor=gear, **fk)
                    lens.append(n)
                    ends[end] = ends.get(end, 0) + 1
                    hps.append(hp)
                lens.sort()
                q = lambda f: lens[min(len(lens) - 1, int(len(lens) * f))]
                tot = args.n
                how = "  ".join(f"{k} {100 * v / tot:4.1f}%" for k, v in sorted(ends.items()))
                label = pname + (" +weapon+armour" if gear else "")
                print(f"{fname:17} {label:26} median {q(.5)}  p90 {q(.9)}  worst {lens[-1]:2}  "
                      f"hp left {sum(hps) / tot:4.1f}  | {how}")
        print()


if __name__ == "__main__":
    main()
