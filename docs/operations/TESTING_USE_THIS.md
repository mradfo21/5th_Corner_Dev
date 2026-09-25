# Read this before you write a test

**Do not write a new playtest script.** One already exists, it drives the real
application, and it encodes a dozen hard-won details about how this game's UI
actually works. Every ad-hoc script written against this codebase has failed
the same way: it looks for `#choices .choice-btn`, finds nothing, reports "the
game is stuck", and sends you chasing a bug that isn't there.

This game does **not** have a list of text buttons. It has an action wheel, a
SCAN pass that tags objects in the frame, per-tag sub-action bars, a camera
mode, and Moments (encounters) with an entirely separate choice system. A test
that doesn't know that cannot drive it.

## The one command

```powershell
cd C:\VersionControl\5th_Corner_Dev
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3
$env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS="--remote-debugging-port=9333"
Start-Process -FilePath python -ArgumentList "play.py" -RedirectStandardOutput logs\play_actual.log -RedirectStandardError logs\play_actual.err.log
# poll http://127.0.0.1:9333/json/list until a *standalone* target appears (~8s), then:
$env:PT_TURNS="3"; $env:PT_TURN_TIMEOUT="150"
Start-Process -FilePath python -ArgumentList "-u","playtest_app.py" -RedirectStandardOutput _pt_out.txt -RedirectStandardError _pt_err.txt
```

Read `_pt_out.txt` for the play-by-play and `logs\play_actual.log` for the
server side. The app **must** be started with that `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`
env var or the harness cannot attach.

Knobs: `PT_TURNS` (default 8), `PT_TURN_TIMEOUT` (default 90, raise to 150 when
image generation is slow), `PT_TAG_INDEX` (which scanned object to pick),
`PT_RELOAD=1` (reload the client first).

PLAY opens the **character screen** before the picker (characters.py,
static/js/characters.js). The harness picks `PT_CHARACTER` there (an id or a
piece of the name; unset, whoever is selected — the one played last), checks
their figure painted, and checks the run it starts IS that character
(`/api/character`). `PT_PLAN` knows a `wear` verb: open the pack (B), put the
first wearable thing on (or take one off), wait for the fitting, and check the
figure changed; the turn after it must have taken the new look. Filed findings
say which of those broke. `_claude_char_run.py` (untracked, the
`_claude_battle_run.py` pattern) runs all of it headless on a server of its
own against a COPY of the roster, so a test never lands on the player's.

## What each tool is for

| Tool | Use it for |
|---|---|
| **`playtest_app.py`** | **The main harness.** Drives the real native app over CDP: start menu, experience picker, SCAN, MOVE TO, INTERACT, PHOTO, ACT, CAMP, encounters. Detects black screens, stalls, console errors, spurious recovery UI. This is the one you want. |
| `playtest.py` | API-level playtest (no client). Forced probes for encounters/death, edge checks for malformed requests and double-submit races, unified check registry, writes a review bundle. Use when you're changing server logic and don't need the UI. |
| `check_js_syntax.py` | **Run after every single `.js` edit.** See below. |
| `check_scene_layers.py` | When the screen is black. Interrogates the real DOM layers and the Reactor video layer. |
| `playtest_boot.py` | Verifies the picture appears *before* the UI chrome on a fresh run (the boot gate). |
| `playtest_capture.py` / `playtest_analyze.py` / `playtest_interactive.py` | Frame capture, run analysis, and manual stepping. |
| `play_log.py` | Structured run logs → `logs/play/*.jsonl`. |

Output from a full run lands in `playtest_results/playtest_<timestamp>/` as
`SUMMARY.md`, `contact_sheet.png`, `frames/`, and `session.json`. Look at the
contact sheet — a lot of bugs are obvious in a strip of frames and invisible in
logs.

## Two rules that will save you a session each

**1. Run `python check_js_syntax.py` after every `.js` edit.** A syntax error in
`standalone.js` kills the client *silently*: the app launches, the menu
renders, and nothing works. There is no error dialog. This has cost two
sessions already.

**2. CDP screenshots do not capture the WebView2 video layer.** A screenshot
can look perfectly healthy while the user's actual screen is black. Never
conclude "it renders fine" from a screenshot alone — use
`check_scene_layers.py`.

## How the UI actually works (this is what your own script would get wrong)

- **There is no `.choice-btn` list during normal play.** Committing an action
  means: click `#scan-btn` → wait for `.scan-tag` elements → click a tag to
  open its action bar → click a sub-action.
- **Sub-action selectors must be scoped to the open tag.** Every tag carries
  its own hidden action bar, so `.scan-action-move` matches many invisible
  buttons. Use `.scan-tag.acting .scan-action-move`.
- **SCAN can be briefly unavailable after a scene change**, until the still
  finishes decoding. Poll for up to ~20s for either tags or `scanReady`; don't
  fail on the first miss.
- **Encounters use `.moment-choice` inside `#moment-choices`**, not
  `.choice-btn`. Different system entirely.
- **Photo mode is multi-step**: click `#realtime-btn`, wait for
  `photo-focusing` to clear, sweep the mouse to find a `#touch-lock` subject,
  click to shoot, tap through `capture-cinema`, then let the game lower the
  camera **itself** — one shot per raise. Clicking `#realtime-btn` again
  re-raises it and looks like a freeze.
- **Input during a Buck transition is queued, not dropped**, but the harness
  still waits for `xp-ready` before sending Enter at the start menu.
- A turn resolves in **~29-30s** on the stills path. Anything that assumes
  faster will report false stalls.

All of the above is already implemented in `playtest_app.py`. That's the point.

## Extending it correctly

Add to the harness; don't fork it.

- **New verb or flow?** Add a branch in the turn loop next to `scan_move` /
  `scan_interact` / `photo` / `encounter` / `act` / `camp`, and add it to the
  `plan` list (`playtest_app.py:~437`).
- **New invariant?** Append to `findings` — that list is the pass/fail. Two
  examples worth copying: the black-frame sampling during resolution, and the
  check that flags a "world hesitated" recovery on a turn that *subsequently
  resolved* (a false alarm is a bug, and the harness used to file those as
  console noise while printing "every turn committed and resolved").
- **Comparing frames?** Use the `continuity()` helper (downscaled mean absolute
  difference, reported as a number). Do **not** reintroduce perceptual hashing
  — aHash was tried, produced constant false positives on this game's subtle
  frame-to-frame changes, and was replaced with exact byte hashing for identity
  plus `continuity()` for similarity. Don't pick a pass/fail threshold on image
  similarity; report the number and look at it.

## What the harness cannot judge

It catches black screens, stalls, dead buttons, console errors, and
non-resolving turns. It cannot tell you whether a close-up is well framed,
whether an encounter feels exciting, or whether the world is cohesive. For
those, read `SUMMARY.md`, look at the contact sheet, and show the user frames.
Don't claim a feature works because the harness went green — the last three
"working" reports were all harness-green with a visibly broken feature.
