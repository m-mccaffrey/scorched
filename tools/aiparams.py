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

TIERS = ai.SKILL_ORDER


def spec() -> list[tuple[str, float, float, bool]]:
    """Every searchable knob as (key, low, high, integral), in a fixed order."""
    out = [(name, lo, hi, whole) for name, (lo, hi, whole) in SHARED.items()]
    for tier in TIERS:
        for name, (lo, hi, whole) in PER_TIER.items():
            out.append((f"{tier}.{name}", lo, hi, whole))
    return out


def baseline() -> dict:
    """What the bot ships with today -- the point the search starts from."""
    values = {name: getattr(ai, name) for name in SHARED}
    for tier in TIERS:
        for name in PER_TIER:
            values[f"{tier}.{name}"] = getattr(ai.SKILLS[tier], name)
    return values


def clamp(values: dict) -> dict:
    """Pull a candidate back inside its declared range and round the integers."""
    out = {}
    for key, low, high, whole in spec():
        value = min(high, max(low, values.get(key, low)))
        out[key] = int(round(value)) if whole else float(value)
    return out


def apply(values: dict) -> None:
    """Install a candidate into the live ``ai`` module. Process-local."""
    values = clamp(values)
    for name in SHARED:
        setattr(ai, name, values[name])
    skills = {}
    for tier in TIERS:
        changes = {name: values[f"{tier}.{name}"] for name in PER_TIER}
        skills[tier] = dataclasses.replace(ai.SKILLS[tier], **changes)
    ai.SKILLS = skills


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
