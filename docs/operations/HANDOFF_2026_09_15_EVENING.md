# Handoff — flipbook everywhere, INTERACT dive, choice UI

Written at the end of a long session, continuing from `HANDOFF_2026_09_15.md`.
Everything below is either **verified by a real playthrough** or explicitly
marked **UNVERIFIED**. Trust the markings — I got burned several times today by
assuming instead of measuring, and once by "fixing" a diagnosis that was wrong.

---

## How to run (read this first, it has three traps in it)

```powershell
cd C:\VersionControl\5th_Corner_Dev
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3
$env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS="--remote-debugging-port=9333"
Start-Process -FilePath python -ArgumentList "play.py" -RedirectStandardOutput logs\play_actual.log -RedirectStandardError logs\play_actual.err.log
# poll http://127.0.0.1:9333/json/list until a *standalone* target appears, then:
$env:PT_TURNS="6"; $env:PT_TURN_TIMEOUT="150"
Start-Process -FilePath python -ArgumentList "-u","playtest_app.py" -RedirectStandardOutput _pt_out.txt -RedirectStandardError _pt_err.txt
```

**TRAP 1 — the turn plan moved.** It is now

```python
["choice", "choice", "scan_move", "photo", "scan_interact", "encounter", "act", "camp"]
```

so **the encounter is TURN 6**, not turn 4. I wasted two full runs and told the
user "changes didn't work" because I kept using `PT_TURNS=4` after moving it
myself. `PT_TURNS=6` for encounter work, `PT_TURNS=5` for the INTERACT dive.

**TRAP 2 — Python edits need an app restart.** CSS/JS reload with the page
(`asset_version` is mtime-based), but `engine.py` / `encounter.py` changes do
**not** apply to a running `play.py`. I spent a cycle diagnosing "silent
failure" that was just a stale process. Check
`(Get-Item encounter.py).LastWriteTime` against the process `CreationDate`.

**TRAP 3 — CSS source order.** `standalone.css` sets `display` on individual
elements around `:2046`. Hide rules placed *earlier* in the file silently lose.
I "removed" the hub buttons three times before noticing; the rules now live at
the **end** of the file with `!important`. If a `display: none` seems ignored,
check for a later rule before doubting the selector.

Still true from the last handoff: **run `python check_js_syntax.py` after EVERY
`.js` edit**, and CDP screenshots do not capture the WebView2 video layer.

---

## Verified working (don't re-fix)

- **INTERACT close-up dive.** 4 consecutive runs: `dive: opened=True
  crop_pinned=True close_up=True`. The root cause was *not* the guard or
  scoping the previous handoff suspected — `moments.js` loads before
  `standalone.js`, registration was fine, and `Moments.push("interact")` works.
  The bug was `enter()` posting `/api/talk/portrait` **without
  `reference_image`**; that endpoint returns `{"image_url": null, "reason":
  "no_crop"}` by design rather than invent a subject (see
  `test_talk_portrait_object_without_crop_does_not_invent_a_person`). Fix:
  crop the object's own pixels *before* `Moments.push` (Talk's discipline),
  pin that crop, then img2img from it. `object_subject` was already wired
  server-side at `engine.py:11745`.
- **INTERACT exit.** `dive: left on the X, landed back in the scene`. The ✕
  appears only once the regenerated frame exists, and Esc/✕ both wait for it.
