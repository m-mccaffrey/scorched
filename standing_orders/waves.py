"""Holdout mode: the schedule the Swarm arrives on, and who is in it.

Holdout is the co-operative scenario. Every commander is on the same side and
the enemy is the map itself: waves walk in at the gates and march on the
nearest Command Post. Nobody is eliminated by another player, and everybody
loses together when the last Command Post falls.

Almost none of this is new machinery. The Swarm is an ordinary player that no
human sits behind; its units are the same five the rest of the game uses; it
walks with the same pathfinder and shoots with the same combat rules. A
Barricade in its way is a building in reach, and buildings in reach get shot at
-- so mazing works, and walls are consumable rather than absolute, without a
single rule being written for it. The champions are ordinary Bruisers with the
rank the promotion system already grants, which means a boss turns up carrying
an officer's aura and buffing its own escort for free.

**The schedule is fixed, not random.** A family learns it -- "the Gunners come
on five" -- and learning it is most of what makes a tower defence fun. Two
sessions on the same map are the same problem, which is the point: you get
better at it.
"""

from __future__ import annotations

from .state import MAX_PLAYERS, SWARM_PID
from .units import UNIT, max_rank

__all__ = ["SWARM_PID", "SWARM_NAME", "SWARM_COLOR", "schedule", "compose",
           "wave_budget", "wave_strength", "bounty_for", "muster_pay",
           "veterancy", "FIRST_WAVE", "WAVE_GAP"]

SWARM_NAME = "The Swarm"

#: Index into the shared team palette. Commanders are only ever given 0..3, so
#: the bone-white at the end of it is the Swarm's alone.
SWARM_COLOR = 7

#: Turn the first wave arrives, and the gap between waves after that.
#:
#: Three turns of quiet to open with is enough for two Engineers to put up a
#: Depot and a first tower, and not enough to feel like the game has not
#: started. The gap is the whole tempo of the mode: at three turns a wave that
#: is not dealt with is still on the board when the next one walks in, which is
#: exactly the pressure a tower defence runs on.
FIRST_WAVE = 3
WAVE_GAP = 3

#: What a wave is worth, in the same supply the players spend.
#:
#: Quadratic rather than linear on purpose. A linear ramp means the first wave
#: that troubles you is also roughly the last one that can, because tower
#: coverage compounds and a wave schedule that does not compound with it stops
#: mattering around wave six.
WAVE_BASE = 10
WAVE_STEP = 8
WAVE_CURVE = 0.5

#: Waves every this many apart come a rank harder, to a ceiling of
#: ``max_rank()``. This is the difficulty curve; the body count is only the
#: shape of it.
#:
#: Spending a bigger budget on more of the same creep does not make a wave
#: harder here, and measurement was emphatic about it: across four seeds, wave
#: curves twice and four times as steep were *easier* to survive than the
#: shallow one -- held 4 of 4 against 1 of 4 -- because a chokepoint kills the
#: twentieth Trooper exactly as cheaply as the fifth while the bounty on it
#: pays for another tower. More bodies through a doorway is more income for the
#: defence. What actually breaks a line is something that survives the doorway,
#: so the curve is carried by rank: +1 attack and +4 health a rung, and at the
#: top rung the creep leads its own escort with an officer's aura.
VETERANCY_EVERY = 3

#: Every fifth wave is a champion wave: a Lieutenant at the head of it. Rank 3
#: is worth +3 attack and +12 health and, because it is the rank that carries
#: an officer's aura, the escort around it hits harder too.
CHAMPION_EVERY = 5
CHAMPION_RANK = 3
CHAMPION_CODE = "bruiser"

#: What a wave is made of as it gets later: (code, first wave, last wave,
#: weight), where a last wave of 0 means "from then on".
#:
#: The rabble retires. Spending a growing budget on a fixed mix makes a late
#: wave *more numerous* rather than harder -- wave twelve came out as thirteen
#: Scouts, which a single tower mows down -- so the cheap fast bodies stop
#: coming once the armour starts, and the curve turns into weight of metal.
#:
#: Gunners reach two tiles, which is one short of a Sentry Tower, and Mortar
#: Teams reach four, which is one *past* it. They are the answer to a line of
#: masonry, and the reason the later waves are dangerous rather than merely
#: large: a Mortar shells a Sentry Tower to rubble from a tile it cannot be
#: shot back from, so from wave eight a static defence has to be defended.
ROSTER = (
    ("scout", 1, 7, 3),
    ("trooper", 1, 0, 3),
    ("bruiser", 3, 0, 3),
    ("ranged", 6, 0, 2),
    ("siege", 8, 0, 3),
)

