"""COMBAT — the fight's rules: a d20, two stat blocks and two health bars.

"make it a proper combat system that has an element of skill / dice roll.
like dungeons and dragons" (Matt, 2026-09-23), after two faults the first
cut of the bars made plain:

  * the last exchange was always 100%. The old engine (a weighted pick of
    survive / escape / wounded / die, then a second draw for whether the verb
    "settled" the other body) ended every roadside fight on round two by
    decree, so round two's odds read 100% and the only live die was his;
  * attacks fizzled. One band of that pick was `escape` — the fight simply
    breaking apart — and detection and luck raised it on every lane, so a
    committed attack ended 13–16% of the time with no roll of yours at all.

Both were the table talking, not a fight. This module replaces the table.
It is the ONLY place a fight is decided, and it is decided the way a tabletop
does it, in the open:

  ROLLS    d20 + modifier against a target. A natural 20 always succeeds (on
           an attack it is a CRITICAL: the damage dice are rolled twice); a
           natural 1 always fails (on your attack it is a FUMBLE: his next
           swing has advantage). ADVANTAGE rolls two d20 and keeps the higher,
           disadvantage the lower; one of each cancels.
  ATTACK   d20 + your attack bonus vs his ARMOUR CLASS; a hit rolls your
           damage dice off his HP.
  FLEE     d20 + athletics vs his FLEE DC. Success ends the fight; failure
           means you turned your back — his next swing has advantage.
  REASON   d20 + persuasion vs his TALK DC. Success: he stands down. Each
           failed attempt lowers the DC by 2 (he is listening), and a
           bloodied foe (at or under half HP) is 3 easier to talk down — so
           hitting him first and talking second is a real play.
  HIM      each round he attacks: d20 + his bonus vs your armour class, his
           named move (HIDDEN BLADE) and his damage dice.
  ORDER    initiative is rolled once when the fight opens and holds. How much
           the world knew about you decides the jump: hidden = SURPRISE (he
           loses round one and your first roll has advantage); alerted = he
           goes first; hunted = AMBUSH (he goes first with advantage, and
           running is harder — it followed you here).
  MORALE   a bloodied foe who is not the boss and not desperate rolls morale
           at the end of each round; failing it, he breaks and runs — a win.
  DOWN     at 0 HP you fall. Armour from the pack takes the first killing
           blow of a fight (you are left on 1 HP); after that, a DEATH SAVE:
           d20 ≥ 10 and you drag yourself up on 1 HP, the DC 5 higher each
           time in the same fight. Fail it and you are dead.
  LUCK     the turn's fate: LUCKY +2 to every roll you make, UNLUCKY −2.
  SKILL    a typed action that uses something real in the scene earns
           ADVANTAGE (encounter.judge_custom_action) — the one place the
           game rewards the player for thinking, not just for choosing.

The % printed beside each word on the slate is `lane_odds`: the exact chance
of that roll succeeding this round, advantage included — the same numbers
the dice are then thrown against. Every die the player sees is the die that
was rolled; there is nothing underneath it.

A fight ends when a bar empties, someone is talked down, someone runs, or
morale breaks. There is no round cap deciding it (COMBAT_MAX_ROUNDS is a
safety net far past where fights end — see tools/encounter_length_probe.py,
which plays thousands of fights through these rules).

Nothing here touches Flask, the network or the state file; `play_round`
takes an rng, so every turn is a unit test away (test_combat.py). What it
returns is the round as BEATS, in the order they happened — who acted, the
die, the target, the damage — which the client plays while the picture of
the round is still being drawn.
"""
from __future__ import annotations

import random
import re
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ── The numbers ────────────────────────────────────────────────────────────
PLAYER_MAX_HP = 30
PLAYER_AC = 12
PLAYER_INIT = 2
PLAYER_ATTACK = 5
PLAYER_DAMAGE = (1, 8, 3)       # fists, and whatever is to hand
WEAPON_ATTACK = 6               # a weapon from the pack: better to hit …
WEAPON_DAMAGE = (2, 6, 3)       # … and harder
PLAYER_FLEE = 3
PLAYER_TALK = 3
LUCK_BONUS = 2
# Walking between fights puts some back.
MEND_BETWEEN_FIGHTS = 8
# His stat block by what he is (encounter.ENCOUNTER_KINDS). dmg = (dice, sides, bonus).
FOE_BLOCKS = {
    "person":    dict(hp=11, ac=12, atk=4, dmg=(1, 8, 2), flee=12, talk=15, init=1, morale=10),
    "character": dict(hp=11, ac=12, atk=4, dmg=(1, 8, 2), flee=12, talk=15, init=1, morale=10),
    "group":     dict(hp=15, ac=11, atk=4, dmg=(2, 6, 1), flee=13, talk=16, init=0, morale=11),
    "creature":  dict(hp=13, ac=11, atk=5, dmg=(1, 10, 2), flee=14, talk=18, init=3, morale=10),
    "anomaly":   dict(hp=14, ac=13, atk=4, dmg=(1, 10, 1), flee=13, talk=18, init=2, morale=0),
}
# The one at the goal: the fight the run walked toward.
BOSS_HP_SCALE = 1.7
BOSS_PLUS = dict(ac=2, atk=0, dmg_bonus=1, flee=2, talk=5)
# His morale roll: d20 + this vs his morale DC.
MORALE_BONUS = 2
# Talking a person round: each failed attempt, they are listening a little
# more. A creature, an anomaly and the boss do not come round.
TALK_DROP_PER_TRY = 1
BLOODIED_TALK_DROP = 3
DEATH_SAVE_DC = 10
DEATH_SAVE_STEP = 5
COMBAT_MAX_ROUNDS = 10