- **Flipbook renders and plays.** `[FLIPBOOK] generating 2x2 grid (4 frames)`.
  It had never run before: `tunables.apply_all()` was *already* called at
  `api.py:293` (the previous handoff's diagnosis was wrong) — the real cause was
  state init hardcoding `flipbook_mode: False`, which shadowed the global
  because `flipbook_settings()` only falls back when it is `None`.
- **Encounter chrome** unified into one borderless left column (user approved).
  `body.moment-encounter` *was* always applied — `moments.js:146` builds it as
  `"moment-" + type`, so a literal grep can't find it.
- **Choices are visible**: 4-row centred stack (3 written + `Custom`), hub
  reduced to the camera only. `#choices-container` had been `display: none`.
- **Choice fidelity 1.00** on every sampled choice (the words the player picks
  do reach the prose).

### Harness improvements (all in `playtest_app.py`)
`DiveWatch` (observes the dive *during* the turn), the `continuity()` report
(was dead code), per-round encounter slates, choice-fidelity scoring,
movement-choice preference, click-to-scan (there is no SCAN button any more),
and `play_out_encounter()` so an encounter that interrupts *any* turn gets
played out instead of freezing the waiter.

---

## UNVERIFIED (written, never observed)

1. **Flipbook frame timing** — 1s/frame holding on the last. The restart bug was
   real (`playSceneSequence` never passed `key`, so every feed poll replayed from
   frame 1 — that's why only 2 frames were ever seen), but nobody has watched
   4 panels advance a second apart.
2. **Black-frame double buffering.** Panels on disk are *not* black (measured:
   luma 50–64), so it was always a paint artifact. New `paintSequenceFrame()`
   paints the hidden A/B layer and flips `scene-active`. Deliberately does **not**
   call `markScenePainted()` — that's the boot gate *and* the interact dive's
   hand-off, so it must fire once per scene, not per frame.
3. **img2img continuity fix**, both paths (`engine.py:15915` main turn,
   `:16418` intro). Precedence was backwards: the flipbook's last panel was only
   a *fallback*, so a flipbook turn could seed the next generation from a frame
   taken before the motion ("make a helicopter appear, next turn you're back
   where you started"). Now the last panel wins when `current_sequence` proves
   this pass drew one.
4. **Narrator at bottom** (`#narrator-bar` 132px → 16px; it was inside the
   choice stack's band).
5. **Hardened flipbook prompt** — no generation since I removed the timing
   language that was being drawn into frames as "1+6 sec".

---

## Open bugs, in the order I'd take them

### 1. Encounter flipbook fails: "no grid came back"
Both encounter stages now generate **straight to frames in one pass**
(`encounter.py`: `_plate_sequence()`, called from `api_begin` and
`_generate_resolve_plate`, which returns early with `gen_mode="flipbook"`). The
last panel *is* the plate, so pinning / SCAN / vision / next img2img still get
one true image.

It does not work yet. Verified log line: `[ENCOUNTER] flipbook plate: no grid
came back - staying a still`. This **rules out** the `flipbook_active` gate and
the `already_open` cached-plate path — flipbook is on, `_flipbook_generate` is
returning `None`.

My last change (UNVERIFIED) validates the reference exists on disk and retries
from the prompt alone if the grid still fails, because test runs showed
`No such file or directory: ...Temp\...\img2img_1.png`. Next run prints one of:
`reference is gone ... generating without it` / `grid failed with the plate
reference - retrying from the prompt alone` / `no grid came back`. If it's still
the last one, the reference is innocent and the next step is **logging inside
`engine._flipbook_generate`, which currently returns `None` without saying why**.

Keep the reference. I removed it at one point (user asked not to base it on the
original image) and that broke
`test_enter_plate_is_the_img2img_init`, which guards a real lesson recorded in
the code: with no reference the punch came back with *"new faces, new clothes,
and an indoor shed where the standoff had been an outdoor yard."* The plate is
not a second generation — it is this generation's img2img init.

### 2. The "Joel from The Last of Us" plate (4 pre-existing test failures)
These fail on a clean tree and **are** the bug the user reported. I dismissed
them as unrelated noise first — read the assertions:

```
test_encounter_fight.py:438  expected "plaid" (the stranger)
                             got "a man in a green quilted vest and a dark baseball cap"
test_encounter_fight.py:476  'the man has long,' found in
                             'the man has long, matted hair and a torn flannel shirt,'
test_encounter.py:289        captions: 4 != 2
```

The stranger's description is **split at its commas**, so the model gets
`"the man has long,"` and invents the rest — a half-described rugged man in a
post-industrial yard is exactly a generic Last-of-Us face. The caption count
(4 vs 2) is the plate failing a validation and regenerating: `ERROR: [ENCOUNTER]
resolve plate missing the other body — retrying`. That retry costs a duplicate
generation per beat and is where a custom action's framing drifts.

### 3. Custom actions in encounters are snapped to lanes
`match_encounter_choice` (`encounter.py:2042`) maps typed text onto
confront/evade/parley and the **lane** drives the roll, so "set the man on fire"
becomes "you chose violence". The non-encounter path is fine — the client tags
`source: "typed"` (`standalone.js:23283`) and the server honours it
(`engine.py:8364`). Design question: how does an arbitrary sentence get a
difficulty?

### 4. Encounter choices repeat (partially fixed, UNVERIFIED)
Lanes are fixed by `DEFAULT_CHOICE_OVERLAY` + `ENCOUNTER_CHOICE_SCHEMA`
(`confront/evade/parley`), so every round was the opening slate reworded. Per
user's choice I kept the 3 lanes and added round number, `enemy_state`, an
escalation rule and an "ALREADY OFFERED — do not reword these" list to
`encounter_choice_overlay(..., continued=True)`. One run showed no repeats, but
that was before later changes.

### 5. Black resolve plates / stuck `…` nameplate
The harness reports **19 black samples** during encounter resolution, and the
user saw a black frame with only the nameplate. `…` is the placeholder from
`moments.js:157` that should be overwritten when the brief lands — seeing it
means the brief never arrived. Probably the same root cause as the black plates.
Fix the load before styling anything over it.

### 6. Centre the action text (needs a state class)
The action beat should show only the verb, centred. Both the standoff and the
action beat share `#moment-nameplate`, so it needs scoping: add
`document.body.classList.add("moment-acting")` at the resolve-beat
`setNameplate(name, verb)` call and remove it on pop, then the CSS next to
`standalone.css:5464`:

```css
body.moment-acting #moment-nameplate { left: 50%; transform: translateX(-50%); text-align: center; }
body.moment-acting #moment-nameplate-name { display: none; }
body.moment-acting #moment-nameplate-sub { -webkit-line-clamp: none; }
```

### 7. Photography (user: "terrible")
Viewfinder only pans left/right — vertical aim appears dead (`no lock-on
offered` on **every** run today). The partial-opacity rectangle on shutter is
`#capture-cinema`; it should be a single white flash then the photo,
found-footage style. `do_photo()` already drives raise → aim → shutter → cinema,
so a fix is provable in 2 turns — but if the flash self-clears, remove the
harness's "tap through" step or it will wait on something that never appears.
Also: the viewfinder took **11s–75s** to come up across runs.

### 8. Smaller items
- **INTERACT has no object information** — the user asked for it. No endpoint
  describes a scanned object (`/api/investigate` files photo evidence). Either a
  new short vision call on the crop (cost/latency) or surface the turn's own
  dispatch when it lands (free but late).
- **Choices in the dive** are still not built (gap from the last handoff).
- **`grid_prompt(frames, seconds=...)`** — `seconds` is now unused, since the
  duration is no longer stated numerically. Dead parameter.
- **`gemini_image_utils.py:74`** — `GEMINI_PRO_IMAGE` comment still claims "4K
  support", stale for the 3.1 Lite line. Nothing in play sets `hd_mode=True`
  (all 15 call sites pass `False`), so play is entirely on
  `gemini-3.1-flash-lite-image`. HQ tier is intentionally kept but unused.
- **"The world hesitated. Choose again."** appeared in turn 6's prose in the last
  run — the watchdog the previous handoff said was fixed. Worth one look.

---

## Current settings (`tunables.json`, survives restarts now)

```json
{"flipbook_enabled": true, "flipbook_frame_ms": 1000, "flipbook_frames": "4"}
```

4 frames = 2×2 grid = 672×376 panels. Frames trade directly against resolution
(one generation split up): 2 → 688×768, 8 → 336×376, 16 → 344×192. The Lite
model is capped at 1K, so **fewer frames is the only way to raise panel
resolution** without moving to the HQ model.

---

## Two stash entries exist

`git stash list` shows two WIP entries from my test-isolation checks. They hold
**doc renames and `prompts/simulation_prompts.json`** — pre-existing working-tree
noise, not my work. A `pop` failed on an untracked-file collision. I left them
alone; verify before dropping.
