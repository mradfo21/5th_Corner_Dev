"""Staggered-choose race test: fire each session's /api/choose at a slightly
different time so one session's choose (which still swaps the global mirror via
_session_scoped) lands WHILE another session's background turn is mid-flight.
Combined with continuous /api/feed polling. Exercises the choose-during-turn
mirror-swap race that Fix B (local_only pipeline) closes.
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
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read())


N = 5
ROUNDS = 3
failures = []

sids = [post("/api/lobby/create", {"name": f"S{i}"})["data"]["session_id"] for i in range(N)]
print("sessions:", sids)
with concurrent.futures.ThreadPoolExecutor(max_workers=N) as ex:
    [f.result() for f in [ex.submit(post, f"/api/reset?session_id={s}", {}) for s in sids]]

_stop = threading.Event()


def poller(sid):
    while not _stop.is_set():
        try:
            get(f"/api/feed?session_id={sid}")
        except Exception:
            pass
        time.sleep(0.25)


for sid in sids:
    threading.Thread(target=poller, args=(sid,), daemon=True).start()

try:
    for rnd in range(ROUNDS):
        print(f"--- ROUND {rnd}: staggered choices ---")
        marks = {s: f"STAG_R{rnd}_{i}_{s}" for i, s in enumerate(sids)}
        # Fire each session's choose ~0.8s apart so later chooses land during
        # earlier sessions' in-flight turns.
        threads = []
        for i, s in enumerate(sids):
            def fire(s=s):
                post("/api/choose", {"choice": marks[s], "session_id": s})
            t = threading.Thread(target=fire)
            t.start()
            threads.append(t)
            time.sleep(0.8)
        for t in threads:
            t.join()
        time.sleep(8)
        for s in sids:
            feed = get(f"/api/feed?session_id={s}")
            contents = " ".join(str(it.get("content", "")) for it in feed)
            for other in sids:
                if other != s and marks[other] in contents:
                    failures.append(f"R{rnd}: LEAK: {s} contains {other}'s mark")
            if marks[s] not in contents:
                failures.append(f"R{rnd}: MISSING: {s} missing own mark")
finally:
    _stop.set()
    time.sleep(0.5)

default_state = json.load(open("sessions/default/state.json"))
print(f"default turn_count={default_state.get('turn_count')} feed_len={len(default_state.get('feed_log', []))}")

if failures:
    print("=== FAILURES ===")
    for f in sorted(set(failures)):
        print(" -", f)
    raise SystemExit(1)
print("=== STAGGERED-CHOOSE: ALL SESSIONS ISOLATED ===")
for s in sids:
    try:
        urllib.request.urlopen(urllib.request.Request(BASE + f"/api/sessions/{s}", method="DELETE"), timeout=10)
    except Exception:
        pass
print("done")