LANE_WORDS = {"confront": "ATTACK", "evade": "FLEE", "parley": "REASON"}
DETECTION_HIDDEN, DETECTION_SUSPICIOUS, DETECTION_ALERTED, DETECTION_HUNTED = 0, 1, 2, 3


# ──────────────────────────────────────────────────────────────────────────
# Dice
# ──────────────────────────────────────────────────────────────────────────

def _die(rng: Any, sides: int) -> int:
    r = rng.random() if rng is not None else random.random()
    return max(1, min(sides, int(float(r) * sides) + 1))


def need_face(mod: int, target: int) -> int:
    """The lowest natural d20 face that succeeds (a 1 never does, a 20 always does)."""
    return max(2, min(20, int(target) - int(mod)))


def chance(mod: int, target: int, adv: int = 0) -> float:
    """Exact chance that d20 + mod meets target, natural 1 / 20 included."""
    p = (21 - need_face(mod, target)) / 20.0
    if adv > 0:
        return 1.0 - (1.0 - p) ** 2
    if adv < 0:
        return p * p
    return p


def pct(p: float) -> int:
    return int(round(max(0.0, min(1.0, p)) * 100))


def roll_d20(rng: Any, mod: int, target: int, adv: int = 0) -> dict:
    """One check, as the player sees it. `adv` > 0 advantage, < 0 disadvantage."""
    adv = (adv > 0) - (adv < 0)
    faces = [_die(rng, 20)] + ([_die(rng, 20)] if adv else [])
    kept = max(faces) if adv > 0 else min(faces) if adv < 0 else faces[0]
    ok = kept == 20 or (kept != 1 and kept + mod >= target)
    return {"faces": faces, "face": kept, "mod": int(mod), "total": kept + int(mod),
            "target": int(target), "need": need_face(mod, target), "adv": adv,
            "nat": kept if kept in (1, 20) else None, "ok": bool(ok),
            "chance": pct(chance(mod, target, adv))}


def spec_text(spec: Sequence[int]) -> str:
    n, d, b = (list(spec) + [0, 0, 0])[:3]
    return f"{n}d{d}" + (f"+{b}" if b > 0 else f"{b}" if b < 0 else "")


def roll_damage(rng: Any, spec: Sequence[int], crit: bool = False) -> dict:
    n, d, b = (list(spec) + [0, 0, 0])[:3]
    count = int(n) * (2 if crit else 1)
    faces = [_die(rng, int(d)) for _ in range(count)]
    return {"spec": spec_text((count, d, b)), "faces": faces,
            "total": max(1, sum(faces) + int(b)), "crit": bool(crit)}


def _mod_text(mod: int) -> str:
    return f"+{mod}" if mod >= 0 else f"{mod}"


def roll_line(r: dict, vs: str) -> str:
    """"d20 12+5 = 17 vs AC 12" — the sum the player can check."""
    faces = r["faces"]
    shown = (f"({faces[0]}, {faces[1]}) {'adv' if r['adv'] > 0 else 'dis'}"
             if len(faces) > 1 else f"{r['face']}")
    nat = " · NATURAL 20" if r["nat"] == 20 else " · NATURAL 1" if r["nat"] == 1 else ""
    return f"d20 {shown}{_mod_text(r['mod'])} = {r['total']} vs {vs} {r['target']}{nat}"


# ──────────────────────────────────────────────────────────────────────────
# Stat blocks
# ──────────────────────────────────────────────────────────────────────────

def player_block(*, name: str = "YOU", weapon: str = "", armor: str = "",
                 fate: str = "NORMAL") -> dict:
    f = str(fate or "NORMAL").strip().upper()
    luck = LUCK_BONUS if f == "LUCKY" else -LUCK_BONUS if f == "UNLUCKY" else 0
    return {
        "name": str(name or "YOU").upper()[:22],
        "ac": PLAYER_AC, "init": PLAYER_INIT,
        "atk": WEAPON_ATTACK if weapon else PLAYER_ATTACK,
        "dmg": list(WEAPON_DAMAGE if weapon else PLAYER_DAMAGE),
        "flee": PLAYER_FLEE, "talk": PLAYER_TALK, "luck": luck,
        "weapon": str(weapon or ""), "armor": str(armor or ""),
    }


def foe_block(kind: str = "person", stance: str = "hostile", boss: bool = False) -> dict:
    k = str(kind or "person").strip().lower()
    b = dict(FOE_BLOCKS.get(k) or FOE_BLOCKS["person"])
    s = str(stance or "hostile").strip().lower()
    dmg = list(b["dmg"])
    if s == "hostile":
        b["talk"] += 1
    elif s == "desperate":
        b["talk"] -= 1
        dmg[2] += 1
        b["morale"] = 0            # nothing left to lose: no morale to break
    elif s == "opportunistic":
        b["talk"] -= 4
        b["morale"] += 2
    if boss:
        b["hp"] = int(round(b["hp"] * BOSS_HP_SCALE))
        b["ac"] += BOSS_PLUS["ac"]
        b["atk"] += BOSS_PLUS["atk"]
        dmg[2] += BOSS_PLUS["dmg_bonus"]
        b["flee"] += BOSS_PLUS["flee"]
        b["talk"] += BOSS_PLUS["talk"]
        b["morale"] = 0            # the boss does not run
    b["dmg"] = dmg
    b["max"] = b["hp"]
    b["kind"] = k
    b["listens"] = 0 if (boss or k in ("creature", "anomaly")) else 1
    return b


def flee_dc(foe: dict, detection: Optional[int]) -> int:
    dc = int(foe.get("flee") or 12)
    if detection == DETECTION_HIDDEN:
        dc -= 2
    elif detection == DETECTION_HUNTED:
        dc += 3
    return dc


def bloodied(foe: dict) -> bool:
    return int(foe.get("hp") or 0) * 2 <= int(foe.get("max") or 1)


