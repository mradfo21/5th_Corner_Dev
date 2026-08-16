# THE ORGANISM — the World Editor as a recursive graph

## The problem

The editor is a phone-hostile stack of everything at once: a five-tab strip that
overflows its own row, a layer header, a gallery, a form, a column of 15,000-
character prompt cards, a controls strip and a three-button footer — all in one
100vw column. Every level of the hierarchy is flattened onto the same plane, so
the only way to understand the shape of the thing you're editing is to scroll
past all of it. It reads as a settings dump, not as a game.

But the data underneath is already a clean tree: one game, four layers, and
inside each layer a handful of objects (spec sheets, prompts, saved levels,
runtime controls). The fix is to stop flattening it.

## The idea

One circle: **THE GAME**. Double-tap it and it becomes the screen — you are now
*inside* the cell, and its children are the circles arranged inside it. Double-
tap one of those and the same thing happens, recursively, as deep as the tree
goes. The parent's membrane stays visible as a thin halo around the edge of the
screen; tapping that halo (the periphery) surfaces you back up one level.

At the vertices — the leaves, where there is nothing left to dive into — a tap
animates a window up from the bottom with that object's editable options.

Navigation *is* the diagram. Zoom depth *is* hierarchy depth. Nothing is
labelled "tab", "panel" or "section", because the geometry says it.

## The tree (derived from live state, never duplicated)

```
◉  THE GAME                              root, neutral accent
├── ⚙  ENGINE          layer   #7aa2ff   How does a turn compute?
│   ├── ▤  SYSTEM      group             the 9 runtime controls
│   │   ├── ◉ Live renderer      control (tap = toggle, in place)
│   │   ├── ◍ World model        control
│   │   └── … 7 more
│   ├── ≡  How Actions Play Out  prompt  (tap = edit window)
│   ├── ≡  What You Can Do       prompt
│   └── ▸  ADVANCED    group             the 7 mechanical rulebooks
├── ◈  GAME            layer   #f0b354   What kind of game is this?
│   ├── ▦  The Game     spec            (tap = the form, in a window)
│   ├── ▦  Camera       spec
│   └── ≡  Art Direction / Negative Prompt   prompt ×2
├── ◱  LEVEL           layer   #a78bfa   Where am I, what's here?
│   ├── ▤  LEVELS      group             saved levels + "save this level"
│   ├── ▦  The Level    spec
│   └── ≡  Opening State prompt
├── ●  CHARACTER       layer   #4ec9a5   Who am I?
│   └── ▦  Character Sheet  spec
└── ▣  BUILDS          group             saved builds + "save a build"
```

Layers, prompts, tiers, spec blocks, levels and builds all come straight from
`/api/admin/studio/content` (already loaded by `WorldEditor`), so the graph can
never drift from the list view — they are two renderings of one state. The nine
runtime controls are the same live `<button>` elements the rail used to hold
(`#we-sys-src`), so their handlers and state classes keep working untouched.

## Geometry (deterministic, no dependencies, no physics)

Circle packing, but *symmetric* rather than greedy — the point is that it looks
composed, not simulated:

- **n = 1** → one child at the centre, `r = 0.52R`
- **n ≤ 6** → one ring: `r = sin(π/n) / (1 + sin(π/n)) · 0.92`, centres at
  `d = R − 1.12r`, first child at 12 o'clock
- **7 ≤ n ≤ 12** → nucleus at the centre + a ring of `n − 1`
- **n > 12** → two concentric rings, split `⌊0.38n⌋` inside

World space is absolute and computed once per data change: the root is
`(0, 0, r = 1000)`, and a child's absolute frame is
`centre = parent.centre + (x, y)·parent.r`, `radius = r·parent.r`. Depth costs
one order of magnitude of scale per level, which is exactly what makes the zoom
read as *entering* something.

Every container also draws **tethers** — hairlines from its nucleus to each
child — so the picture is honestly a graph (nodes + edges), not just nested
bubbles, plus faint concentric guide rings and radial spokes for the
"mathematical" register.

## Zoom

A single `viewBox` animation on one `<svg>`. The focused cell spans 87% of the
view's shorter axis, and that remaining 13% is the whole trick behind going
back up: it's the parent's interior, visible as a halo of membrane (and the
crowded edges of your sibling cells) around the current one. Tap it → focus
moves to the parent. The viewBox matches the canvas's aspect exactly, so on a
phone the long axis fills with the parent rather than with letterboxed nothing.
The root is the exception — it has no parent to reveal, so it opens edge to
edge. Easing is a 620 ms cubic, with the scale interpolated in log space or the
descent lurches at the end; `prefers-reduced-motion` cuts it to a snap.

Because everything is in world units, apparent text size is kept constant by
sizing labels relative to each node's own radius (`≈0.15r`): a child of the
focus is always about a third of the view, so its label always lands at about
the same number of screen pixels. The focus node's own name goes to the HUD
breadcrumb instead of being drawn at absurd size, and grandchildren render as
unlabelled organelles — detail you can see but not read, which is what tells you
there's something in there.

Culling: nodes whose projected radius is under ~1.5 px, or fully outside the
view, are dropped from the frame.

## Interaction

| Gesture | On a container | On a vertex (leaf) |
| --- | --- | --- |
| tap | select — HUD shows what it is + "double-tap to go inside" | **opens its window** |
| tap again (any delay) | zoom in | — |
| double-tap | zoom in | opens its window |
| tap the halo / periphery | up one level | up one level |
| `Esc` | up one level (closes the editor at the root) | closes the window |

Double-tap is the gesture, but a *slow* second tap on a selected cell enters it
too: a control that only responds under 340 ms reads as broken.

Breadcrumbs in the HUD are tappable for a direct jump to any ancestor. A
control vertex is the exception that proves the rule: it has nothing to
configure, so tapping it just *is* the toggle — the circle lights up. With a
mouse, the cursor names the gesture before you commit to it (`zoom-in` over a
container, `pointer` over a vertex, `zoom-out` over the periphery).

## The window

A bottom sheet (`#eg-sheet`) that animates up over the graph, dimming it.
Header: glyph, name, one line of what it does, close. Body by vertex kind:

- **prompt** → description, a live textarea bound to the same `edits` buffer the
  list view uses, char count, `Reset to default`, and `Full editor` handing the
  same key to the existing pop-out editor (line numbers + diff, untouched)
- **spec** → the real cast form for that block, mounted by the existing
  `renderCast`, saving on blur exactly as before
- **level / build** → its metadata plus `Open` / `Delete`
- **new level / new build** → name field + save
- **control** → never opens a sheet; the tap is the toggle

Global actions (`Revert`, `Apply Live`, `Save & Restart`), the unsaved badge, the
`CONTROLS` strip and the header stay exactly where they are — they apply to the
whole organism, not to whatever cell you happen to be inside.

## Scope and safety

- The list view is not deleted. `#we-view` in the header flips between `GRAPH`
  and `LIST`, remembered per browser; graph is the default. Everything the list
  can do, it still does.
- No new endpoints, no new state. `editor_graph.js` reads and writes through a
  thin bridge on `WorldEditor` (`window.__WorldEditor.bridge`), so there is one
  source of truth and one save path.
- In graph mode `render()` skips building the list DOM (dozens of textareas) and
  just notifies the graph — so the heavy column isn't sitting behind the canvas.
