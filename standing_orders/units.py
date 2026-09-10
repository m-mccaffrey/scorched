"""Unit and building data.

A deliberately small roster. Three units form a rock-paper-scissors triangle
and the fourth is an honest generalist, which is enough for composition to be
a real decision without anyone needing a wiki open.

    Scout  beats  Ranged  beats  Bruiser  beats  Scout
    Trooper is even against everything.

Health values are three times what a first sketch would suggest because a unit
that stays engaged for a whole turn attacks three times: the ratios between
units are what matter, and this way most fights take more than one turn, so
reinforcements and retreats mean something.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Damage multipliers applied when the counter triangle is in play. Integer
#: arithmetic throughout -- halves and three-halves, never floats.
COUNTER_BONUS_NUM, COUNTER_BONUS_DEN = 3, 2
COUNTERED_PENALTY_NUM, COUNTERED_PENALTY_DEN = 1, 2


@dataclass(frozen=True)
class UnitType:
    code: str
    name: str
    cost: int
    build_turns: int
    speed: int              # tiles of open ground per turn
    hp: int
    attack: int             # damage per shot; three shots per engaged turn
    reach: int              # attack range in tiles (1 = adjacent)
    vision: int
    initiative: int         # lower acts first when two units contest a tile
    beats: str = ""         # unit code this one counters
    built_at: str = "base"
    blurb: str = ""


@dataclass(frozen=True)
class BuildingType:
    code: str
    name: str
    cost: int
    build_turns: int
    hp: int
    vision: int
    produces: tuple = ()
    income: int = 0
    blurb: str = ""


UNITS: tuple[UnitType, ...] = (
    UnitType("scout", "Scout", cost=3, build_turns=1, speed=3, hp=12, attack=2,
             reach=1, vision=6, initiative=0, beats="ranged", built_at="base",
             blurb="Fast, blind-spot filler. Loses every fair fight."),
    UnitType("trooper", "Trooper", cost=5, build_turns=2, speed=2, hp=24,
             attack=4, reach=1, vision=4, initiative=2, beats="",
             built_at="base",
             blurb="The safe pick. Beaten by nothing, beats nothing."),
    UnitType("ranged", "Gunner", cost=7, build_turns=2, speed=2, hp=15,
             attack=5, reach=2, vision=5, initiative=1, beats="bruiser",
             built_at="barracks",
             blurb="Outranges everything. Dies if anything reaches it."),
    UnitType("bruiser", "Bruiser", cost=9, build_turns=3, speed=1, hp=48,
             attack=6, reach=1, vision=3, initiative=3, beats="scout",
             built_at="barracks",
             blurb="A wall that walks. Slowly."),
)

BUILDINGS: tuple[BuildingType, ...] = (
    BuildingType("base", "Command Post", cost=0, build_turns=0, hp=90,
                 vision=6, produces=("scout", "trooper"), income=2,
                 blurb="Lose it and you are out."),
    BuildingType("barracks", "Barracks", cost=10, build_turns=3, hp=60,
                 vision=4, produces=("ranged", "bruiser"), income=0,
                 blurb="Unlocks Gunners and Bruisers."),
)

UNIT = {u.code: u for u in UNITS}
BUILDING = {b.code: b for b in BUILDINGS}

#: Supply earned per turn for holding a resource node.
NODE_INCOME = 3

#: Maximum living units one commander may field.
#:
#: This is the single most important number in the game. Without a cap, two
#: even armies reinforce exactly as fast as they die and the match never ends
#: -- measured at a median of 124 turns before this existed. With it, losing a
#: battle actually costs you something, because you cannot buy the army back
#: while the winner is marching on your Command Post.
UNIT_CAP = 12

#: New buildings must be placed within this many tiles of one you already own,
#: so the front line means something and nobody teleports a barracks behind
#: your lines.
BUILD_RADIUS = 4


def damage_between(attacker: str, defender: str) -> int:
    """Damage one shot from ``attacker`` does to ``defender``."""
    base = UNIT[attacker].attack
    if UNIT[attacker].beats == defender:
        return max(1, base * COUNTER_BONUS_NUM // COUNTER_BONUS_DEN)
    if UNIT[defender].beats == attacker:
        return max(1, base * COUNTERED_PENALTY_NUM // COUNTERED_PENALTY_DEN)
    return base


def damage_to_building(attacker: str) -> int:
    """Buildings do not participate in the counter triangle."""
    return UNIT[attacker].attack


def buildable_units(building_codes) -> list[str]:
    """Unit codes available given the set of building types a player owns."""
    out = []
    for unit in UNITS:
        if unit.built_at in building_codes:
            out.append(unit.code)
    return out


def catalogue() -> list[dict]:
    """Everything a client needs to draw build menus without knowing rules."""
    rows = []
    for unit in UNITS:
        rows.append({"code": unit.code, "name": unit.name, "kind": "unit",
                     "cost": unit.cost, "turns": unit.build_turns,
                     "hp": unit.hp, "attack": unit.attack, "speed": unit.speed,
                     "reach": unit.reach, "beats": unit.beats,
                     "built_at": unit.built_at, "blurb": unit.blurb})
    for building in BUILDINGS:
        if building.code == "base":
            continue
        rows.append({"code": building.code, "name": building.name,
                     "kind": "building", "cost": building.cost,
                     "turns": building.build_turns, "hp": building.hp,
                     "blurb": building.blurb})
    return rows