def talk_dc(record: dict) -> int:
    foe = record.get("foe") or {}
    dc = int(foe.get("talk") or 15) - (TALK_DROP_PER_TRY * int(record.get("talk_tries") or 0)
                                       * int(foe.get("listens", 1) or 0))
    if bloodied(foe):
        dc -= BLOODIED_TALK_DROP
    return dc


def roll_initiative(rng: Any, you: dict, foe: dict, detection: Optional[int]) -> dict:
    """Who acts first, for the whole fight. Decided by what the world knew
    about you when it started, else rolled."""
    if detection == DETECTION_HIDDEN:
        return {"first": "you", "surprise": "you", "you": None, "foe": None}
    if detection == DETECTION_HUNTED:
        return {"first": "foe", "surprise": "foe", "you": None, "foe": None}
    if detection == DETECTION_ALERTED:
        return {"first": "foe", "surprise": None, "you": None, "foe": None}
    a = _die(rng, 20) + int(you.get("init") or 0)
    b = _die(rng, 20) + int(foe.get("init") or 0)
    return {"first": "you" if a >= b else "foe", "surprise": None, "you": a, "foe": b}
# ──────────────────────────────────────────────────────────────────────────
# Names
# ──────────────────────────────────────────────────────────────────────────

_ARTICLE_RE = re.compile(r"^(?:a|an|the|some)\s+", re.I)
_CLAUSE_RE = re.compile(
    r"\s+(?:in|with|wearing|holding|carrying|of|on|at|from|behind|under|near|"
    r"who|that|which|by|out|up|into)\b.*$", re.I)
_NAME_JUNK = {"whatever", "something", "someone", "what", "who", "it", "came",
              "is", "was", "are"}
_NAME_STOP = {"someone", "something", "presence", "stranger", "figure", "thing"}


def foe_short_name(label: str, kind: str = "", limit: int = 22) -> str:
    """The name on his bar: the noun of his label, the way a battle screen
    names what you are fighting. "An investigative freelancer" -> FREELANCER,
    "A Horizon site foreman" -> SITE FOREMAN."""
    text = re.sub(r"\s+", " ", str(label or "")).strip().strip(".,;:!")
    text = _ARTICLE_RE.sub("", text)
    text = _CLAUSE_RE.sub("", text).strip()
    words = [w for w in re.split(r"\s+", text) if w]
    # A participle after the noun starts a clause too: "crazed citizen
    # suffering from neural fever" is a CRAZED CITIZEN, not a CITIZEN
    # SUFFERING (seen live). Not "thing"/"sibling": those are the noun.
    for i, w in enumerate(words):
        lw = w.lower()
        if i and len(lw) >= 7 and lw.endswith("ing") and not lw.endswith(("thing", "ling")):
            words = words[:i]
            break
    picked: List[str] = []
    for w in reversed(words):
        cand = " ".join([w] + picked)
        if len(cand) > limit:
            break
        picked.insert(0, w)
        if len(picked) >= 2:
            break
    name = " ".join(picked).strip("-—,")
    if any(w.lower() in _NAME_JUNK for w in picked):
        name = ""
    if not name or name.lower() in _NAME_STOP and len(words) <= 1:
        k = str(kind or "").strip().lower()
        name = {"creature": "creature", "group": "crew", "anomaly": "thing"}.get(k, name or "stranger")
    return name.upper()[:limit]


# A reading of the picture, not a name: what a plate caption falls back to.
GENERIC_NAMES = {"MAN", "WOMAN", "FIGURE", "STRANGER", "PERSON", "SOMEONE",
                 "PRESENCE", "SHAPE", "SILHOUETTE", "THING", "PEOPLE", "MEN",
                 "WOMEN", "BODY"}


def best_foe_name(labels: Iterable[Any], kind: str = "") -> str:
    """The first of `labels` that names someone — the brief's own label
    before the plate relabels them "A man" — else the last one, however
    plain."""
    names = [foe_short_name(str(l), kind) for l in labels if str(l or "").strip()]
    for n in names:
        if n and n not in GENERIC_NAMES:
            return n
    return names[-1] if names else foe_short_name("", kind)


_MOVE_FALLBACK = {
    "creature": "LUNGE",
    "group": "RUSH",
    "anomaly": "SURGE",
}


def foe_move(character: Optional[dict]) -> str:
    """What he does to you, as the battle line names it ("used HIDDEN BLADE!").
    The brief writes it off what the plate shows him holding or being
    (encounter.ENCOUNTER_BRIEF_SCHEMA); a brief from before that field existed
    falls back to his kind."""
    char = character if isinstance(character, dict) else {}
    raw = re.sub(r"[^A-Za-z0-9' \-]", " ", str(char.get("move") or ""))
    raw = re.sub(r"\s+", " ", raw).strip()
    if raw:
        words = raw.split(" ")[:3]
        move = " ".join(words).upper()
        if 2 < len(move) <= 24:
            return move
    kind = str(char.get("kind") or "").strip().lower()
    if kind in _MOVE_FALLBACK:
        return _MOVE_FALLBACK[kind]
    stance = str(char.get("stance") or "").strip().lower()
    return "WILD SWING" if stance == "desperate" else "STRIKE"


def opening_line(label: str, kind: str = "", stance: str = "", boss: bool = False) -> str:
    """The first thing a fight says: "An investigative freelancer blocks your
    way." Said the way the rest of the game says things — plainly, in the
    second person. The first cut shouted ("blocks your way!", the name in
    bold), and next to the game's own prose it read as a cartoon."""
    text = re.sub(r"\s+", " ", str(label or "")).strip().rstrip(".!")
    who = text or "Someone"
    who = who[0].upper() + who[1:]
    if boss:
        return f"{who} has been waiting for you."
    k = str(kind or "").strip().lower()
    s = str(stance or "").strip().lower()
    if k == "creature":
        return f"{who} is coming for you."
    if s == "desperate":
        return f"{who} is cornered, and dangerous."
    if s == "opportunistic":
        return f"{who} wants something from you."
    return f"{who} blocks your way."


