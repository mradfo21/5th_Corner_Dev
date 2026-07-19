"""Aggressive race test that POLLS /api/feed continuously DURING turn
processing — reproducing the real production access pattern (every browser
polls the feed every ~1s while a turn is being computed in the background).

This exercises the set_active_session() global-mirror swap racing against the
background turn pipeline, which the after-the-fact-only poll test missed.
"""
import concurrent.futures
import json
import os
import threading
import time
import urllib.request

# Requires a LIVE server: PORT=5097 python3 api.py  (then run this script).
BASE = os.environ.get("SOMEWHERE_BASE_URL", "http://localhost:" + os.environ.get("PORT", "5097"))


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read())


def create_session(name):
    d = post("/api/lobby/create", {"name": name})
    return d["data"]["session_id"]


N = 5
ROUNDS = 4
all_failures = []

print(f"Creating {N} sessions...")
sids = [create_session(f"Session-{i}") for i in range(N)]
print("Session ids:", sids)

print("Resetting all sessions CONCURRENTLY...")
with concurrent.futures.ThreadPoolExecutor(max_workers=N) as ex:
    for f in [ex.submit(post, f"/api/reset?session_id={sid}", {}) for sid in sids]:
        f.result()

# Continuous background pollers — one per session — hammering /api/feed the
# whole time, exactly like real browsers do while a turn is computed.
_stop = threading.Event()
_poll_seen = {sid: set() for sid in sids}
_poll_lock = threading.Lock()


def poller(sid):
    while not _stop.is_set():
        try:
            feed = get(f"/api/feed?session_id={sid}")
            texts = " ".join(str(it.get("content", "")) for it in feed)
            with _poll_lock:
                _poll_seen[sid].add(texts)
        except Exception:
            pass
        time.sleep(0.3)


pollers = [threading.Thread(target=poller, args=(sid,), daemon=True) for sid in sids]
for p in pollers:
    p.start()

try:
    for rnd in range(ROUNDS):
        print(f"\n--- ROUND {rnd} (polling continuously during processing) ---")
        choice_text = {sid: f"MARK_R{rnd}_{i}_{sid}" for i, sid in enumerate(sids)}
        with concurrent.futures.ThreadPoolExecutor(max_workers=N) as ex:
            futs = {
                sid: ex.submit(post, "/api/choose", {"choice": choice_text[sid], "session_id": sid})
                for sid in sids
            }
            for sid, f in futs.items():
                f.result()
        time.sleep(8)  # let turns finish while pollers keep hammering

        for sid in sids:
            feed = get(f"/api/feed?session_id={sid}")
            contents = " ".join(str(it.get("content", "")) for it in feed)
            for other in sids:
                if other == sid:
                    continue
                if choice_text[other] in contents:
                    all_failures.append(f"R{rnd}: LEAK: {sid}'s feed contains {other}'s marker")
            if choice_text[sid] not in contents:
                all_failures.append(f"R{rnd}: MISSING: {sid}'s feed missing its own marker")
finally:
    _stop.set()
    time.sleep(0.5)

# Also assert no poller ever OBSERVED another session's marker in a feed snapshot
for sid in sids:
    with _poll_lock:
        snapshots = list(_poll_seen[sid])
    for other in sids:
        if other == sid:
            continue
        for rnd in range(ROUNDS):
            marker = f"MARK_R{rnd}_{sids.index(other)}_{other}"
            if any(marker in s for s in snapshots):
                all_failures.append(f"POLL-LEAK: a /api/feed?session_id={sid} snapshot contained {other}'s marker {marker}")

default_state = json.load(open("sessions/default/state.json"))
print(f"\ndefault turn_count={default_state.get('turn_count')} feed_log_len={len(default_state.get('feed_log', []))}")

if all_failures:
    print("\n=== FAILURES ===")
    for f in sorted(set(all_failures)):
        print(" -", f)
    raise SystemExit(1)
else:
    print("\n=== ALL SESSIONS ISOLATED EVEN UNDER CONTINUOUS CONCURRENT POLLING ===")

print("\nCleaning up test sessions...")
for sid in sids:
    try:
        req = urllib.request.Request(BASE + f"/api/sessions/{sid}", method="DELETE")
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print("cleanup failed for", sid, e)
print("Done.")
