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

#: Supply a worker sends per turn from a node inside depot range.
HARVEST_RATE = 4

#: How far a worker can transmit to a Command Post or Supply Depot.
HARVEST_RADIUS = 5

#: Army supported by a Command Post alone, and by each Supply Depot.
#:
#: Engineers count against this, which is the tension that makes economy a
#: decision -- but the first numbers here were carried over from a roster with
#: no workers in it, so three Engineers ate a third of the army and neither
#: side could ever field enough to crack a Command Post. Matches went from a
#: 32-turn median to unresolved. The base is now sized for an army *including*
#: its labour.
#:
#: The ceiling used to sit at 24, three Depots' worth, and that turned out to
#: be where a real game stops being about decisions: by the time both sides
#: have an economy running, every match is two full armies of exactly the same
#: size trading into each other with a thousand supply banked and nowhere to
#: put it. Raising it to 40 costs nothing in pacing -- measured across five
#: matchups, match length and win rates were identical at 24, 32, 40 and 64 --
#: because what actually ends a match is the economy, not the ceiling. It is
#: still a ceiling, though: resolution cost grows with the square of the units
#: on the board, and 40 a side is about where a Pi 400 can still resolve a turn
#: without a visible pause.
ARMY_CAP_BASE = 12
DEPOT_CAP = 4
ARMY_CAP_MAX = 40

#: Units that can properly demolish a barricade. Everyone else can chip at it,
#: at a quarter rate -- enough that a wall is never an absolute full stop, far
#: too slow to be the answer to one.
BREACHERS = frozenset({"bruiser", "worker"})
WALL_CHIP_DIVISOR = 4

#: What an Engineer does to a structure. Its attack stat governs only its
#: (pitiful) ability to shoot people.
WORKER_DEMOLISH = 4

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
    builder: bool = False   # can raise structures and harvest nodes
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
    supply_cap: int = 0     # how much army this structure supports
    harvests: bool = False  # can receive a worker's transmissions
    attack: int = 0         # sentry towers shoot; nothing else does
    reach: int = 0
    wall: bool = False
    buildable: bool = True  # false for the Command Post, which you start with
    blurb: str = ""


UNITS: tuple[UnitType, ...] = (
    UnitType("worker", "Engineer", cost=3, build_turns=1, speed=2, hp=10,
             attack=1, reach=1, vision=4, initiative=1, beats="",
             built_at="base", builder=True,
             blurb="Raises structures. Works a node inside depot range."),
    UnitType("scout", "Scout", cost=3, build_turns=1, speed=3, hp=12, attack=2,
             reach=1, vision=6, initiative=0, beats="ranged", built_at="base",
             blurb="Fast, blind-spot filler. Loses every fair fight."),
    UnitType("trooper", "Trooper", cost=5, build_turns=2, speed=2, hp=24,
             attack=4, reach=1, vision=4, initiative=3, beats="",
             built_at="base",
             blurb="The safe pick. Beaten by nothing, beats nothing."),
    UnitType("ranged", "Gunner", cost=7, build_turns=2, speed=2, hp=15,
             attack=5, reach=2, vision=5, initiative=2, beats="bruiser",
             built_at="barracks",
             blurb="Outranges everything. Dies if anything reaches it."),
    UnitType("bruiser", "Bruiser", cost=9, build_turns=3, speed=1, hp=48,
             attack=6, reach=1, vision=3, initiative=4, beats="scout",
             built_at="barracks",
             blurb="A wall that walks. Slowly."),
)

BUILDINGS: tuple[BuildingType, ...] = (
    BuildingType("base", "Command Post", cost=0, build_turns=0, hp=90,
                 vision=6, produces=("worker", "scout", "trooper"), income=2,
                 supply_cap=ARMY_CAP_BASE, harvests=True, buildable=False,
                 blurb="Lose it and you are out. Also receives supply."),
    BuildingType("barracks", "Barracks", cost=10, build_turns=3, hp=60,
                 vision=4, produces=("ranged", "bruiser"),
                 blurb="Unlocks Gunners and Bruisers."),
    BuildingType("depot", "Supply Depot", cost=8, build_turns=2, hp=50,
                 vision=4, supply_cap=DEPOT_CAP, harvests=True,
                 blurb="+3 army cap. Workers within 5 tiles can send supply."),
    BuildingType("tower", "Sentry Tower", cost=8, build_turns=2, hp=40,
                 vision=5, attack=4, reach=3,
                 blurb="Shoots three tiles. Never moves."),
    BuildingType("wall", "Barricade", cost=2, build_turns=1, hp=30,
                 vision=0, wall=True,
                 blurb="Blocks the way. Bruisers and Engineers break it fast."),
)