# ──────────────────────────────────────────────────────────────────────────
# The verb, in the past tense
# ──────────────────────────────────────────────────────────────────────────
# The slate writes imperatives ("Shatter the camera against him."); the battle
# line reports what happened ("Isaac shattered the camera against him!"). The
# exchange is thrown before any model has written a word about it, so this is
# done by rule — and a verb the rule cannot place is quoted rather than mangled.

_IRREGULAR = {
    "be": "was", "beat": "beat", "become": "became", "bend": "bent", "bite": "bit",
    "bleed": "bled", "blow": "blew", "break": "broke", "bring": "brought",
    "build": "built", "burn": "burned", "burst": "burst", "buy": "bought",
    "cast": "cast", "catch": "caught", "choose": "chose", "cling": "clung",
    "come": "came", "creep": "crept", "cut": "cut", "deal": "dealt", "dig": "dug",
    "dive": "dove", "do": "did", "draw": "drew", "drink": "drank", "drive": "drove",
    "eat": "ate", "fall": "fell", "feed": "fed", "feel": "felt", "fight": "fought",
    "find": "found", "flee": "fled", "fling": "flung", "fly": "flew",
    "forget": "forgot", "freeze": "froze", "get": "got", "give": "gave", "go": "went",
    "grind": "ground", "grip": "gripped", "hang": "hung", "have": "had", "hear": "heard",
    "hide": "hid", "hit": "hit", "hold": "held", "hurt": "hurt", "keep": "kept",
    "kneel": "knelt", "know": "knew", "lay": "laid", "lead": "led", "leap": "leapt",
    "leave": "left", "lend": "lent", "let": "let", "lie": "lied", "light": "lit",
    "lose": "lost", "make": "made", "mean": "meant", "meet": "met", "pay": "paid",
    "put": "put", "quit": "quit", "read": "read", "ride": "rode", "ring": "rang",
    "rise": "rose", "run": "ran", "say": "said", "see": "saw", "seek": "sought",
    "sell": "sold", "send": "sent", "set": "set", "shake": "shook", "shed": "shed",
    "shine": "shone", "shoot": "shot", "show": "showed", "shut": "shut", "sing": "sang",
    "sink": "sank", "sit": "sat", "slay": "slew", "sleep": "slept", "slide": "slid",
    "sling": "slung", "slink": "slunk", "slit": "slit", "speak": "spoke",
    "speed": "sped", "spend": "spent", "spin": "spun", "spit": "spat", "split": "split",
    "spread": "spread", "spring": "sprang", "stand": "stood", "steal": "stole",
    "stick": "stuck", "sting": "stung", "strike": "struck", "swear": "swore",
    "sweep": "swept", "swim": "swam", "swing": "swung", "take": "took",
    "teach": "taught", "tear": "tore", "tell": "told", "think": "thought",
    "throw": "threw", "thrust": "thrust", "tread": "trod", "wake": "woke",
    "wear": "wore", "weave": "wove", "win": "won", "wind": "wound", "wring": "wrung",
    "write": "wrote",
}
# Short verbs whose last consonant doubles: grab -> grabbed. Anything not
# listed takes the plain rule, which is right for the long tail.
_DOUBLES = {
    "bag", "bat", "beg", "blot", "bob", "brag", "chip", "chop", "clap", "clip",
    "clog", "cram", "crop", "dab", "dip", "drag", "drip", "drop", "drum", "fan",
    "flap", "flip", "grab", "grin", "hug", "jab", "jam", "jog", "jot", "kid",
    "knit", "knot", "lob", "lug", "mob", "mug", "nab", "nag", "nip", "nod", "pat",
    "pin", "plan", "plod", "plot", "plug", "pop", "prod", "prop", "rap", "rib",
    "rip", "rob", "rub", "sag", "scan", "scrub", "ship", "shop", "shrug", "skid",
    "skip", "slam", "slap", "slip", "slit", "slog", "slug", "snap", "snip", "sob",
    "spot", "stab", "stem", "step", "stir", "stop", "strap", "strip", "sum",
    "swap", "swat", "tag", "tap", "tip", "trap", "trim", "trip", "tug", "wag",
    "whip", "wrap", "zap", "bar", "blur", "char", "jar", "mar", "scar", "spar",
    "star", "stun", "shun", "span", "ram", "rig", "dig", "bud",
}
_NOT_VERBS = {"the", "a", "an", "my", "his", "her", "their", "your", "this",
              "that", "it", "no", "not", "with", "at", "to", "and", "or", "him",
              "them", "over", "into", "away", "back", "now", "just"}
_PRONOUN_LEAD_RE = re.compile(r"^(?:i|you|we|they|i'll|i will|let me|try to|attempt to)\s+", re.I)


def _past_word(word: str) -> Optional[str]:
    low = word.lower()
    if not re.fullmatch(r"[a-z]+", low):
        return None
    if low in _IRREGULAR:
        out = _IRREGULAR[low]
    elif low.endswith("e"):
        out = low + "d"
    elif re.search(r"[^aeiou]y$", low):
        out = low[:-1] + "ied"
    elif low in _DOUBLES:
        out = low + low[-1] + "ed"
    elif low.endswith("c"):
        out = low + "ked"
    else:
        out = low + "ed"
    return out


