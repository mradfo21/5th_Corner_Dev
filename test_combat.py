"""The fight's rules — a d20, two stat blocks, two health bars (combat.py).

    python -m unittest test_combat -v

"make it a proper combat system that has an element of skill / dice roll.
like dungeons and dragons" (Matt, 2026-09-23). The two faults that asked for
it are pinned first — no round is ever decided by decree (the old last
exchange read 100%), and no attack ends without its die (the old `escape`
band ended 13–16% of them with no roll at all). The rest are the honesty
invariants the screen depends on:

  * a face that succeeds is a face that succeeds: d20 + mod vs the target,
    a natural 20 always, a natural 1 never;
  * the % on the slate is the exact chance of the roll it names, and a
    thousand rolls land on it;
  * the bars move only for damage that was rolled, and only a failed death
    save empties yours;
  * the round plays in an order that could have happened;
  * the dice are thrown ONCE (/api/encounter/exchange), and the resolve
    draws exactly that.
"""
import contextlib
import copy
import random
import unittest
from pathlib import Path
from unittest import mock

import combat
import encounter
import engine

ROOT = Path(__file__).resolve().parent


class _Rng:
    """Hands out given draws in order (each draw d is a d20 face int(d*20)+1)."""

    def __init__(self, *vals):
        self.vals = list(vals)

    def random(self):
        return self.vals.pop(0) if self.vals else 0.5


def face(n, sides=20):
    """The draw that rolls face `n` on a die of `sides` (a d20 by default)."""
    return (n - 0.5) / float(sides)


def _enc(**over):
    enc = {"character": {"label": "An investigative freelancer", "kind": "person",
                         "stance": "hostile", "move": "hidden blade"},
           "enemy_state": "ready", "round_no": 1, "detection": 1}
    enc.update(over)
    return enc


def _state(**over):
    st = {"player_state": {"alive": True, "condition": "ok", "hp": 30, "hp_max": 30}}
    st.update(over)
    return st


def _record(first="you", surprise=None, **foe_over):
    rec = combat.open_combat({"label": "An investigative freelancer", "kind": "person",
                              "stance": "hostile", "move": "hidden blade"},
                             you_name="Isaac", you_hp=30, you_max=30, detection=1,
                             rng=random.Random(0))
    rec["init"] = {"first": first, "surprise": surprise, "you": 15, "foe": 9}
    rec["foe"].update(foe_over)
    return rec


def _you(**over):
    y = combat.player_block(name="Isaac")
    y.update(over)
    return y


class TheTwoFaultsAreGone(unittest.TestCase):

    def test_no_round_after_the_first_is_a_sure_thing(self):
        """The old last exchange was decided by decree and read 100%."""
        rng = random.Random(4)
        for _ in range(3000):
            rec = _record(first=rng.choice(["you", "foe"]))
            rec["turns"] = rng.randint(1, 6)
            rec["talk_tries"] = rng.randint(0, 6)
            rec["foe"]["hp"] = rng.randint(1, rec["foe"]["max"])
            you = _you(luck=rng.choice([-2, 0, 2]))
            for lane in encounter.ENCOUNTER_LANES:
                self.assertLess(combat.lane_odds(rec, you, lane), 100)
                self.assertGreater(combat.lane_odds(rec, you, lane), 0)

    def test_an_attack_always_gets_its_die(self):
        """The old `escape` band ended a committed attack with no roll of the
        player's at all. Now every attack round has the player's attack roll,
        unless they were killed before their turn came."""
        rng = random.Random(9)
        for _ in range(4000):
            rec = _record(first=rng.choice(["you", "foe"]))
            out = combat.play_round(rec, lane="confront", verb="Hit him", you=_you(),
                                    you_hp=rng.randint(1, 30), rng=rng)
            mine = [b for b in out["exchange"]["beats"] if b.get("kind") == "attack"]
            if out["end"] == "dead" and not mine:
                continue
            self.assertEqual(len(mine), 1, out["exchange"]["beats"])
            self.assertIn("roll", mine[0])

    def test_nothing_ends_a_fight_but_the_rules(self):
        rng = random.Random(2)
        for _ in range(3000):
            rec = _record(first=rng.choice(["you", "foe"]))
            lane = rng.choice(encounter.ENCOUNTER_LANES)
            out = combat.play_round(rec, lane=lane, verb="x", you=_you(), you_hp=30, rng=rng)
            end = out["end"]
            beats = out["exchange"]["beats"]
            if end == "escaped":
                self.assertEqual(lane, "evade")
            if end == "settled":
                self.assertEqual(lane, "parley")
            if end == "ko":
                self.assertEqual(out["exchange"]["foe"]["hp"], 0)
            if end == "routed":
                self.assertTrue(any(b.get("kind") == "morale" for b in beats))


