"""The bot's tunable numbers, in one place, so a search can drive them.

Nothing here is imported by the game. It exists so that the offline parameter
search and the game agree on exactly which knobs exist, what each one means and
what range is sane for it -- and so that applying a candidate is one call
rather than a scatter of monkeypatching.
"""

from __future__ import annotations

import dataclasses

import standing_orders.ai as ai

#: Module-level constants shared by every skill tier: name -> (low, high, int?)
#:
#: These do not separate the tiers -- every bot reads the same value -- but
#: they set how well the shared machinery works, and a threshold tuned for a
#: cautious bot can be quietly wrong for an aggressive one.
SHARED = {
    "BARRACKS_BUFFER":     (0, 15, True),
    "EXPAND_SURPLUS":      (5, 60, True),
    "RESEARCH_BUFFER":     (5, 60, True),
    "DEFEND_RADIUS":       (4, 12, True),
    "SUPPORT_BUFFER":      (10, 100, True),
    "PROMOTIONS_PER_TURN": (1, 4, True),
    "PROMOTE_BUFFER":      (10, 120, True),
    "WOUNDED_SHARE":       (0.2, 0.9, False),
    "STRIKE_WORTH":        (1, 4, True),
    "STRIKE_RESERVE":      (0, 40, True),
    "PRESS_ADVANTAGE":     (0.9, 2.0, False),
    "RALLY_RADIUS":        (3, 10, True),
    "CONTACT_DISTANCE":    (2, 10, True),
    "MIN_THREAT":          (2, 20, True),
    "THREAT_SHARE":        (0.05, 0.70, False),
}

#: Per-tier fields of the Skill record: name -> (low, high, int?)
#:
#: The booleans (defends, researches, expands) are deliberately left out. They
#: are what a difficulty level *is* -- a Novice that researches is not a
#: Novice -- so they are design, not tuning. Only the numbers are searched.
PER_TIER = {
    "mass_at":      (2, 10, True),
    "counter_pick": (0.0, 1.0, False),
    "workers":      (1, 6, True),
    "barracks":     (1, 4, True),
    "supports":     (0, 2, True),
}

#: Fields that may only ever go *up* the ladder, expressed as a rung plus
#: non-negative steps rather than as four free numbers.
#:
#: Left unconstrained, the search produced a table that met every win-rate
#: target and meant nothing: Novice running five Engineers to Cyborg's two,
#: Novice counter-picking at 0.90 against Moderate's 0.24, and a supports
#: column reading 1, 0, 1, 1. Win rates were all the objective ever asked
#: about, so that is all it delivered. A difficulty tier has to *be* more than
#: the one below it, not merely beat it.
MONOTONE = ("counter_pick", "workers", "barracks", "supports")

#: Fields left free per tier, because more is not obviously better and the
#: whole question is open. The first search wanted Veteran at 3 rather than 6
#: -- less cautious, not more -- and caution is not competence.
FREE = ("mass_at",)

TIERS = ai.SKILL_ORDER

#: Novice is pinned to what it ships with. It is the reference for "gentle",
#: and an objective that only scores the ordering will happily make the
#: beginners' bot stronger so long as everyone else rises faster. The first
#: search did exactly that, taking Novice from one Engineer to five.
ANCHOR = TIERS[0]


def spec() -> list[tuple[str, float, float, bool]]:
    """Every searchable knob as (key, low, high, integral), in a fixed order.

    The per-tier entries are *steps*, not values: what the search moves for a
    monotone field is how much further up the ladder each rung sits, which can
    never be negative. The tier table is then ordered by construction rather
    than by hoping the objective notices.
    """
    out = [(name, lo, hi, whole) for name, (lo, hi, whole) in SHARED.items()]
    for tier in TIERS:
        if tier == ANCHOR:
            continue
        for name in FREE:
            low, high, whole = PER_TIER[name]
            out.append((f"{tier}.{name}", low, high, whole))
        for name in MONOTONE:
            low, high, whole = PER_TIER[name]
            out.append((f"{tier}.{name}+", 0.0, high - low, whole))
    return out


def baseline() -> dict:
    """What the bot ships with today -- the point the search starts from."""
    values = {name: getattr(ai, name) for name in SHARED}
    for index, tier in enumerate(TIERS):
        if tier == ANCHOR:
            continue
        for name in FREE:
            values[f"{tier}.{name}"] = getattr(ai.SKILLS[tier], name)
        for name in MONOTONE:
            below = getattr(ai.SKILLS[TIERS[index - 1]], name)
            values[f"{tier}.{name}+"] = max(0, getattr(ai.SKILLS[tier], name)
                                            - below)
    return values


def clamp(values: dict) -> dict:
    """Pull a candidate back inside its declared range and round the integers."""
    out = {}
    for key, low, high, whole in spec():
        value = min(high, max(low, values.get(key, low)))
        out[key] = int(round(value)) if whole else float(value)
    return out


def profiles(values: dict) -> dict:
    """Turn a candidate's steps into the four actual Skill records."""
    values = clamp(values)
    running = {name: getattr(ai.SKILLS[ANCHOR], name) for name in MONOTONE}
    out = {}
    for tier in TIERS:
        if tier == ANCHOR:
            out[tier] = ai.SKILLS[ANCHOR]
            continue
        changes = {name: values[f"{tier}.{name}"] for name in FREE}
        for name in MONOTONE:
            low, high, whole = PER_TIER[name]
            running[name] = min(high, running[name]
                                + values[f"{tier}.{name}+"])
            changes[name] = (int(round(running[name])) if whole
                             else float(running[name]))
        out[tier] = dataclasses.replace(ai.SKILLS[tier], **changes)
    return out


def apply(values: dict) -> None:
    """Install a candidate into the live ``ai`` module. Process-local."""
    for name in SHARED:
        setattr(ai, name, clamp(values)[name])
    ai.SKILLS = profiles(values)


def describe(values: dict, against: dict) -> list[str]:
    """Lines for every knob the candidate actually moved."""
    lines = []
    for key, _low, _high, whole in spec():
        was, now = against.get(key), values.get(key)
        if was == now:
            continue
        fmt = "{:g}".format
        lines.append(f"  {key:28s} {fmt(was):>8s} -> {fmt(now):>8s}")
    return lines
