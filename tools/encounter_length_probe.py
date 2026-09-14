"""How many rounds does a confrontation take to end?

The player reported that encounters never end. The release rules live in
`encounter_releases`, and the escalation that is supposed to reach them lives
in `advance_enemy_state`, so this walks the real roll loop many thousands of
times and reports the distribution instead of clicking through 30s renders.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

env = ROOT / ".env"
if env.exists():
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

import encounter as E  # noqa: E402


def run(lane_pick, trials=20000, cap=40):
    lens, unfinished, how = [], 0, {}
    for _ in range(trials):
        enemy, cond, n = "ready", "ok", 0
        while True:
            n += 1
            if n > cap:
                unfinished += 1
                lens.append(cap + 1)
                break
            r = E.roll_encounter_outcome(
                lane_pick(n), stance="hostile", kind="person",
                condition=cond, fate="NORMAL", enemy_state=enemy, round_no=n,
            )
            enemy, cond = r["enemy_state"], r["condition"]
            if E.encounter_releases(r["outcome"], {"enemy_state": enemy}):
                lens.append(n)
                key = r["outcome"] if r["outcome"] in ("escape", "die") else "enemy down"
                how[key] = how.get(key, 0) + 1
                break
    lens.sort()
    return lens, unfinished, how


def main():
    plans = (
        ("always CONFRONT", lambda n: "confront"),
        ("always EVADE", lambda n: "evade"),
        ("always USE", lambda n: "use"),
        ("mixed", lambda n: ("confront", "use", "evade")[n % 3]),
    )
    for name, pick in plans:
        lens, unfinished, how = run(pick)
        total = sum(how.values()) or 1
        print("{:17} median={:3}  p90={:3}  p99={:3}  worst={:3}  never-ended={}".format(
            name, lens[len(lens) // 2], lens[int(len(lens) * .90)],
            lens[int(len(lens) * .99)], lens[-1], unfinished))
        print("{:17} ends by: {}".format(
            "", ", ".join("{}={}%".format(k, round(v * 100 / total))
                          for k, v in sorted(how.items()))))


if __name__ == "__main__":
    main()