class TheDice(unittest.TestCase):

    def test_a_face_succeeds_by_the_rules(self):
        rng = random.Random(11)
        for _ in range(5000):
            mod, target = rng.randint(-3, 9), rng.randint(5, 25)
            r = combat.roll_d20(rng, mod, target)
            want = r["face"] == 20 or (r["face"] != 1 and r["face"] + mod >= target)
            self.assertEqual(r["ok"], want)
            self.assertEqual(r["ok"], r["face"] >= r["need"], r)
            self.assertEqual(r["total"], r["face"] + mod)

    def test_advantage_keeps_the_higher(self):
        r = combat.roll_d20(_Rng(face(4), face(17)), 5, 12, adv=1)
        self.assertEqual((r["faces"], r["face"]), ([4, 17], 17))
        r = combat.roll_d20(_Rng(face(4), face(17)), 5, 12, adv=-1)
        self.assertEqual(r["face"], 4)

    def test_the_chance_is_exact(self):
        for mod in range(-3, 10):
            for target in range(4, 27):
                p = sum(f == 20 or (f != 1 and f + mod >= target) for f in range(1, 21)) / 20
                self.assertAlmostEqual(combat.chance(mod, target), p)
                self.assertAlmostEqual(combat.chance(mod, target, 1), 1 - (1 - p) ** 2)
                self.assertAlmostEqual(combat.chance(mod, target, -1), p * p)

    def test_damage_is_its_dice(self):
        d = combat.roll_damage(_Rng(face(3, 8), face(6, 8)), (1, 8, 3))
        self.assertEqual((d["faces"], d["total"], d["spec"]), ([3], 6, "1d8+3"))
        d = combat.roll_damage(_Rng(face(3, 8), face(6, 8)), (1, 8, 3), crit=True)
        self.assertEqual((d["faces"], d["total"], d["spec"]), ([3, 6], 12, "2d8+3"))


class TheSlateSaysTheOddsTheDieIsThrownAt(unittest.TestCase):
    """A thousand rolls of each lane land on the % the slate printed."""

    def _rate(self, rec, you, lane, n=4000, seed=3):
        rng = random.Random(seed)
        hit = 0
        kind = {"confront": "attack", "evade": "flee", "parley": "reason"}[lane]
        for _ in range(n):
            out = combat.play_round(copy.deepcopy(rec), lane=lane, verb="x", you=you,
                                    you_hp=30, rng=rng)
            mine = [b for b in out["exchange"]["beats"] if b.get("kind") == kind]
            hit += bool(mine and mine[0]["roll"]["ok"])
        return 100.0 * hit / n

    def test_each_lane(self):
        cases = [
            ("plain", _record(), _you()),
            ("surprise", _record(first="you", surprise="you"), _you()),
            ("lucky", _record(), _you(luck=2)),
            ("armed", _record(), combat.player_block(name="Isaac", weapon="Baton")),
            ("bloodied, tried twice", dict(_record(), talk_tries=2), _you()),
        ]
        cases[-1][1]["foe"]["hp"] = 3
        for name, rec, you in cases:
            for lane in encounter.ENCOUNTER_LANES:
                with self.subTest(case=name, lane=lane):
                    shown = combat.lane_odds(rec, you, lane)
                    self.assertAlmostEqual(self._rate(rec, you, lane), shown, delta=2.5)

    def test_the_slate_reads_the_fight(self):
        st, enc = _state(), _enc()
        enc["combat"] = encounter.open_fight_record(copy.deepcopy(st), enc)
        odds = encounter.slate_odds(st, enc)
        you = encounter.fight_context(st, enc)["you"]
        rec = combat.clean_combat(enc["combat"])
        for lane in encounter.ENCOUNTER_LANES:
            self.assertEqual(odds[lane], combat.lane_odds(rec, you, lane))

    def test_the_odds_ride_on_the_slate(self):
        slate = [{"text": "Hit", "lane": "confront"}, {"text": "Go", "lane": "evade"}, "x"]
        out = combat.odds_on_slate(slate, {"confront": 62, "evade": 70})
        self.assertEqual([c.get("odds") if isinstance(c, dict) else c for c in out],
                         [62, 70, "x"])
        self.assertNotIn("odds", slate[0], "the slate it was handed is not edited")