def attempted(verb: str, subject: str) -> str:
    """"Crush his windpipe." -> "Isaac tried to crush his windpipe" — for a
    verb that never got to land, so the line does not claim it did."""
    text = re.sub(r"\s+", " ", str(verb or "")).strip().strip("\"'“”")
    text = re.sub(r"[.!?…]+$", "", text).strip()
    text = _PRONOUN_LEAD_RE.sub("", text)
    if not text:
        return f"{subject} made a move"
    first = text.split(" ", 1)[0]
    if first.isupper() and len(first) > 1:
        return f"{subject} tried: “{text}”"
    return f"{subject} tried to {text[0].lower()}{text[1:]}"


def past_tense(verb: str, subject: str) -> str:
    """"Shatter the camera against him." -> "Isaac shattered the camera
    against him". Adverbs lead fine ("Quickly shove him" -> "Isaac quickly
    shoved him"). A line that does not open on a verb is quoted instead."""
    text = re.sub(r"\s+", " ", str(verb or "")).strip().strip("\"'“”")
    text = re.sub(r"[.!?…]+$", "", text).strip()
    text = _PRONOUN_LEAD_RE.sub("", text)
    if not text:
        return f"{subject} made a move"
    words = text.split(" ")
    lead: List[str] = []
    while words and words[0].lower().endswith("ly") and len(words) > 1:
        lead.append(words.pop(0).lower())
    first = words[0]
    # A line that opens on something other than a bare lower/upper-case word
    # (a quote, a number, a name) is not ours to conjugate.
    past = _past_word(first) if first[:1].isalpha() else None
    if not past or first.lower() in _NOT_VERBS:
        return f"{subject}: “{text}”"
    rest = " ".join(words[1:])
    parts = [subject] + lead + [past] + ([rest] if rest else [])
    return " ".join(parts)


# ──────────────────────────────────────────────────────────────────────────


def trying(verb: str, subject: str) -> str:
    """"Crush his windpipe." -> "Isaac goes to crush his windpipe" — the line
    while the die is in the air: it has not happened yet."""
    t = attempted(verb, subject)
    return t.replace(f"{subject} tried to ", f"{subject} goes to ", 1).replace(
        f"{subject} tried: ", f"{subject}: ", 1)


def you_go(verb: str) -> str:
    """"Crush his windpipe." -> "You crush his windpipe". The battle line
    speaks the way the game's prose does — to the player, in the present — so
    the slate's own words go in as they are. Written about the character by
    name it came out as "Jason goes to drop your cam" (seen live): the slate
    is already second person, and a third-person frame round it could not
    hold the "your". A line that does not open on a verb is quoted."""
    text = re.sub(r"\s+", " ", str(verb or "")).strip().strip("\"'“”")
    text = re.sub(r"[.!?…]+$", "", text).strip()
    text = _PRONOUN_LEAD_RE.sub("", text)
    if not text:
        return "You make your move"
    first = text.split(" ", 1)[0]
    if (first.isupper() and len(first) > 1) or not first[:1].isalpha() \
            or first.lower() in _NOT_VERBS or _past_word(first) is None:
        return f"You: “{text}”"
    return f"You {text[0].lower()}{text[1:]}"


def the_foe(name: str, boss: bool = False) -> str:
    """"RECLAMATION GUARD" -> "the reclamation guard". A boss is named by the
    goal and keeps the name as a name."""
    n = _subj(name)
    if boss or n == "You":
        return n
    return "the " + n.lower()


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


# ──────────────────────────────────────────────────────────────────────────
# HP
# ──────────────────────────────────────────────────────────────────────────

