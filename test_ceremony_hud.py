#!/usr/bin/env python3
"""The turn pipeline is one top-right circle, not a bottom action bar."""

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent


class TheCeremonyIsACornerCircle(unittest.TestCase):
    def setUp(self):
        self.css = (ROOT / "static" / "css" / "standalone.css").read_text(
            encoding="utf-8", errors="replace")
        self.js = (ROOT / "static" / "js" / "standalone.js").read_text(
            encoding="utf-8", errors="replace")

    def test_the_hud_sits_top_right_not_across_the_frame(self):
        # Anchored to the start of a line so this finds the BASE rule. Plain
        # "#processing-veil {" first matches a descendant selector that ends in
        # the same token (body.moment-cutscene #processing-veil), and this read
        # the three lines of that override instead — failing on a stylesheet
        # that was perfectly correct.
        veil = self.css.split("\n#processing-veil {", 1)[1][:500]
        self.assertIn("inset: auto", veil)
        self.assertIn("right:", veil)
        self.assertNotIn("align-items: flex-end", veil)

    def test_only_the_live_phase_is_drawn(self):
        self.assertIn(".cere-step.active,", self.css)
        step = self.css.split(".cere-step {", 1)[1][:400]
        self.assertIn("display: none", step)

    def test_the_ring_spins_like_a_loader(self):
        self.assertIn("@keyframes cere-spin", self.css)
        self.assertIn("cere-orb", self.css)

    def test_six_phases_still_exist_they_just_replace_each_other(self):
        steps = self.js.split("const STEPS = [", 1)[1].split("];", 1)[0]
        for key in ("action", "consequence", "world_update",
                    "world_respond", "actions", "guide_image"):
            self.assertIn('key: "' + key + '"', steps)
        self.assertIn("hud:", steps)

    def test_the_image_step_sits_where_the_wait_actually_is(self):
        """The render is ~67% of a turn; the consequence is ~13%. With
        guide_image last, nothing advanced the circle between the consequence
        prose and the finished still, so it sat lit on CONSEQUENCE for a median
        8.8s (p90 21.9s) and the consequence took the blame for the picture.
        The image step has to come BEFORE world_respond/actions, which only
        fire off the scene_image beat the render produces."""
        steps = self.js.split("const STEPS = [", 1)[1].split("];", 1)[0]
        order = [k for k in ("action", "consequence", "world_update",
                             "guide_image", "world_respond", "actions")]
        found = sorted(order, key=lambda k: steps.index('key: "' + k + '"'))
        self.assertEqual(found, order, f"ceremony steps out of pipeline order: {found}")

    def test_the_render_wait_spins_with_an_elapsed_readout(self):
        """A slow render has to read as live progress, not a frozen app."""
        self.assertIn("function enterGuideImageWait", self.js)
        self.assertIn("Rendering the guide image", self.js)
        # Landing on the image step with no still yet parks there.
        pump = self.js.split("function pump() {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("cur === IMG_STEP", pump)
        self.assertIn("enterGuideImageWait()", pump)

    def test_stills_mode_walks_to_the_image_step_when_the_consequence_lands(self):
        """The server renders the picture immediately after the consequence, so
        the client advances itself rather than waiting for a beat that cannot
        arrive until the render is already done. Realtime keeps its own beats."""
        self.assertIn('if (Renderer.mode !== "reactor") Ceremony.reach("guide_image");',
                      self.js)


class TheCircleKeepsTurningInsideAMoment(unittest.TestCase):
    """"Having it gone makes me think the app isn't responding."

    A fight hid the loader, and a fight is a full image generation — so the
    player watched a still picture for ~30 seconds with nothing anywhere
    saying the machine was alive. A cutscene still hides it: that one is
    playing, and the wait IS the content.
    """

    def setUp(self):
        self.css = (ROOT / "static" / "css" / "standalone.css").read_text(
            encoding="utf-8", errors="replace")
        self.js = (ROOT / "static" / "js" / "standalone.js").read_text(
            encoding="utf-8", errors="replace")

    def test_a_fight_no_longer_hides_the_loader(self):
        self.assertNotIn("body.moment-encounter #processing-veil", self.css)
        self.assertNotIn("body.moment-encounter #ceremony", self.css)
        # The stale turn UI is still hidden — its slate is not the offer.
        self.assertIn("body.moment-encounter #choices-container", self.css)

    def test_a_cutscene_still_hides_it(self):
        self.assertIn("body.moment-cutscene #processing-veil", self.css)

    def test_it_is_lifted_over_the_moment_overlay(self):
        """Un-hiding alone is not visibility: the overlay is z-index 33 and the
        loader 20, so it painted underneath the letterbox."""
        rule = self.css.split("body.moment-active #processing-veil {", 1)
        self.assertEqual(len(rule), 2, "no z-index lift for the loader")
        z = int(rule[1].split("z-index:", 1)[1].split(";", 1)[0].strip())
        self.assertGreater(z, 33, "the loader is still under #moment-overlay")

    def test_the_grain_and_the_post_come_up_too(self):
        """"All the grain / post process effects disappear during encounters."

        They were never switched off — the whole stack lives between z-index 2
        and 20 and the Moment overlay is 33, so a fight was the one place in
        the run with no film look at all.
        """
        for sel in ("#vhs-overlay", ".danger-vignette", ".danger-chroma",
                    "#scene-glitch"):
            with self.subTest(layer=sel):
                rule = self.css.split(f"body.moment-active {sel}", 1)
                self.assertEqual(len(rule), 2, f"{sel} is left under the Moment")

    def test_the_loader_still_sits_on_top_of_the_grain(self):
        def z_for(sel):
            tail = self.css.split(f"body.moment-active {sel}", 1)[1]
            return int(tail.split("z-index:", 1)[1].split(";", 1)[0].strip())
        self.assertGreater(z_for("#processing-veil"), z_for("#vhs-overlay"))
        self.assertGreater(z_for("#scene-glitch"), z_for(".danger-chroma"))

    def test_nothing_is_lifted_over_the_flare_or_the_capture(self):
        """Those two are supposed to own the screen when they run."""
        for sel in ("#vhs-overlay", ".danger-vignette", ".danger-chroma",
                    "#scene-glitch", "#processing-veil"):
            with self.subTest(layer=sel):
                tail = self.css.split(f"body.moment-active {sel}", 1)[1]
                z = int(tail.split("z-index:", 1)[1].split(";", 1)[0].strip())
                self.assertLess(z, 50, f"{sel} would cover the encounter flare")

    def test_the_encounter_reports_both_of_its_waits(self):
        enc = self.js.split("const Encounter = (function ()", 1)[1]
        # The opening plate (the longest dead wait) and each round's resolve.
        self.assertEqual(enc.count("Ceremony.begin({ passive: true })"), 2)
        self.assertIn("Ceremony.settle()", enc)

    def test_it_borrows_the_picture_without_taking_the_turn_gate(self):
        """passive must not claim state.processing: the fight owns its own
        input gate, and pick() refuses a round while processing is set — so a
        gating ceremony would eat the player's next lane."""
        begin = self.js.split("      begin(opts) {", 1)[1].split("\n      },", 1)[0]
        self.assertIn("passive = !!(opts && opts.passive)", begin)
        gated = begin.split("if (!passive) {", 1)
        self.assertEqual(len(gated), 2, "begin() does not guard the turn gate")
        self.assertIn("state.processing = true", gated[1])
        # ...and nothing sets it before that guard.
        self.assertNotIn("state.processing = true", gated[0])

    def test_a_failed_round_cannot_leave_it_spinning(self):
        fail = self.js.split("function failResolve(err) {", 1)[1][:400]
        self.assertIn("Ceremony.abort()", fail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