class TheRules(unittest.TestCase):

    def test_initiative_holds_the_order(self):
        out = combat.play_round(_record(first="foe"), lane="confront", verb="x",
                                you=_you(), you_hp=30, rng=random.Random(1))
        self.assertEqual(out["exchange"]["beats"][0]["side"], "foe")
        out = combat.play_round(_record(first="you"), lane="confront", verb="x",
                                you=_you(), you_hp=30, rng=random.Random(1))
        self.assertEqual(out["exchange"]["beats"][0]["side"], "you")

    def test_the_jump_is_worth_having(self):
        """Hidden: a surprise round — he loses round one, your roll has advantage."""
        ini = combat.roll_initiative(random.Random(0), _you(), combat.foe_block(), 0)
        self.assertEqual((ini["first"], ini["surprise"]), ("you", "you"))
        rec = _record(first="you", surprise="you")
        out = combat.play_round(rec, lane="confront", verb="x", you=_you(), you_hp=30,
                                rng=random.Random(5))
        kinds = [b.get("kind") for b in out["exchange"]["beats"]]
        self.assertNotIn("strike", kinds)
        self.assertEqual(out["exchange"]["beats"][0]["roll"]["adv"], 1)
        self.assertGreater(combat.lane_odds(rec, _you(), "confront"),
                           combat.lane_odds(_record(), _you(), "confront"))

    def test_you_cannot_outrun_what_followed_you_here(self):
        """Hunted: an ambush — he goes first with advantage — and running is harder."""
        ini = combat.roll_initiative(random.Random(0), _you(), combat.foe_block(), 3)
        self.assertEqual((ini["first"], ini["surprise"]), ("foe", "foe"))
        rec = _record(first="foe", surprise="foe")
        out = combat.play_round(rec, lane="confront", verb="x", you=_you(), you_hp=30,
                                rng=random.Random(5))
        self.assertEqual(out["exchange"]["beats"][0]["roll"]["adv"], 1)
        hunted = dict(_record(), detection=3)
        hidden = dict(_record(), detection=0)
        self.assertLess(combat.lane_odds(hunted, _you(), "evade"),
                        combat.lane_odds(_record(), _you(), "evade"))
        self.assertGreater(combat.lane_odds(hidden, _you(), "evade"),
                           combat.lane_odds(_record(), _you(), "evade"))

    def test_the_level_the_fight_opened_on_decides_it(self):
        st = _state()
        enc = _enc(detection=3)
        rec = encounter.open_fight_record(copy.deepcopy(st), enc)
        self.assertEqual(rec["init"]["surprise"], "foe")

    def test_a_weapon_hits_more_often_and_harder(self):
        bare, armed = _you(), combat.player_block(name="Isaac", weapon="Riot Baton")
        self.assertGreater(combat.lane_odds(_record(), armed, "confront"),
                           combat.lane_odds(_record(), bare, "confront"))
        self.assertGreater(armed["dmg"][0] * (armed["dmg"][1] + 1) / 2 + armed["dmg"][2],
                           bare["dmg"][0] * (bare["dmg"][1] + 1) / 2 + bare["dmg"][2])
        st = _state(gear=[{"name": "Riot Baton", "kind": "weapon"}])
        self.assertEqual(encounter.fight_context(st, _enc())["you"]["weapon"], "Riot Baton")

    def test_the_pack_card_says_the_real_numbers(self):
        pack = (ROOT / "static" / "js" / "pack.js").read_text(encoding="utf-8")
        line = (f"+{combat.WEAPON_ATTACK - combat.PLAYER_ATTACK} to hit, and "
                f"{combat.spec_text(combat.WEAPON_DAMAGE)} damage instead of "
                f"{combat.spec_text(combat.PLAYER_DAMAGE)}")
        self.assertIn(line, pack)

    def test_a_fumble_opens_you_up(self):
        rec = _record(first="you")
        out = combat.play_round(rec, lane="confront", verb="x", you=_you(), you_hp=30,
                                rng=_Rng(face(1), face(10), face(10)))
        mine, his = out["exchange"]["beats"][0], out["exchange"]["beats"][1]
        self.assertIn("FUMBLE", mine["sub"])
        self.assertEqual(mine["text"], "You lose your footing.")
        self.assertEqual(his["roll"]["adv"], 1)

    def test_a_natural_20_doubles_the_dice(self):
        out = combat.play_round(_record(first="you"), lane="confront", verb="x", you=_you(),
                                you_hp=30, rng=_Rng(face(20), face(4, 8), face(5, 8)))
        mine = out["exchange"]["beats"][0]
        self.assertTrue(mine["crit"])
        self.assertEqual(mine["dmg"]["faces"], [4, 5])
        self.assertEqual(mine["damage"], 4 + 5 + combat.PLAYER_DAMAGE[2])

    def test_failing_to_run_costs_you(self):
        # you first: fail to flee -> his swing this round has advantage
        out = combat.play_round(_record(first="you"), lane="evade", verb="Run", you=_you(),
                                you_hp=30, rng=_Rng(face(2), face(10), face(12)))
        beats = out["exchange"]["beats"]
        self.assertEqual(beats[0]["result"], "caught")
        self.assertEqual(beats[1]["roll"]["adv"], 1)
        # he went first: failing gives him a free swing
        out = combat.play_round(_record(first="foe"), lane="evade", verb="Run", you=_you(),
                                you_hp=30, rng=_Rng(face(2), face(2), face(10), face(12)))
        kinds = [b["kind"] for b in out["exchange"]["beats"]]
        self.assertEqual(kinds, ["strike", "flee", "strike"])

    def test_talking_a_person_round(self):
        rec = _record()
        first = combat.talk_dc(rec)
        self.assertEqual(combat.talk_dc(dict(rec, talk_tries=2)), first - 2)
        bloodied = copy.deepcopy(rec)
        bloodied["foe"]["hp"] = 2
        self.assertEqual(combat.talk_dc(bloodied), first - combat.BLOODIED_TALK_DROP)

    def test_a_creature_and_the_boss_do_not_come_round(self):
        for block in (combat.foe_block("creature"), combat.foe_block("person", boss=True)):
            rec = dict(_record(), foe=dict(block, hp=block["max"], name="X", move="Y"))
            self.assertEqual(combat.talk_dc(dict(rec, talk_tries=5)), combat.talk_dc(rec))

    def test_the_boss_never_runs(self):
        boss = combat.foe_block("person", "opportunistic", boss=True)
        self.assertEqual(boss["morale"], 0)
        self.assertGreater(boss["max"], combat.foe_block("person")["max"])

    def test_luck_is_on_every_roll(self):
        self.assertEqual(combat.player_block(fate="LUCKY")["luck"], 2)
        self.assertEqual(combat.player_block(fate="UNLUCKY")["luck"], -2)
        self.assertGreater(combat.lane_odds(_record(), _you(luck=2), "parley"),
                           combat.lane_odds(_record(), _you(), "parley"))