def player_hp(player_state: Optional[dict]) -> Tuple[int, int]:
    """(hp, max) for the run. A run from before HP existed reads as full, or
    as half if it was already hurt."""
    ps = player_state if isinstance(player_state, dict) else {}
    try:
        mx = max(1, int(ps.get("hp_max") or PLAYER_MAX_HP))
    except (TypeError, ValueError):
        mx = PLAYER_MAX_HP
    if ps.get("hp") is None:
        wounded = str(ps.get("condition") or "ok").strip().lower() == "wounded"
        return (mx // 2 if wounded else mx), mx
    try:
        hp = int(ps.get("hp"))
    except (TypeError, ValueError):
        hp = mx
    return max(0, min(mx, hp)), mx


def condition_for(hp: int, mx: int) -> str:
    """The one flag the rest of the game reads (the prose, the old saves):
    wounded at or under half."""
    return "wounded" if int(hp) * 2 <= int(mx) else "ok"


def mend(player_state: dict) -> int:
    """Walking to the next fight puts some of it back. Mutates and returns hp."""
    hp, mx = player_hp(player_state)
    hp = min(mx, hp + MEND_BETWEEN_FIGHTS)
    player_state["hp"] = hp
    player_state["hp_max"] = mx
    player_state["condition"] = condition_for(hp, mx)
    return hp


def _subj(name: str) -> str:
    n = str(name or "").strip()
    return "You" if not n or n.upper() == "YOU" else n.title() if n.isupper() else n


def initiative_beat(record: dict) -> dict:
    ini = record.get("init") or {}
    The = _cap(the_foe((record.get("foe") or {}).get("name"), bool(record.get("boss"))))
    if ini.get("surprise") == "you":
        return {"kind": "initiative", "text": f"{The} hasn't seen you yet.",
                "sub": "HIDDEN · you move first · your first roll has advantage"}
    if ini.get("surprise") == "foe":
        return {"kind": "initiative", "text": f"{The} found you first.",
                "sub": "HUNTED · he strikes first, with advantage · running is harder"}
    if ini.get("you") is None:
        return {"kind": "initiative", "text": f"{The} saw you coming.",
                "sub": "ALERTED · he has the first move"}
    return {"kind": "initiative",
            "text": "You move first." if ini.get("first") == "you" else f"{The} moves first.",
            "sub": f"INITIATIVE · d20{_mod_text(PLAYER_INIT)} = {ini.get('you')} · "
                   f"d20{_mod_text(int((record.get('foe') or {}).get('init') or 0))} = {ini.get('foe')}"}


def open_combat(character: Optional[dict], *, you_name: str, you_hp: int,
                you_max: int, boss: bool = False, name: str = "",
                detection: Optional[int] = None, rng: Any = None) -> dict:
    """The fight's own record, carried on the encounter brief for its life.
    Initiative is rolled here, once."""
    char = character if isinstance(character, dict) else {}
    foe = foe_block(char.get("kind") or "person", char.get("stance") or "hostile", boss)
    foe["name"] = (str(name).upper()[:22] if name else
                   foe_short_name(char.get("label") or "", char.get("kind") or ""))
    foe["move"] = foe_move(char)
    you = {"name": str(you_name or "YOU").upper()[:22], "max": int(you_max), "start": int(you_hp)}
    record = {
        "you": you,
        "foe": foe,
        "detection": detection,
        "turns": 0, "talk_tries": 0, "saves": 0, "fumbled": False,
        "boss": bool(boss),
        "opening": opening_line(char.get("label") or "", char.get("kind") or "",
                                char.get("stance") or "", boss=boss),
    }
    record["init"] = roll_initiative(rng, player_block(name=you["name"]), foe, detection)
    return record


def _int(v: Any, d: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def clean_combat(raw: Any) -> Optional[dict]:
    """Keep a combat record through encounter.normalize_encounter_brief's
    rebuild, coerced, so a hand-edited or half-written one cannot crash a
    turn. A record from before these rules (no armour class on him) is not a
    record: the fight re-opens its bars under the rules it will be played by."""
    if not isinstance(raw, dict):
        return None
    you = raw.get("you") if isinstance(raw.get("you"), dict) else {}
    foe = raw.get("foe") if isinstance(raw.get("foe"), dict) else {}
    if foe.get("ac") is None:
        return None
    base = foe_block(foe.get("kind") or "person")
    f = {}
    for k in ("ac", "atk", "flee", "talk", "init", "morale", "listens"):
        f[k] = _int(foe.get(k), base[k])
    f["max"] = max(1, _int(foe.get("max"), base["max"]))
    f["hp"] = max(0, min(f["max"], _int(foe.get("hp"), f["max"])))
    dmg = foe.get("dmg") if isinstance(foe.get("dmg"), (list, tuple)) else base["dmg"]
    f["dmg"] = [_int(x, y) for x, y in zip((list(dmg) + [0, 0, 0])[:3], base["dmg"])]
    f["kind"] = str(foe.get("kind") or "person")[:20]
    f["name"] = str(foe.get("name") or "STRANGER")[:22]
    f["move"] = str(foe.get("move") or "STRIKE")[:24]
    ini = raw.get("init") if isinstance(raw.get("init"), dict) else {}
    det = raw.get("detection")
    out = {
        "you": {"name": str(you.get("name") or "YOU")[:22],
                "max": max(1, _int(you.get("max"), PLAYER_MAX_HP)),
                "start": max(0, _int(you.get("start"), PLAYER_MAX_HP))},
        "foe": f,
        "detection": _int(det, 1) if det is not None else None,
        "init": {"first": "foe" if ini.get("first") == "foe" else "you",
                 "surprise": ini.get("surprise") if ini.get("surprise") in ("you", "foe") else None,
                 "you": ini.get("you"), "foe": ini.get("foe")},
        "turns": max(0, _int(raw.get("turns"), 0)),
        "talk_tries": max(0, _int(raw.get("talk_tries"), 0)),
        "saves": max(0, _int(raw.get("saves"), 0)),
        "fumbled": bool(raw.get("fumbled")),
        "boss": bool(raw.get("boss")),
        "opening": str(raw.get("opening") or "")[:200],
    }
    if isinstance(raw.get("pending"), dict):
        out["pending"] = raw["pending"]
    if isinstance(raw.get("last"), dict):
        out["last"] = raw["last"]
    return out


# ──────────────────────────────────────────────────────────────────────────
# Odds
# ──────────────────────────────────────────────────────────────────────────

def _opening_adv(record: dict) -> int:
    ini = record.get("init") or {}
    return 1 if int(record.get("turns") or 0) == 0 and ini.get("surprise") == "you" else 0


def lane_odds(record: dict, you: dict, lane: str, edge: int = 0) -> int:
    """The number beside a word on the slate: the exact chance this round's
    roll succeeds. ATTACK: you hit. FLEE: you get away. REASON: he stands
    down. The same mods, targets and advantage play_round rolls against."""
    foe = record.get("foe") or {}
    luck = int(you.get("luck") or 0)
    adv = _opening_adv(record) + int(edge or 0)
    if lane == "confront":
        return pct(chance(int(you["atk"]) + luck, int(foe.get("ac") or 12), adv))
    if lane == "evade":
        return pct(chance(int(you["flee"]) + luck, flee_dc(foe, record.get("detection")), adv))
    return pct(chance(int(you["talk"]) + luck, talk_dc(record), adv))


# ──────────────────────────────────────────────────────────────────────────
# A round
# ──────────────────────────────────────────────────────────────────────────

def play_round(record: dict, *, lane: str, verb: str, you: dict, you_hp: int,
               rng: Any = None, edge: int = 0, edge_why: str = "",
               armor_ready: bool = False) -> dict:
    """Play one round under the rules above. Returns
    ``{exchange, after, outcome, alive, condition, enemy_state, armor_used}``:
    the beats the player watches, the record the fight goes on with, and the
    words the rest of the game already reads a fight by (the picture prompt,
    the engine turn, the verdict)."""
    rec = clean_combat(record) or record
    lane = lane if lane in LANE_WORDS else "confront"
    foe = dict(rec["foe"])
    ymax = int((rec.get("you") or {}).get("max") or PLAYER_MAX_HP)
    hp0 = max(0, min(ymax, int(you_hp)))
    fhp0 = int(foe["hp"])
    hp, fhp = hp0, fhp0
    YOU = _subj((rec.get("you") or {}).get("name") or you.get("name"))
    FOE = _subj(foe.get("name"))
    creature = foe.get("kind") == "creature"
    The = _cap(the_foe(foe.get("name"), bool(rec.get("boss"))))
    MOVE = foe.get("move") or "STRIKE"
    luck = int(you.get("luck") or 0)
    first_round = int(rec.get("turns") or 0) == 0
    ini = rec.get("init") or {}
    order = ["you", "foe"] if ini.get("first") != "foe" else ["foe", "you"]
    foe_skips = first_round and ini.get("surprise") == "you"
    foe_adv = (1 if first_round and ini.get("surprise") == "foe" else 0) + (1 if rec.get("fumbled") else 0)
    fumbled = False
    my_adv = _opening_adv(rec) + int(edge or 0)
    talk_tries = int(rec.get("talk_tries") or 0)
    saves = int(rec.get("saves") or 0)
    armor_used = ""
    beats: List[dict] = []
    end: Optional[str] = None
    foe_acted = False

    def luck_note() -> str:
        return f" · luck {_mod_text(luck)}" if luck else ""

    def foe_attack(extra_adv: int = 0, opportunity: bool = False) -> None:
        nonlocal hp, end, saves, armor_used, foe_adv
        r = roll_d20(rng, int(foe["atk"]), int(you.get("ac") or PLAYER_AC), foe_adv + extra_adv)
        foe_adv = 0
        intro = (f"You turn your back — {The.lower() if The.startswith('The ') else The} gets a free swing…"
                 if opportunity else f"{The} comes at you — {MOVE.lower()}…")
        beat = {"side": "foe", "kind": "strike", "move": MOVE, "intro": intro, "roll": r,
                "target": "you", "damage": 0, "hp_before": hp}
        sub = [MOVE, roll_line(r, "AC")]
        if r["ok"]:
            crit = r["nat"] == 20
            dmg = roll_damage(rng, foe["dmg"], crit)
            sub += ["CRITICAL" if crit else "HIT", f"{dmg['spec']} = {dmg['total']}"]
            hp -= dmg["total"]
            beat.update(result="crit" if crit else "hit", damage=dmg["total"], dmg=dmg, crit=crit,
                        text=(f"{The} hits you hard. {dmg['total']} damage." if crit
                              else f"{The} hits you. {dmg['total']} damage."))
        else:
            sub.append("MISS")
            beat.update(result="miss", text=f"{The} misses.")
        beat["hp_after"] = max(0, hp)
        beat["sub"] = " · ".join(sub)
        beats.append(beat)
        if hp > 0:
            return
        # DOWN. Armour first, once a fight; then the save.
        if armor_ready and not armor_used:
            armor_used = str(you.get("armor") or "armour")
            hp = 1
            beats.append({"side": "you", "kind": "armor", "result": "armor", "target": "you",
                          "text": f"The {armor_used.lower()} takes the killing blow. You're still standing.",
                          "sub": f"ARMOUR · {armor_used.upper()} · spent for this fight",
                          "damage": 0, "hp_after": 1, "crit": True})
            return
        dc = DEATH_SAVE_DC + DEATH_SAVE_STEP * saves
        saves += 1
        s = roll_d20(rng, 0, dc)
        beat = {"side": "you", "kind": "save", "intro": "You go down…",
                "roll": s, "target": "you", "damage": 0,
                "sub": f"DEATH SAVE · {roll_line(s, 'DC')} · {'SAVED' if s['ok'] else 'FAILED'}"}
        if s["ok"]:
            hp = 1
            beat.update(result="saved", hp_after=1,
                        text="You drag yourself back up.")
        else:
            hp = 0
            end = "dead"
            beat.update(result="dead", hp_after=0, text="You don't get up.")
        beats.append(beat)

    def my_action() -> None:
        nonlocal fhp, end, talk_tries, fumbled, foe_adv
        lw = LANE_WORDS[lane]
        beat = {"side": "you", "lane": lane, "lane_word": lw, "intro": f"{you_go(verb)}…",
                "damage": 0, "target": None}
        if edge_why and int(edge or 0) > 0:
            beat["edge"] = edge_why
        if lane == "confront":
            r = roll_d20(rng, int(you["atk"]) + luck, int(foe["ac"]), my_adv)
            sub = [lw, roll_line(r, "AC") + luck_note()]
            beat.update(kind="attack", roll=r, target="foe", hp_before=fhp)
            if r["ok"]:
                crit = r["nat"] == 20
                dmg = roll_damage(rng, you["dmg"], crit)
                fhp = max(0, fhp - dmg["total"])
                sub += ["CRITICAL" if crit else "HIT", f"{dmg['spec']} = {dmg['total']}"]
                if you.get("weapon"):
                    sub.append(f"with the {you['weapon'].lower()}")
                beat.update(result="crit" if crit else "hit", damage=dmg["total"], dmg=dmg, crit=crit,
                            text=(f"It lands clean. {dmg['total']} damage." if crit
                                  else f"It lands. {dmg['total']} damage."))
                if fhp <= 0:
                    beat["result"] = "ko"
                    end = "ko"
            else:
                if r["nat"] == 1:
                    fumbled = True
                    foe_adv += 1
                    sub.append("FUMBLE — his next swing has advantage")
                    beat.update(result="miss", text="You lose your footing.")
                else:
                    sub.append("MISS")
                    beat.update(result="miss", text="It misses.")
            beat["hp_after"] = fhp
        elif lane == "evade":
            dc = flee_dc(foe, rec.get("detection"))
            r = roll_d20(rng, int(you["flee"]) + luck, dc, my_adv)
            beat.update(kind="flee", roll=r)
            sub = [lw, roll_line(r, "DC") + luck_note()]
            if r["ok"]:
                sub.append("AWAY")
                beat.update(result="away", text="You get away.")
                end = "escaped"
            else:
                sub.append("CAUGHT — his next swing has advantage")
                beat.update(result="caught", text=f"{The} cuts you off.")
                foe_adv += 1
        else:
            dc = talk_dc(dict(rec, talk_tries=talk_tries, foe=dict(foe, hp=fhp)))
            r = roll_d20(rng, int(you["talk"]) + luck, dc, my_adv)
            beat.update(kind="reason", roll=r)
            sub = [lw, roll_line(r, "DC") + luck_note()]
            if r["ok"]:
                sub.append("PERSUADED")
                beat.update(result="settled",
                            text=f"{The} backs {'away' if creature else 'off'}.")
                end = "settled"
            else:
                talk_tries += 1
                drop = TALK_DROP_PER_TRY * int(foe.get("listens", 1) or 0)
                sub.append(f"NOT LISTENING · DC {dc} → {dc - drop} next time" if drop
                           else "NOT LISTENING")
                beat.update(result="listening", text=f"{The} isn't listening.")
        beat["sub"] = " · ".join(sub)
        beats.append(beat)

    for who in order:
        if end:
            break
        if who == "you":
            my_action()
            # Caught running after he has already swung: he gets a free one.
            if lane == "evade" and not end and foe_acted and foe_adv > 0:
                foe_attack(opportunity=True)
        else:
            if foe_skips:
                beats.append({"side": "foe", "kind": "note", "result": "flat",
                              "text": f"{The} is caught flat-footed.", "sub": "SURPRISE ROUND",
                              "damage": 0, "target": None})
            else:
                foe_attack()
            foe_acted = True

    turns = int(rec.get("turns") or 0) + 1
    foe["hp"] = fhp
    # MORALE, end of round: a bloodied foe with something to lose may break.
    if not end and fhp > 0 and int(foe.get("morale") or 0) > 0 and bloodied(foe):
        m = roll_d20(rng, MORALE_BONUS, int(foe["morale"]))
        beat = {"side": "foe", "kind": "morale", "intro": f"{The} is hurt…", "roll": m,
                "damage": 0, "target": None,
                "sub": f"MORALE · {roll_line(m, 'DC')} · {'HOLDS' if m['ok'] else 'BROKEN'}"}
        if m["ok"]:
            beat.update(result="holds", text=f"{The} is hurt, but holds.")
        else:
            beat.update(result="routed", text=f"{The} breaks and runs.")
            end = "routed"
        beats.append(beat)
    if not end and turns >= COMBAT_MAX_ROUNDS:
        beats.append({"side": "foe", "kind": "end", "result": "routed",
                      "text": f"{The} has had enough, and backs off into the dark.",
                      "sub": "", "damage": 0, "target": None})
        end = "routed"
    if end == "ko":
        beats.append({"side": "foe", "kind": "end", "result": "ko",
                      "text": f"{The} goes down.", "sub": "", "damage": 0,
                      "target": "foe", "hp_after": 0})

    after = dict(rec, foe=foe, turns=turns, talk_tries=talk_tries, saves=saves,
                 fumbled=fumbled)
    after.pop("pending", None)
    exchange = {
        "id": uuid.uuid4().hex[:12],
        "round": turns,
        "turn": turns,
        "lane": lane,
        "lane_word": LANE_WORDS[lane],
        "verb": str(verb or "")[:200],
        "edge": {"adv": int(edge or 0), "why": edge_why} if edge else None,
        "you": {"name": (rec.get("you") or {}).get("name") or "YOU", "hp_before": hp0,
                "hp": max(0, hp), "max": ymax},
        "foe": {"name": foe.get("name"), "hp_before": fhp0, "hp": fhp,
                "max": int(foe.get("max") or 1), "move": MOVE},
        "beats": beats,
        "end": end,
    }
    after["last"] = exchange
    alive = end != "dead"
    outcome = ("die" if not alive else "escape" if end == "escaped"
               else "wounded" if hp < hp0 else "survive")
    enemy_state = ("down" if end == "ko" else "standing_down" if end in ("settled", "routed")
                   else "staggered" if bloodied(foe) else "ready")
    return {"exchange": exchange, "after": after, "outcome": outcome, "alive": alive,
            "condition": condition_for(max(0, hp), ymax), "enemy_state": enemy_state,
            "armor_used": armor_used, "end": end}


def hud(combat: Optional[dict], you_hp: int) -> dict:
    """The two bars, for a response that opens or continues a fight."""
    c = combat if isinstance(combat, dict) else {}
    you = c.get("you") or {}
    foe = c.get("foe") or {}
    return {
        "you": {"name": you.get("name") or "YOU", "hp": int(you_hp),
                "max": int(you.get("max") or PLAYER_MAX_HP)},
        "foe": {"name": foe.get("name") or "STRANGER",
                "hp": int(foe.get("hp") if foe.get("hp") is not None else 1),
                "max": int(foe.get("max") or 1), "ac": foe.get("ac"),
                "move": foe.get("move") or "STRIKE"},
        "turns": int(c.get("turns") or 0),
        "opening": c.get("opening") or "",
        "initiative": initiative_beat(c) if c.get("init") and not int(c.get("turns") or 0) else None,
    }


def odds_on_slate(choices: Any, odds: Dict[str, int]) -> list:
    """Stamp each lane's odds on the slate the client draws."""
    out = []
    for c in choices or []:
        if isinstance(c, dict):
            c = dict(c)
            lane = str(c.get("lane") or "")
            if lane in odds:
                c["odds"] = int(odds[lane])
        out.append(c)
    return out
