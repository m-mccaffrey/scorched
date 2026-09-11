"""Running bot-versus-bot matches for the parameter search.

Two things matter here and nothing else does.

**Common random numbers.** Every candidate plays the *same* seeds, so two
candidates are compared on the same games rather than on their luck. With eight
matches a pair the absolute win rate is far too noisy to trust, but the
*difference* between two candidates on identical seeds is not, which is what a
search actually needs. The cost is that a candidate can overfit to its seeds,
so the winner is re-checked on seeds it has never played.

**Side swapping.** Spawns are not identical, so every pairing is played from
both sides an equal number of times. Without that the search would happily
learn which corner of the map is better.
"""

from __future__ import annotations

import random

from standing_orders.ai import SKILL_ORDER, BotBrain
from standing_orders.game import PHASE_OVER, Match, Settings

#: Turns after which a match is called unfinished.
#:
#: Past the 135-turn median of the slowest shipped matchup, so a cap still
#: means "these two ground each other down" rather than "we ran out of
#: patience" -- but not so far past that a single bad candidate can spend
#: minutes proving it. Search cost is dominated by exactly the candidates
#: that never resolve anything, and those are being selected against anyway.
TURN_CAP = 140

#: Every ordered pair of tiers, with the win rate the stronger one should be
#: managing. A ladder wants daylight between neighbours and a rout across the
#: whole length of it.
TARGETS = {
    ("novice", "moderate"): 0.70,
    ("moderate", "veteran"): 0.70,
    ("veteran", "cyborg"): 0.70,
    ("novice", "veteran"): 0.85,
    ("moderate", "cyborg"): 0.85,
    ("novice", "cyborg"): 0.95,
}

#: The two pairings that are known to be broken today carry more weight: the
#: rest already work, and a search that spends its budget polishing them has
#: not fixed anything.
WEIGHTS = {
    ("moderate", "veteran"): 3.0,
    ("veteran", "cyborg"): 3.0,
    ("moderate", "cyborg"): 2.0,
}

#: An unfinished match is a bad match whoever "wins" it. Weighted to matter
#: without dominating: the ladder is the objective, pace is a constraint.
UNRESOLVED_PENALTY = 1.5


def play(weaker: str, stronger: str, seed: int, swap: bool,
         map_name: str = "duel") -> tuple[int, int]:
    """One match. Returns (stronger won, match was unfinished) as 0/1."""
    random.seed(seed)
    match = Match(Settings(map_name=map_name))
    line_up = [stronger, weaker] if not swap else [weaker, stronger]
    for index, skill in enumerate(line_up):
        match.add_player("AB"[index], bot=True, skill=skill)
    match.start_match()
    brains = {p.pid: BotBrain(p.skill, random.Random(seed * 97 + p.pid))
              for p in match.state.players.values()}

    turns = 0
    while match.phase != PHASE_OVER and turns < TURN_CAP:
        for player in list(match.state.players.values()):
            if player.alive:
                match.submit(player.pid, brains[player.pid].plan(match, player))
        match.resolve(timelines=False)
        turns += 1
        if not match.begin_orders():
            break

    alive = [p for p in match.state.players.values() if p.alive]
    if match.phase != PHASE_OVER or len(alive) != 1:
        return 0, 1
    return (1 if alive[0].skill == stronger else 0), 0


#: How many seeds each pairing is worth, as a multiple of the base budget.
#:
#: The three Novice pairings are already 8-0 and cost a match each to confirm;
#: the three that are broken are the ones a search needs resolution on. Eight
#: matches can only measure a win rate to the nearest 12%, which is coarse
#: next to a 70% target, so the budget goes where the information is rather
#: than being spread evenly over questions already answered.
SEED_SHARE = {
    ("novice", "moderate"): 1,
    ("novice", "veteran"): 1,
    ("novice", "cyborg"): 1,
    ("moderate", "veteran"): 3,
    ("veteran", "cyborg"): 3,
    ("moderate", "cyborg"): 3,
}


def jobs(seeds) -> list[tuple[str, str, int, bool]]:
    """Every match one evaluation consists of, in a fixed order."""
    seeds = list(seeds)
    out = []
    for pair in TARGETS:
        share = SEED_SHARE.get(pair, 1)
        for seed in seeds[:max(1, len(seeds) * share // 3)]:
            for swap in (False, True):
                out.append((pair[0], pair[1], seed, swap))
    return out


def score(results: dict) -> tuple[float, dict]:
    """Turn per-pair tallies into a loss to minimise, plus a readable report.

    Falling short of a target is punished; overshooting it is free. A Novice
    that loses every single game is not a problem to be fixed.
    """
    loss = 0.0
    report = {}
    total_unresolved = 0
    total_matches = 0
    for pair, target in TARGETS.items():
        wins, played, unfinished = results.get(pair, (0, 0, 0))
        rate = wins / played if played else 0.0
        shortfall = max(0.0, target - rate)
        loss += WEIGHTS.get(pair, 1.0) * shortfall ** 2
        report[pair] = (rate, target, played, unfinished)
        total_unresolved += unfinished
        total_matches += played
    stalled = total_unresolved / total_matches if total_matches else 0.0
    loss += UNRESOLVED_PENALTY * stalled ** 2
    report["unresolved"] = stalled
    return loss, report


def format_report(report: dict) -> str:
    rows = []
    for pair, target in TARGETS.items():
        rate, target, played, unfinished = report[pair]
        flag = "ok " if rate >= target else "SHORT"
        rows.append(f"    {pair[1]:8s} over {pair[0]:8s} {rate:5.0%} "
                    f"(want {target:.0%}) {flag} n={played} unfinished={unfinished}")
    rows.append(f"    unfinished overall: {report['unresolved']:.0%}")
    return "\n".join(rows)


def ladder_holds(report: dict) -> bool:
    """Is every tier actually beating the one below it?"""
    return all(report[(a, b)][0] > 0.5
               for a, b in zip(SKILL_ORDER, SKILL_ORDER[1:]))