class TheBarsMoveForWhatWasRolled(unittest.TestCase):

    def _sweep(self, n=3000):
        rng = random.Random(21)
        for _ in range(n):
            rec = _record(first=rng.choice(["you", "foe"]),
                          surprise=rng.choice([None, None, "you", "foe"]))
            rec["foe"]["hp"] = rng.randint(1, rec["foe"]["max"])
            rec["saves"] = rng.choice([0, 0, 1])
            hp = rng.choice([30, 12, 5, 1])
            armor = rng.random() < 0.3
            you = combat.player_block(name="Isaac", armor="Vest" if armor else "")
            yield combat.play_round(rec, lane=rng.choice(encounter.ENCOUNTER_LANES),
                                    verb="x", you=you, you_hp=hp, rng=rng,
                                    armor_ready=armor)

    def test_what_each_bar_loses_is_what_the_beats_say(self):
        for out in self._sweep():
            ex = out["exchange"]
            foe = sum(b.get("damage", 0) for b in ex["beats"] if b.get("target") == "foe")
            self.assertEqual(max(0, ex["foe"]["hp_before"] - foe), ex["foe"]["hp"], ex)
            you = sum(b.get("damage", 0) for b in ex["beats"] if b.get("target") == "you")
            back = any(b.get("result") in ("armor", "saved") for b in ex["beats"])
            if not back and out["end"] != "dead":
                self.assertEqual(ex["you"]["hp_before"] - you, ex["you"]["hp"], ex)

    def test_only_a_failed_death_save_empties_your_bar(self):
        for out in self._sweep():
            ex = out["exchange"]
            if out["alive"]:
                self.assertGreaterEqual(ex["you"]["hp"], 1, ex)
            else:
                self.assertEqual(ex["you"]["hp"], 0)
                self.assertEqual(ex["beats"][-1]["kind"], "save")
                self.assertFalse(ex["beats"][-1]["roll"]["ok"])

    def test_armour_takes_the_first_killing_blow_and_then_it_is_spent(self):
        rec = _record(first="foe")
        you = combat.player_block(name="Isaac", armor="Blast Apron")
        # he hits (face 15) for 1d8+2 on 3 HP
        out = combat.play_round(rec, lane="parley", verb="x", you=you, you_hp=3,
                                rng=_Rng(face(15), face(8), face(2)), armor_ready=True)
        self.assertEqual(out["armor_used"], "Blast Apron")
        self.assertEqual(out["exchange"]["you"]["hp"], 1)
        st = _state(gear=[{"name": "Blast Apron", "kind": "armor"}])
        self.assertTrue(encounter.fight_context(st, _enc())["armor_ready"])
        self.assertFalse(encounter.fight_context(st, _enc(armor_spent="Blast Apron"))["armor_ready"])

    def test_the_death_save_gets_harder_each_time(self):
        rec = _record(first="foe")
        out = combat.play_round(rec, lane="parley", verb="x", you=_you(), you_hp=2,
                                rng=_Rng(face(15), face(8), face(12), face(2)))
        save = [b for b in out["exchange"]["beats"] if b["kind"] == "save"][0]
        self.assertEqual(save["roll"]["target"], 10)
        self.assertTrue(out["alive"])
        again = combat.play_round(out["after"], lane="parley", verb="x", you=_you(), you_hp=1,
                                  rng=_Rng(face(15), face(8), face(12), face(2)))
        save = [b for b in again["exchange"]["beats"] if b["kind"] == "save"][0]
        self.assertEqual(save["roll"]["target"], 15)
        self.assertFalse(again["alive"])

    def test_a_man_who_is_down_does_not_swing_after(self):
        for out in self._sweep():
            beats = out["exchange"]["beats"]
            at = next((i for i, b in enumerate(beats)
                       if b.get("result") in ("ko", "settled", "away")), None)
            if at is None:
                continue
            self.assertFalse(any(b.get("kind") == "strike" for b in beats[at:]), beats)

    def test_the_rest_of_the_game_reads_the_same_words(self):
        for out in self._sweep(1500):
            end = out["end"]
            self.assertEqual(out["outcome"] == "die", end == "dead")
            self.assertEqual(out["outcome"] == "escape", end == "escaped")
            if end == "ko":
                self.assertEqual(out["enemy_state"], "down")
            if end in ("settled", "routed"):
                self.assertEqual(out["enemy_state"], "standing_down")
            self.assertTrue(encounter.encounter_releases(out["outcome"], out) == bool(end))


