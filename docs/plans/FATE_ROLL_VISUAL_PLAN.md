# The Tracking Roll — making fate visible

## The opportunity

Two facts about the game today, which together suggest one feature.

**The dice are already being rolled, and the player never sees them.** Every
turn the server decides:

```
[TURN DYNAMICS] source=encounter phase=critical threat=6 fate=UNLUCKY escalated=False
[TURN DYNAMICS] source=encounter phase=critical threat=8 fate=UNLUCKY escalated=False
```

`fate` swings the outcome, `threat` is a real escalating number, `phase` and
`escalated` gate how hard the world pushes. The client is told none of it —
`fate` does not appear anywhere in `standalone.js`. So outcomes arrive with no
visible causality, which is why a death reads as arbitrary rather than earned.
Two UNLUCKY rolls at threat 6 and 8 killed a run, and the player had no way to
know either number existed.

**Every turn has a ~30 second wait while the frame generates.** Right now that
is dead time behind a progress bar — the single worst part of the feel, and the
thing the watchdog bug was born out of.

The proposal is to spend one on the other. **Put the roll in the wait.** The
latency stops being a loading screen and becomes the pause between the die
leaving your hand and landing — which in D&D is the most charged moment at the
table.

## The visual: the tracking bar is the die

Not a literal d20. This world is a 1993 VHS recording of an industrial site,
and the native object for "the tape is deciding what it shows you" is the
**tracking bar** — that horizontal band of noise that rolls up a worn VHS image
while the heads hunt for a lock.

The beat, on commit:

1. **CAST.** The moment the action commits, a tracking band tears across the
   frame and the picture drops into rolling analogue noise. The action you chose
   prints in the corner in the tape's own OSD font — `INTERACT · LIGHT FIXTURE`.
   Sound is the head-hunt whine, not a dice rattle.
2. **HUNT.** The band sweeps while the turn resolves — this is the 30 seconds.
   Under it, two OSD readouts you can actually read: `THREAT 06` and a modifier
   line naming what is pushing on this roll (`DELIBERATE MEDDLING +2` for a scan
   interaction, `EXPOSED`, `BLEEDING`). The player learns the system by watching
   it rather than being told.
3. **LOCK.** The band snaps to centre and the noise resolves into the new frame.
   `fate` colours the snap, and this is the whole payoff:
   - **LUCKY** — clean instant lock, the band vanishes, a bright chime.
   - **NORMAL** — the band settles with a little residual roll.
   - **UNLUCKY** — the lock fails once, the frame rolls over, *then* catches.
     Chroma smears red. You feel the miss before you read a word of prose.
4. **CONSEQUENCE.** `THREAT 06 → 08` ticks up on the OSD, digit by digit, and
   only then does the prose land.

An escalation (`escalated=True`) gets the strongest version: the tape appears to
*eat* the frame — full dropout, vertical roll, and the threat number counts up
past where it should sit.

## Why this shape

- **It is diegetic.** A d20 floating over a VHS still breaks the fiction; a
  tracking failure *is* the fiction. The game already sells itself as found
  footage, so the interface admitting it is a tape deepens the frame rather than
  puncturing it.
- **It teaches the model it already runs.** Players currently cannot perceive
  threat or fate, so they cannot play around them. Showing `THREAT 06` climbing
  makes retreat, camping, and photographing legible as *choices about a number*.
- **It makes the wait an asset.** The longest-standing complaint (slow turns)
  becomes suspense. Anticipation is the only kind of waiting people enjoy.
- **It makes death fair.** A run that ends on two UNLUCKY rolls at threat 8
  reads as a tragedy the player watched happen, not a bug.

## Wiring

The visual is the easy half; the data is the work.

1. **Expose the roll.** `[TURN DYNAMICS]` is a log line today. Emit the same
   fields (`fate`, `threat`, `phase`, `escalated`, plus the modifier reasons) as
   a feed item at the *start* of turn processing, so the client can begin the
   cast immediately rather than learning the roll when the turn is already over.
   This ordering is the crux: the reveal has to precede the frame.
2. **Name the modifiers.** The escalation inputs exist (`risk_boost = 2 if
   is_interaction`, `source`, fate bias) but are anonymous. Each needs a short
   display string, since "+2 DELIBERATE MEDDLING" is the line that makes the
   system feel like rules rather than mood.
3. **Own the wait.** The tracking overlay replaces the current progress
   ceremony for a committed turn, and must be driven by the same resolution
   signal the turn watchdog watches (`state.lastId` liveness), so a slow turn
   extends the hunt instead of pretending to finish.
4. **Respect the Moments.** Encounters already have their own ceremony; the
   roll belongs on encounter *choices* too, where threat is highest and the
   swing matters most — but it must not fight the Moment's own cut.

## Risks

- **It cannot lie.** If the OSD says `THREAT 06` the prose must not read as
  safe. Surfacing the numbers means the narrative and the simulation have to
  agree, and this run already showed them diverging (custom actions vs plates).
  Expect the roll to expose incoherence that is currently invisible.
- **Repetition.** A 30-second ceremony that is identical every turn becomes
  wallpaper by turn 10. The three fate variants plus escalation give four
  textures; that is probably the minimum, and LUCKY should be *fast* so the
  ceremony's length itself carries information.
- **It must be skippable.** A held key should snap to lock. Ceremony that
  cannot be dismissed turns into a tax on replay.
