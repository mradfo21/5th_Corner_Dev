"""Why did a replay run miss? Name the argument that moved.

A hit rate on its own is not actionable. "27% of calls missed" says the run was
not reproducible but not *what* varied, and the candidates are all plausible: a
timestamp baked into a prompt, a reference frame that came back one pixel
different, a random seed, a choice the harness picked differently.

Every miss in a replay run writes its key payload to `.cache/replay/_misses/`,
and every recording keeps the payload it was stored under. So the question is
answerable by comparison: for each miss, find the recordings of the same kind,
pick the closest one, and report the fields that differ. Fixing determinism then
becomes a list of named arguments rather than a hunt.

    python tools/replay_misses.py             # summarise every miss
    python tools/replay_misses.py --full      # show the differing values too
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The Windows console is cp1252 and the payloads are full of prompt text.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import replay_cache  # noqa: E402


def _load(path: Path):
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def recordings_by_kind(kind: str, before: str = "") -> list:
    """Recordings a replay run could legitimately have hit.

    Entries written *during* a replay run are misses that fell through to the
    real call. Comparing a miss against one of those always reports a perfect
    match and explains nothing -- it is the miss comparing itself. Older caches
    predate the `during_mode` marker, hence `--before` as a manual escape.
    """
    out = []
    folder = replay_cache.CACHE_DIR / kind
    if not folder.exists():
        return out
    for meta in folder.rglob("meta.json"):
        data = _load(meta)
        if not isinstance(data, dict) or not isinstance(data.get("args"), dict):
            continue
        if data.get("during_mode") == replay_cache.MODE_REPLAY:
            continue
        if before and str(data.get("recorded_at") or "") >= before:
            continue
        out.append(data)
    return out


def similarity(a: dict, b: dict) -> float:
    """How alike two key payloads are, so the closest recording can be found.

    Weighted towards the fields both share having equal values; a payload that
    matches on nine of ten arguments is almost certainly the recording this miss
    was meant to hit, and the tenth is the answer.
    """
    keys = set(a) | set(b)
    if not keys:
        return 0.0
    same = sum(1 for k in keys if json.dumps(a.get(k), sort_keys=True, default=str)
               == json.dumps(b.get(k), sort_keys=True, default=str))
    return same / len(keys)


def differing_fields(miss: dict, recorded: dict) -> list:
    out = []
    for key in sorted(set(miss) | set(recorded)):
        left = json.dumps(miss.get(key), sort_keys=True, default=str)
        right = json.dumps(recorded.get(key), sort_keys=True, default=str)
        if left != right:
            out.append((key, right, left))
    return out


def describe_change(field: str, recorded: str, missed: str) -> str:
    """A one-line reading of how a field moved, which usually names the cause."""
    if recorded.startswith('"file:') and missed.startswith('"file:'):
        return "the reference image had different pixels"
    if len(recorded) > 200 or len(missed) > 200:
        ratio = difflib.SequenceMatcher(None, recorded, missed).ratio()
        if ratio > 0.95:
            return f"nearly identical text ({ratio:.3f}) — something small is embedded in it"
        return f"the text differs substantially ({ratio:.3f}) — an upstream call produced different output"
    return f"{recorded} -> {missed}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true", help="print the differing values in full")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--before", default="", metavar="'YYYY-MM-DD HH:MM'",
                    help="ignore recordings made at or after this time (for caches "
                         "written before entries carried a during_mode marker)")
    args = ap.parse_args(argv)

    misses_dir = replay_cache.CACHE_DIR / "_misses"
    if not misses_dir.exists():
        print("no misses recorded — run a replay-mode session first")
        return 0

    files = sorted(misses_dir.glob("*.json"))[: args.limit]
    if not files:
        print("no misses recorded")
        return 0

    cache = {}
    blame = {}

    print(f"{len(files)} miss(es) in {misses_dir}\n")
    for path in files:
        miss = _load(path)
        if not isinstance(miss, dict):
            continue
        kind = miss.get("kind") or "?"
        payload = miss.get("args") or {}
        if kind not in cache:
            cache[kind] = recordings_by_kind(kind, args.before)
        candidates = cache[kind]

        print(f"-- {path.name}  [{kind}]")
        excerpt = (miss.get("excerpt") or "").replace("\n", " ")[:110]
        if excerpt:
            print(f"   asked for: {excerpt}")
        if not candidates:
            print("   nothing of this kind was ever recorded\n")
            blame.setdefault("(nothing recorded for this kind)", 0)
            blame["(nothing recorded for this kind)"] += 1
            continue

        best = max(candidates, key=lambda c: similarity(payload, c["args"]))
        score = similarity(payload, best["args"])
        fields = differing_fields(payload, best["args"])
        print(f"   closest recording: {best['key'][:12]}  similarity {score:.2f}"
              f"  ({len(fields)} field(s) differ)")
        for field, recorded, missed in fields:
            print(f"     * {field}: {describe_change(field, recorded, missed)}")
            blame[field] = blame.get(field, 0) + 1
            if args.full:
                print(f"         recorded: {recorded[:400]}")
                print(f"         this run: {missed[:400]}")
        print()

    if blame:
        print("=" * 60)
        print("Fields responsible, most frequent first — fix these to raise the hit rate:")
        for field, count in sorted(blame.items(), key=lambda kv: -kv[1]):
            print(f"  {count:3d}  {field}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
