# On-device object detection for SCAN

`/api/detect` — the vision call behind the SCAN tool and the PHOTO targeting
brackets — used to be a Gemini request per scan. It is now answered on the box in
about 20 ms by `local_vision.py`, with no network call, no per-scan bill and no
API key. The Gemini path is still there, one environment variable away.

This document records what was measured, why the obvious version of this change
does not work, and what shipped instead.

---

## The investigation

MediaPipe was the starting suggestion, and it is genuinely fast. The question was
never speed. It was whether MediaPipe can see this world at all.

### MediaPipe's object detectors, measured on our own frames

All three of Google's published MediaPipe object-detector models, run over the
lobby gallery stills and the two reference scene renders (single-thread CPU,
median of repeated passes):

| model | size | per frame | what it found across 9 game frames |
|---|---|---|---|
| EfficientDet-Lite0 int8 | 4.6 MB | **16 ms** | `bed`, `tv`, `surfboard`, `person`(hand), `car`, `bench` |
| EfficientDet-Lite0 float32 | 13.8 MB | 20 ms | `bed`, `tv`, `dog`, `bird`, `person`(hand), `car` |
| EfficientDet-Lite2 int8 | 7.5 MB | 39 ms | `bed`, `person`(hand), `car` |
| SSD MobileNetV2 | 11.3 MB | 14 ms | 6 phantom `car`s, `tv`, `bed`, `chair`, `traffic light` |

Fast, and almost entirely wrong. Three findings decided the design:

**1. The vocabulary does not overlap the game.** These detectors are trained on
COCO's 80 everyday classes. This world is built out of silos, gas pumps,
chain-link fences, corridors, water towers, floodlights and figures in fog — and
COCO contains none of those words. There is no threshold or preprocessing that
fixes a missing class.

**2. On dark, VHS-degraded frames the confident answers are hallucinations.** A
lone figure under a sodium lamp comes back `tv: 0.42`. An abandoned filling
station comes back as six overlapping `car`s. A treeline comes back `dog`.
Gamma-lifting and CLAHE contrast normalization were tried on the theory that
darkness was the problem; they moved individual scores around but changed nothing
structural — `gal_gasstation.jpg` returns nothing at any exposure, because a gas
pump is not a class.

**3. The one high-confidence true positive is the thing we must never tag.** On
`scene_exterior.png`, the cleanest in-game render, the strongest detection in the
frame is `person: 0.84` — the player's own hand holding the flashlight. A hand
gripping a torch is a textbook COCO person. Shipped naively, SCAN would offer the
player a *conversation with their own hand*.

Open-vocabulary local detectors (OWLv2, YOLO-World) would solve the vocabulary
problem and were ruled out on deployment grounds: they need PyTorch, which is
larger than this service's entire disk allowance, or a 100 MB+ model download in
the browser.

So a bare MediaPipe swap was never an option. It would have quietly gutted SCAN
while looking like a 50× speedup.

---

## What shipped

The insight that makes this tractable is that **we already know what is in the
frame, because we wrote it.** Every scene is rendered from a prompt the engine
composed, and it is already on the session state as `current_image_prompt`. In a
generated world, "what is out there" is not something to infer from pixels. Only
*where it is on screen* needs looking at.

So the two halves are given the jobs they are each good at:

| | supplies | why it is the right source |
|---|---|---|
| **MediaPipe** | boxes + a coarse category | measures real pixel positions; excellent on people, vehicles and animals |
| **scene prompt** | the open vocabulary + spatial hints | knows "armored personnel carrier", which no COCO model ever will |

And they check each other. The pipeline in `local_vision.detect()`:

1. **Detect.** EfficientDet-Lite0 int8 over the frame, downscaled to 640 px.
2. **Reject what cannot be here.** COCO classes that do not exist in a 1993
   rural-industrial world (surfboards, giraffes, broccoli) are dropped outright.
   Sub-2% pinpoint boxes are dropped as tape noise.