UNIT = {u.code: u for u in UNITS}
BUILDING = {b.code: b for b in BUILDINGS}

#: The army cap is no longer flat: it starts at ARMY_CAP_BASE and grows with
#: Supply Depots, so an economy buys you a bigger army rather than merely a
#: faster-rebuilt one. Some sort of cap is essential either way -- without one,
#: two even armies reinforce exactly as fast as they die and the match never
#: ends, measured at a median of 124 turns before the flat cap existed.

#: New buildings must be placed within this many tiles of one you already own,
#: so the front line means something and nobody teleports a barracks behind
#: your lines.
BUILD_RADIUS = 4


def damage_between(attacker: str, defender: str, bonus: int = 0) -> int:
    """Damage one shot from ``attacker`` does to ``defender``."""
    base = UNIT[attacker].attack + bonus
    if UNIT[attacker].beats == defender:
        return max(1, base * COUNTER_BONUS_NUM // COUNTER_BONUS_DEN)
    if UNIT[defender].beats == attacker:
        return max(1, base * COUNTERED_PENALTY_NUM // COUNTERED_PENALTY_DEN)
    return base


def damage_to_building(attacker: str, building_code: str = "",
                       bonus: int = 0) -> int:
    """Buildings do not participate in the counter triangle.

    Barricades are the exception: Bruisers and Engineers tear them down, and
    everyone else can only chip away.
    """
    damage = UNIT[attacker].attack + bonus
    if UNIT[attacker].builder:
        damage = WORKER_DEMOLISH + bonus
    if building_code and BUILDING[building_code].wall:
        if attacker not in BREACHERS:
            return max(1, damage // WALL_CHIP_DIVISOR)
    return max(1, damage)


def buildable_units(building_codes) -> list[str]:
    """Unit codes available given the set of building types a player owns."""
    out = []
    for unit in UNITS:
        if unit.built_at in building_codes:
            out.append(unit.code)
    return out


# ---------------------------------------------------------------------------
# Research. Conducted at the Command Post, one project at a time, permanent
# and army-wide. Its real job is to be somewhere for a healthy economy to
# spend, so that surplus supply buys quality once quantity is capped.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Research:
    code: str
    name: str
    cost: int
    turns: int
    requires: str = ""
    blurb: str = ""


RESEARCH: tuple[Research, ...] = (
    Research("weapons1", "Weapons I", 15, 3, blurb="+1 attack, every unit."),
    Research("weapons2", "Weapons II", 30, 4, requires="weapons1",
             blurb="+1 attack again."),
    Research("armour1", "Armour I", 15, 3, blurb="+4 health, every unit."),
    Research("armour2", "Armour II", 30, 4, requires="armour1",
             blurb="+4 health again."),
    Research("logistics", "Logistics", 20, 3,
             blurb="+2 supply from every working Engineer."),
    Research("engineering", "Engineering", 15, 2,
             blurb="Structures finish a turn sooner. Barricades cost 1."),
)
RESEARCH_BY_CODE = {r.code: r for r in RESEARCH}

ATTACK_BONUS = {"weapons1": 1, "weapons2": 1}
HP_BONUS = {"armour1": 4, "armour2": 4}
LOGISTICS_BONUS = 2


def attack_bonus(done) -> int:
    return sum(value for code, value in ATTACK_BONUS.items() if code in done)


def hp_bonus(done) -> int:
    return sum(value for code, value in HP_BONUS.items() if code in done)


def harvest_rate(done) -> int:
    return HARVEST_RATE + (LOGISTICS_BONUS if "logistics" in done else 0)


def build_turns_for(code: str, done) -> int:
    """Construction time after Engineering, never below one turn."""
    base = BUILDING[code].build_turns
    if "engineering" in done:
        return max(1, base - 1)
    return base


def cost_of_building(code: str, done) -> int:
    cost = BUILDING[code].cost
    if code == "wall" and "engineering" in done:
        return 1
    return cost


def available_research(done) -> list:
    """Projects whose prerequisites are met and which are not already done."""
    return [r for r in RESEARCH
            if r.code not in done and (not r.requires or r.requires in done)]


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
        if not building.buildable:
            continue
        rows.append({"code": building.code, "name": building.name,
                     "kind": "building", "cost": building.cost,
                     "turns": building.build_turns, "hp": building.hp,
                     "blurb": building.blurb})
    for project in RESEARCH:
        rows.append({"code": project.code, "name": project.name,
                     "kind": "research", "cost": project.cost,
                     "turns": project.turns, "requires": project.requires,
                     "blurb": project.blurb})
    return rows


def army_cap(structure_codes) -> int:
    """Army supported by the structures a player currently has standing."""
    total = sum(BUILDING[code].supply_cap for code in structure_codes)
    return min(ARMY_CAP_MAX, total)