class AFightIsAFight(unittest.TestCase):
    """Pacing, measured through the real rules (tools/encounter_length_probe.py
    prints the whole table). Every round is a real generation on screen:
    "encounters take far too long" was a four-round fight."""

    def _fights(self, n=3000, boss=False, hp=30, kind="person"):
        import sys
        sys.path.insert(0, str(ROOT / "tools"))
        import encounter_length_probe as P
        rng = random.Random(7)
        return [P.fight(rng, kind=kind, stance="hostile", boss=boss, detection=1,
                        plan=lambda n, r: "confront", hp=hp, weapon=False, armor=False)
                for _ in range(n)]

    def test_a_roadside_fight_is_short(self):
        fights = self._fights()
        lens = sorted(n for n, _, _ in fights)
        self.assertLessEqual(lens[len(lens) // 2], 2)
        self.assertGreater(sum(n <= 3 for n in lens) / len(lens), 0.8)
        self.assertLessEqual(lens[-1], combat.COMBAT_MAX_ROUNDS)

    def test_a_fresh_player_rarely_dies_on_the_road_but_a_hurt_one_might(self):
        fresh = sum(e == "dead" for _, e, _ in self._fights()) / 3000
        hurt = sum(e == "dead" for _, e, _ in self._fights(hp=8)) / 3000
        self.assertLess(fresh, 0.04)
        self.assertGreater(hurt, 0.2)

    def test_the_boss_is_the_long_fight(self):
        lens = sorted(n for n, _, _ in self._fights(boss=True, n=1500))
        self.assertGreaterEqual(lens[len(lens) // 2], 3)


class TheBattleLine(unittest.TestCase):

    def test_the_verb_is_reported_in_the_past(self):
        for verb, want in (
            ("Shatter the camera against him.", "Isaac shattered the camera against him"),
            ("Hand him the stolen files.", "Isaac handed him the stolen files"),
            ("Grab the rifle barrel", "Isaac grabbed the rifle barrel"),
            ("Sprint through the dark breach.", "Isaac sprinted through the dark breach"),
            ("Throw sand in its eyes", "Isaac threw sand in its eyes"),
            ("Quickly shove him back", "Isaac quickly shoved him back"),
            ("Bury the pick in its skull", "Isaac buried the pick in its skull"),
            ("I try to run for the door", "Isaac tried to run for the door"),
        ):
            self.assertEqual(combat.past_tense(verb, "Isaac"), want)

    def test_a_hit_reads_like_one(self):
        out = combat.play_round(_record(first="you", hp=10), lane="confront",
                                verb="Crush the camera into his face", you=_you(), you_hp=30,
                                rng=_Rng(face(14), face(7, 8), face(3), face(15)))
        beats = out["exchange"]["beats"]
        self.assertEqual(beats[0]["intro"], "You crush the camera into his face…")
        self.assertEqual(beats[0]["text"], "It lands. 10 damage.")
        self.assertIn("ATTACK · d20 14+5 = 19 vs AC 12 · HIT · 1d8+3 = 10", beats[0]["sub"])
        self.assertEqual(beats[1]["text"], "The freelancer goes down.")

    def test_a_miss_does_not_claim_it_happened(self):
        out = combat.play_round(_record(first="you"), lane="confront",
                                verb="Crush the camera into his face", you=_you(), you_hp=30,
                                rng=_Rng(face(3), face(2)))
        self.assertEqual(out["exchange"]["beats"][0]["text"], "It misses.")

    def test_the_line_speaks_to_the_player_like_the_prose_does(self):
        # seen live: "Jason goes to drop your cam" — a second-person slate line
        # wrapped in a third-person frame.
        self.assertEqual(combat.you_go("Drop your camera at him."), "You drop your camera at him")
        self.assertEqual(combat.you_go("I try to run for the door"), "You try to run for the door")
        self.assertEqual(combat.you_go("The files — take them"), "You: “The files — take them”")
        out = combat.play_round(_record(first="foe"), lane="evade", verb="Sprint away",
                                you=_you(), you_hp=30, rng=_Rng(face(15), face(3), face(2)))
        for b in out["exchange"]["beats"]:
            for line in (b.get("intro") or "", b.get("text") or ""):
                self.assertNotIn("**", line)
                self.assertNotIn("!", line)
                self.assertNotIn("Isaac", line)

    def test_initiative_is_announced(self):
        rec = _record()
        self.assertIn("first.", combat.initiative_beat(rec)["text"])
        self.assertIn("INITIATIVE", combat.initiative_beat(rec)["sub"])
        self.assertEqual(combat.initiative_beat(_record(surprise="you"))["text"],
                         "The freelancer hasn't seen you yet.")
        self.assertEqual(combat.initiative_beat(_record(surprise="foe", first="foe"))["text"],
                         "The freelancer found you first.")
        self.assertIsNotNone(combat.hud(rec, 30)["initiative"])
        self.assertIsNone(combat.hud(dict(rec, turns=1), 30)["initiative"])

    def test_a_line_that_is_not_a_verb_is_quoted_not_mangled(self):
        self.assertEqual(combat.past_tense("The files — take them", "You"),
                         "You: “The files — take them”")

    def test_his_bar_is_named_like_a_battle_screen(self):
        self.assertEqual(combat.foe_short_name("An investigative freelancer"), "FREELANCER")
        self.assertEqual(combat.foe_short_name("A Horizon site foreman in a hazard suit"),
                         "SITE FOREMAN")
        self.assertEqual(combat.foe_short_name("whatever came up out of Shaft 6", "creature"),
                         "CREATURE")
        # seen live: "CITIZEN SUFFERING"
        self.assertEqual(combat.foe_short_name("crazed citizen suffering from neural fever"),
                         "CRAZED CITIZEN")
        self.assertEqual(combat.foe_short_name("A Dissenting Rogue System Control Officer"),
                         "CONTROL OFFICER")
        self.assertEqual(combat.foe_short_name("A cornered sibling"), "CORNERED SIBLING")

    def test_the_brief_names_him_not_the_picture(self):
        # seen live: a "Rogue Police Enforcer" fought under a bar reading MAN
        self.assertEqual(combat.best_foe_name(["Rogue Police Enforcer", "A man"]),
                         "POLICE ENFORCER")
        self.assertEqual(combat.best_foe_name(["", "A man"]), "MAN")
        rec = encounter.open_fight_record(
            _state(), {"_named": "Rogue Police Enforcer",
                       "character": {"label": "A man", "kind": "person"}})
        self.assertEqual(rec["foe"]["name"], "POLICE ENFORCER")

    def test_his_move_is_named(self):
        self.assertEqual(combat.foe_move({"move": "hidden blade"}), "HIDDEN BLADE")
        self.assertEqual(combat.foe_move({"kind": "creature"}), "LUNGE")
        self.assertEqual(combat.foe_move({"move": "!!"}), "STRIKE")

    def test_the_brief_keeps_the_move(self):
        brief = encounter.normalize_encounter_brief({
            "character": {"label": "A guard", "stance": "hostile", "move": "Rifle butt"},
            "danger": "d", "stakes": "s"})
        self.assertEqual(brief["character"]["move"], "Rifle butt")
        self.assertIn("move", encounter.ENCOUNTER_BRIEF_SCHEMA["properties"]
                      ["character"]["properties"])

    def test_the_opening_line(self):
        self.assertEqual(combat.opening_line("An investigative freelancer", "person", "hostile"),
                         "An investigative freelancer blocks your way.")



class TheBarsLastTheRun(unittest.TestCase):

    def test_walking_mends(self):
        ps = {"hp": 12, "hp_max": 30}
        self.assertEqual(combat.mend(ps), 12 + combat.MEND_BETWEEN_FIGHTS)
        self.assertEqual(ps["condition"], "ok")
        ps = {"hp": 29, "hp_max": 30}
        self.assertEqual(combat.mend(ps), 30)

    def test_a_run_from_before_the_bars(self):
        self.assertEqual(combat.player_hp({}), (30, 30))
        self.assertEqual(combat.player_hp({"condition": "wounded"}), (15, 30))

    def test_the_record_survives_the_rebuild(self):
        rec = _record()
        rec["pending"] = {"verb": "x"}
        brief = encounter.normalize_encounter_brief({
            "character": {"label": "A guard", "stance": "hostile"},
            "danger": "d", "stakes": "s", "combat": rec})
        self.assertEqual(brief["combat"]["foe"]["ac"], rec["foe"]["ac"])
        self.assertEqual(brief["combat"]["pending"], {"verb": "x"})
        self.assertEqual(encounter.align_brief_to_plate(brief)["combat"]["init"]["you"], 15)

    def test_a_record_from_before_the_rules_is_reopened(self):
        old = {"you": {"name": "ISAAC", "max": 40, "start": 40},
               "foe": {"name": "MAN", "hp": 30, "max": 30, "move": "STRIKE"}, "turns": 1}
        self.assertIsNone(combat.clean_combat(old))
        got = encounter.roll_exchange(_state(), _enc(combat=old), "Hit him", "confront",
                                      rng=random.Random(1))
        self.assertIn("ac", got["combat"]["foe"])


class TheDeathIsItsOwnScene(unittest.TestCase):
    """"hold suspense, render a new DEATH FLIPBOOK … all focused on your
    painful death, THEN show the YOU DIED" (Matt, 2026-09-23)."""

    def test_the_death_is_drawn_as_a_death(self):
        brief = encounter.normalize_encounter_brief({
            "character": {"label": "A freelancer", "stance": "hostile", "move": "Hidden blade",
                          "look": "a trench coat"},
            "danger": "d", "stakes": "s"})
        p = encounter.build_encounter_resolve_prompt(brief, "Hand him the files", "parley", "die")
        self.assertIn("THIS IS THE PLAYER'S DEATH", p)
        self.assertIn("Hidden blade", p)
        self.assertNotIn("AGENCY LOCK", p)
        self.assertNotIn("If you show a blow landing, you failed", p)
        self.assertIn("kills the player with Hidden blade", encounter._death_beat(brief))

    def test_the_client_holds_the_death_screen_for_the_reel(self):
        js = (ROOT / "static" / "js" / "standalone.js").read_text(encoding="utf-8")
        self.assertIn("async function playDeath(res)", js)
        self.assertIn("playPlateFrames(res.sequence, res.resolve_url, DEATH_FRAME_MS)", js)
        # the feed's game_over waits for the reel
        self.assertIn("if (dyingSeq) { dyingSeq.gameOver = item; return; }", js)
        self.assertIn("if (window.Encounter && Encounter.isDying && Encounter.isDying()) return;", js)
        # and the death screen names what did it
        self.assertIn("handleGameOver(over, deathCause(dead.exchange))", js)
        html = (ROOT / "templates" / "standalone.html").read_text(encoding="utf-8")
        self.assertIn('id="death-cause"', html)


class TheDiceAreThrownOnce(unittest.TestCase):
    """/api/encounter/exchange throws; /api/encounter/resolve draws that."""

    def setUp(self):
        self.store = {"s": {
            "player_state": {"alive": True, "condition": "ok", "hp": 30, "hp_max": 30},
            "turn_count": 3,
            "encounter": dict(_enc(
                choices=[{"text": "Shatter the camera against him", "lane": "confront"},
                         {"text": "Sprint through the breach", "lane": "evade"},
                         {"text": "Hand him the files", "lane": "parley"}],
                plate_url="/img/plate.png", danger="d", stakes="s")),
        }}
        self.store["s"]["encounter"]["combat"] = combat.open_combat(
            self.store["s"]["encounter"]["character"], you_name="Isaac", you_hp=30, you_max=30,
            detection=1)

    def _patches(self):
        import api
        load = lambda sid="default": copy.deepcopy(self.store.get(sid) or {})

        def save(st, sid="default"):
            self.store[sid] = copy.deepcopy(st)

        return [
            mock.patch.object(engine, "_rate_limited", return_value=False),
            mock.patch.object(engine, "_resolve_request_session_id", return_value="s"),
            mock.patch.object(engine, "_load_state", side_effect=load),
            mock.patch.object(engine, "_save_state", side_effect=save),
            mock.patch.object(engine, "_sync_ambient_state"),
            # The request wrapper persists the module-global mirror on exit,
            # which _sync_ambient_state keeps current in play; with that
            # mocked out, the wrapper would write a stale copy over the store.
            mock.patch.object(engine, "session_context",
                              lambda sid: contextlib.nullcontext(sid)),
            mock.patch.object(engine, "_load_history", return_value=[]),
            mock.patch.object(engine, "_save_history"),
            mock.patch.object(engine, "_to_web_image_url", return_value="/img/resolve.png"),
            mock.patch.object(engine, "_process_turn_background",
                              return_value={"choices": [
                                  {"text": "Crush it into his face", "lane": "confront"},
                                  {"text": "Break away", "lane": "evade"},
                                  {"text": "Offer the rest", "lane": "parley"}],
                                  "dispatch": "He staggers."}),
            mock.patch.object(encounter, "_generate_resolve_plate",
                              return_value=("/tmp/resolve.png", "img2img")),
            mock.patch.object(encounter, "world_flavor", return_value=""),
            mock.patch.object(encounter, "set_look_session"),
            mock.patch.object(api, "_spend_blocked", return_value=None),
        ]

    def _post(self, path, body):
        import api
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)
            r = api.app.test_client().post(path, json=dict(body, session_id="s"))
        return r.status_code, r.get_json()

    def test_asking_twice_is_the_same_throw(self):
        body = {"choice": "Hand him the files", "lane": "parley"}
        code, first = self._post("/api/encounter/exchange", body)
        self.assertEqual(code, 200, first)
        code, again = self._post("/api/encounter/exchange",
                                 {"choice": "Shatter the camera against him", "lane": "confront"})
        self.assertEqual(again["exchange"]["id"], first["exchange"]["id"])
        self.assertEqual(again["lane"], "parley")
        self.assertTrue(again.get("repeat"))

    def test_the_resolve_draws_what_was_thrown(self):
        code, thrown = self._post("/api/encounter/exchange",
                                  {"choice": "Shatter the camera against him", "lane": "confront"})
        self.assertEqual(code, 200, thrown)
        ex = thrown["exchange"]
        # the client posts something else — the throw stands
        code, res = self._post("/api/encounter/resolve",
                               {"choice": "Hand him the files", "lane": "parley"})
        self.assertEqual(code, 200, res)
        self.assertEqual(res["exchange"]["id"], ex["id"])
        self.assertEqual(res["lane"], "confront")
        self.assertEqual(res["verb"], "Shatter the camera against him")
        st = self.store["s"]
        self.assertEqual(st["player_state"]["hp"], ex["you"]["hp"])
        if not res["released"]:
            rec = st["encounter"]["combat"]
            self.assertNotIn("pending", rec)
            self.assertEqual(rec["foe"]["hp"], ex["foe"]["hp"])
            self.assertEqual(rec["turns"], 1)
            self.assertEqual(res["combat"]["foe"]["hp"], ex["foe"]["hp"])
            odds = [c.get("odds") for c in res["choices"]]
            self.assertTrue(all(isinstance(o, int) for o in odds), res["choices"])

    def test_a_resolve_with_no_throw_still_throws_one(self):
        code, res = self._post("/api/encounter/resolve",
                               {"choice": "Sprint through the breach", "lane": "evade"})
        self.assertEqual(code, 200, res)
        self.assertEqual(res["exchange"]["lane"], "evade")
        self.assertIn("flee", [b.get("kind") for b in res["exchange"]["beats"]])

    def test_no_throw_without_a_fight(self):
        self.store["s"]["encounter"] = None
        code, res = self._post("/api/encounter/exchange", {"choice": "x", "lane": "evade"})
        self.assertEqual((code, res.get("error")), (409, "no_encounter"))


    def test_a_clever_typed_action_rolls_with_advantage(self):
        """The skill in the system: the player's own words, judged against the
        scene, buy a second d20."""
        judged = {"lane": "confront", "advantage": True, "why": "the steam vent is behind him"}
        with mock.patch.object(encounter, "judge_custom_action", return_value=judged):
            code, thrown = self._post("/api/encounter/exchange",
                                      {"choice": "Kick the valve so the steam hits him",
                                       "custom": True})
        self.assertEqual(code, 200, thrown)
        mine = [b for b in thrown["exchange"]["beats"] if b.get("side") == "you"][0]
        self.assertEqual(mine["edge"], "the steam vent is behind him")
        self.assertEqual(mine["roll"]["adv"], 1)
        self.assertEqual(len(mine["roll"]["faces"]), 2)
        self.assertEqual(thrown["lane"], "confront")

    def test_a_plain_typed_action_rolls_plain(self):
        judged = {"lane": "parley", "advantage": False, "why": ""}
        with mock.patch.object(encounter, "judge_custom_action", return_value=judged):
            code, thrown = self._post("/api/encounter/exchange",
                                      {"choice": "Talk to him", "custom": True})
        mine = [b for b in thrown["exchange"]["beats"] if b.get("side") == "you"][0]
        self.assertNotIn("edge", mine)
        self.assertEqual(mine["roll"]["adv"], 0)

    def test_a_typed_shot_hurts_him_even_with_the_models_offline(self):
        """"shoot him with a gun" played the shot and left his health where
        it was: the judge and the lane reader both came back as the engine's
        offline sentence, and the lane fell through to REASON, which cannot
        deal damage. A plain attack is ATTACK without asking."""
        offline = "You are still in the yard. The next move is yours."
        hurt = False
        for _ in range(12):
            self.store["s"]["encounter"]["combat"] = combat.clean_combat(
                self.store["s"]["encounter"]["combat"]) or self.store["s"]["encounter"]["combat"]
            self.store["s"]["encounter"]["combat"].pop("pending", None)
            with mock.patch.object(engine, "_ask", return_value=offline):
                code, thrown = self._post("/api/encounter/exchange",
                                          {"choice": "shoot him with a gun", "custom": True})
            self.assertEqual(code, 200, thrown)
            self.assertEqual(thrown["lane"], "confront")
            mine = [b for b in thrown["exchange"]["beats"] if b.get("side") == "you"][0]
            if mine.get("target") == "foe" and int(mine.get("damage") or 0) > 0:
                hurt = True
                self.assertLess(thrown["exchange"]["foe"]["hp"],
                                self.store["s"]["encounter"]["combat"]["foe"]["hp"])
                break
        self.assertTrue(hurt, "twelve shots and not one hit")

    def test_the_judge_cannot_file_a_shot_as_talk(self):
        with mock.patch.object(engine, "_ask",
                               return_value='{"lane": "parley", "advantage": false}'):
            judged = encounter.judge_custom_action("shoot him with a gun", {})
        self.assertEqual(judged["lane"], "confront")
        with mock.patch.object(engine, "_ask",
                               return_value='{"lane": "parley", "advantage": false}'):
            judged = encounter.judge_custom_action("threaten to shoot him", {})
        self.assertEqual(judged["lane"], "parley")

    def test_a_judge_that_fails_falls_back_to_the_lane_reader(self):
        with mock.patch.object(encounter, "judge_custom_action", return_value={}), \
             mock.patch.object(encounter, "classify_custom_lane", return_value="evade"):
            code, thrown = self._post("/api/encounter/exchange",
                                      {"choice": "get out of here", "custom": True})
        self.assertEqual((code, thrown["lane"]), (200, "evade"))



if __name__ == "__main__":
    unittest.main()