#: Supply every commander is paid as a wave musters, and the share of a
#: creep's cost paid to whoever killed it.
#:
#: The bounty is what makes a tower pay for itself, and it is paid to the
#: killer rather than the team so that building the guns that do the work is
#: rewarded. The stipend is paid to everyone so that a commander who spent the
#: wave rebuilding a wall is not left behind by one who got the kills.
#:
#: Below what a creep cost to field, and deliberately: a bounty that pays back
#: more than the wave was worth turns every escalation into a subsidy.
#:
#: The muster is paid when the wave *arrives*, not when it is cleared. Paying
#: on a clear looks fairer and is unplayable: from about wave six the waves
#: overlap, the board is never empty again, and the income simply stops -- at
#: exactly the point in the match where a defence most needs to be spending.
#: A wave you can see coming is a wage you can plan around. A Holdout map
#: puts its resource nodes outside the wall, so the ground economy is a risk
#: you take rather than an income you can count on -- the waves themselves have
#: to pay for the defence or there is no defence. At a bounty of one-for-one
#: bots finished a twelve-wave match with two towers and an army of fifteen.
MUSTER_PAY = 8
MUSTER_SHARE = 6
BOUNTY_SHARE = 0.7


def wave_budget(number: int, players: int) -> int:
    """What wave ``number`` (1-based) is worth against ``players`` commanders."""
    raw = WAVE_BASE + WAVE_STEP * number + WAVE_CURVE * number * number
    # Two commanders should not face what four can handle, but nor should the
    # wave shrink to nothing: half the table is well over half the defence,
    # because towers do not go home when a player leaves.
    share = (max(1, min(MAX_PLAYERS, players)) + 2) / (MAX_PLAYERS + 2)
    return max(UNIT["scout"].cost, int(raw * share))


def compose(number: int, players: int) -> list:
    """The pack for one wave, as [[code, rank, count], ...].

    Spent down from a budget in the order the roster lists, cheapest first, so
    an early wave is a crowd of Scouts and a late one is a wall of Bruisers
    with Gunners behind it.
    """
    budget = wave_budget(number, players)
    rank = veterancy(number)
    pack: list = []

    if number % CHAMPION_EVERY == 0:
        champion = UNIT[CHAMPION_CODE]
        if budget >= champion.cost:
            pack.append([CHAMPION_CODE, CHAMPION_RANK, 1])
            budget -= champion.cost * 2      # a champion costs double its keep

    available = [(code, weight) for code, first, until, weight in ROSTER
                 if number >= first and (not until or number <= until)]
    total = sum(weight for _code, weight in available) or 1
    for code, weight in available:
        cost = UNIT[code].cost
        count = int(budget * weight / total) // cost
        if count:
            pack.append([code, rank, count])
    if not pack:
        pack.append(["scout", rank, 1])
    return pack


def veterancy(number: int) -> int:
    """How hardened wave ``number`` turns up: 0 for the first few, then up."""
    return min(max_rank(), (max(1, number) - 1) // VETERANCY_EVERY)


def schedule(count: int, players: int) -> list:
    """The whole match, as a list of waves the state can carry on the wire."""
    return [{"n": n, "at": FIRST_WAVE + (n - 1) * WAVE_GAP,
             "pack": compose(n, players)}
            for n in range(1, max(1, int(count)) + 1)]


def wave_strength(wave: dict) -> int:
    """What a wave is worth in supply -- used to price the clear stipend."""
    return sum(UNIT[code].cost * count for code, _rank, count in wave["pack"])


def bounty_for(code: str) -> int:
    """What killing one of these pays the commander who did it."""
    return max(1, int(UNIT[code].cost * BOUNTY_SHARE))


def muster_pay(wave: dict) -> int:
    """What every commander still standing is paid as this wave walks in."""
    return MUSTER_PAY + wave_strength(wave) // MUSTER_SHARE