3. **Reject the camera operator.** A box clipped by the bottom edge with its mass
   below the midline is the player's own hand, arm, gear or car hood. This is the
   guard for finding #3 above; `engine`'s existing backstop does not catch it,
   because that one requires a tall narrow column and the hand is wider than it
   is tall.
4. **Relabel from the prompt.** A measured box borrows the prompt's specific
   wording when the two are compatible, so COCO's `car` is presented as
   "abandoned armored personnel carrier" *at the position MediaPipe measured*.
5. **Corroborate.** Classes we trust outright (people, vehicles, animals — the
   ones SCAN's TALK affordance hangs off) become tags on the detector's word.
   Everything else (furniture, appliances, props) only survives if the prompt
   independently names something compatible. This is what kills the television in
   the middle of a forest.
6. **Anchor the rest.** Prompt nouns the detector is structurally blind to — the
   silo, the fence, the doorway — are placed on the most salient region of the
   frame matching their spatial hint ("to your left", "in the distance") and
   their category's habitual place in a shot.

Step 6 is the one inferential step, and it is deliberately barred from the case
where guessing is harmful: **people and creatures are never anchored.** For a
silo, "the detector found nothing" carries no information and the prompt is the
only witness, so placing it beats showing the player an empty screen. For a
person the reverse holds — people are the class COCO is best at, so silence
really is evidence of absence — and the cost of being wrong is asymmetric,
because a person tag sets `speaks` and offers a conversation with a patch of
empty gravel. Those require pixels or they do not appear.

### One filter, not two

Both backends emit the *same* intermediate shape Gemini's structured response
already parsed into: `{"label", "box_2d": [ymin, xmin, ymax, xmax] on a 0-1000
grid, "kind"}`. So every rule that decides what a player is allowed to see —
the underwhelming-label filter, the operator's-body geometry backstop, degenerate
box rejection, dedupe, the `speaks`/`kind` classifier, the item cap — lives once
in `engine._normalize_detections()` and applies whoever did the looking. The
client's wire contract is unchanged, so SCAN, PHOTO targeting, the objectives
system and the TALK snapshot all kept working without a client edit.

### Results on the frame from finding #3

The same `scene_exterior.png` that previously yielded "the player's hand, at high
confidence" now returns:

```
pixels  abandoned armored personnel carrier   (measured, on the vehicle)
prompt  fence
prompt  processing plant
prompt  rusted silos
prompt  loading dock
prompt  gate
prompt  floodlight
prompt  warning sign
```

No hand, nothing talkable, and the vehicle is boxed where it actually is.
Downstream, the objectives system turns these into real bounties ("Capture the
abandoned armored personnel carrier", "Photograph the rusted silos").

---

## Configuration

> **Not on by default.** `DETECT_BACKEND` ships as `gemini`. On Render, inference
> that takes ~16 ms locally was observed to **hang indefinitely** — and since
> `/api/detect` is polled roughly every 2.5 s by photo targeting against a worker
> with four threads, hung calls don't degrade SCAN, they take the whole service
> down. Until that is understood, local is opt-in via `DETECT_BACKEND=local`.
> It is solid locally, and it remains the only backend that works with no API key
> at all. A 4 s deadline plus a two-strike breaker (below) means an opt-in can no
> longer wedge a worker even if the hang recurs.

| variable | default | meaning |
|---|---|---|
| `DETECT_BACKEND` | `gemini` | `local`, `gemini`, or `auto` (local when it can run, Gemini otherwise) |
| `DETECT_LOCAL_TIMEOUT_S` | `4.0` | hard deadline on one inference; the stuck thread is abandoned, the request answers |
| `DETECT_LOCAL_MAX_TIMEOUTS` | `2` | timeouts before local is switched off for the process |
| `DETECT_MODEL_PATH` | `models/efficientdet_lite0_int8.tflite` | swap in another `.tflite` |
| `DETECT_LOCAL_MIN_SCORE` | `0.22` | MediaPipe confidence floor |
| `DETECT_LOCAL_ANCHOR` | `1` | `0` emits only pixel-measured tags, skipping step 6 |

`DETECT_BACKEND=gemini` restores the previous behaviour exactly, without a
deploy. `/api/health` reports which backend is live and whether the model loaded,
so a missing `.tflite` cannot degrade SCAN silently.

The model is vendored in `models/` rather than fetched at boot: the production
filesystem is ephemeral apart from the session mount, so a download-on-first-use
would re-download on every deploy.

### Widening the vocabulary

`local_vision._LEXICON` is a categorised set of nouns this world is built from,
matched with optional adjective modifiers so "rusted corrugated silo" survives as
one specific tag. Adding a word is a one-line edit. This mirrors how the engine
already handles `_UNDERWHELMING_LABELS` and `_SPEAKER_LABEL_RE`: inspectable
vocabularies rather than opaque weights.

Category also drives a noun's tag priority, its `kind`, and where it gets
anchored vertically — sky things high, terrain low, structures across the middle.

---

## Known limitations

- **Anchored positions are inferred, not measured.** A "chain-link fence" tag
  lands on a salient region consistent with the prompt's spatial hint, which is
  usually the fence and sometimes merely near it. `DETECT_LOCAL_ANCHOR=0` trades
  most of SCAN's tags for pixel-exact ones.
- **The vocabulary is fixed.** Gemini could name something the lexicon has never
  heard of. If the world's setting changes substantially, the lexicon needs
  widening — that is the price of not paying per scan.
- **The prompt and the frame must agree.** They do by construction (the frame was
  rendered *from* that prompt), but a frame captured long after a scene change
  can be scanned against a stale prompt. Tags are already invalidated on scene
  swap.
- **Detection is serialized.** MediaPipe's detector is not thread-safe, so one
  process-wide instance sits behind a lock. At 16 ms per call and a ~2.5 s scan
  cadence, threads effectively never queue.
- **It hangs on Render, cause not yet identified.** The model loads
  (`/api/health` reports `available: true`) and then `detect()` never returns,
  while the same code and model answer in ~16 ms locally and survive 96
  concurrent calls across 8 threads. Memory is a candidate — the backend adds
  ~130 MB RSS (72 MB → 202 MB at boot) on a single-worker box — as is CPU quota
  starving TFLite's XNNPACK thread pool. This is why the default is `gemini`.

## Why server-side and not in the browser

MediaPipe's Web/JS build was the obvious reading of "local", and it would remove
the client→server round trip. It was not chosen, for two measured reasons:

- **Download cost.** `vision_wasm_internal.wasm` is 3.0 MB over the wire
  (11.5 MB decompressed) plus the 4.4 MB model — roughly **7.6 MB** before a
  player's first scan, against 7.6 MB for the game's entire current static
  payload.
- **Two implementations of one judgement.** The labels come from the scene
  prompt, so a browser detector needs the lexicon, the priority ordering, the
  corroboration rule and the anchoring heuristics in JavaScript as well as
  Python — and `_talk_vision_snapshot()` needs the Python path regardless,
  because it scans a scene render off disk with no browser involved. Two copies
  of these rules would drift.

The server path already removes the 1–3 s model latency, the bill and the key
dependency. If SCAN later wants continuous per-video-frame tracking rather than a
tap-driven pass, the browser build becomes worth its cost, and the fusion rules
should move behind a single shared description of the vocabulary at that point.

---

## Tests

```bash
python3 -m unittest test_local_vision -v          # fusion rules, stubbed detector
python3 -m unittest test_detect_filter -v          # shared label filter, both backends
python3 -m unittest test_local_vision_e2e -v       # real browser, real model, no API key
```

`test_local_vision.py` stubs MediaPipe deliberately: the assertions are about our
rules — which classes may exist here, when a class needs corroboration, that the
operator's hand never becomes a talkable figure — and should hold whatever the
weights say on a given frame. A handful of tests at the end do run the real model,
and skip when it is not installed.

`test_local_vision_e2e.py` mocks nothing. A real browser captures a real frame,
posts it to a real `/api/detect` with every API key blanked, and asserts that
story-grounded tags appear in the DOM — the path that did not exist before, since
`/api/detect` previously returned `[]` with no `GEMINI_API_KEY`.
