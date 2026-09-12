"""Run bot-only games and report what actually happened.

    python3 -m tools.playtest --map reach --games 8
    python3 -m tools.playtest --all --games 6 --workers 4

The parameter search in ``tune.py`` answers one narrow question -- are the
difficulty tiers ordered -- by reducing a match to a win or a loss. This
answers the broader one: is a game on this map any good? It reports how long
matches run, how they end, how close they finish, how much of the map anybody
ever sees, and what it cost to resolve a turn.

Nothing here is imported by the game.
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import random
import statistics
import time

from standing_orders.ai import BotBrain
from standing_orders.game import PHASE_OVER, Match, Settings

#: Rosters worth asking about. A spread of skills is the normal case; the
#: mirror is the hardest thing for any symmetric AI and the usual source of
#: matches that never end.
ROSTERS = {
    "mixed": ("veteran", "moderate", "moderate", "novice"),
    "mirror": ("veteran", "veteran", "veteran", "veteran"),
    "ladder": ("cyborg", "veteran", "moderate", "novice"),
}


def play(task) -> dict:
    map_name, roster, teams, seed, cap = task
    random.seed(seed)
    match = Match(Settings(map_name=map_name, teams=teams))
    for index, skill in enumerate(roster):
        match.add_player("ABCD"[index], bot=True, skill=skill)
    match.start_match()
    brains = {p.pid: BotBrain(p.skill, random.Random(seed * 97 + p.pid))
              for p in match.state.players.values()}

    turns = 0
    spent = 0.0
    peak = 0
    events = {"offer": 0, "pact": 0, "declare": 0}
    leaders = []
    while match.phase != PHASE_OVER and turns < cap:
        for player in list(match.state.players.values()):
            if player.alive:
                match.submit(player.pid, brains[player.pid].plan(match, player))
        started = time.perf_counter()
        result = match.resolve()
        spent += time.perf_counter() - started
        turns += 1
        state = match.state
        peak = max(peak, sum(len(state.units_of(p.pid))
                             for p in state.players.values()))
        ground = {p.pid: state.holding(p.pid)
                  for p in state.players.values() if p.alive}
        if ground:
            leaders.append(max(ground, key=lambda pid: ground[pid]))
        for timeline in result.values():
            for event in timeline["events"]:
                if event["e"] in events:
                    events[event["e"]] += 1
            break                           # one player's view is enough
        if not match.begin_orders():
            break

    state = match.state
    alive = [p for p in state.players.values() if p.alive]
    ground = sorted((state.holding(p.pid) for p in alive), reverse=True)
    # How often the commander in front changed hands: a proxy for whether the
    # game was ever in doubt.
    swings = sum(1 for a, b in zip(leaders, leaders[1:]) if a != b)
    return {
        "turns": turns,
        "ending": ("unresolved" if turns >= cap
                   else "armistice" if match.peace else "conquest"),
        "survivors": len(alive),
        "peak_units": peak,
        "ms": spent / max(1, turns) * 1000,
        "margin": (ground[0] - ground[1]) / max(1, ground[0]) if len(ground) > 1
                  else 1.0,
        "claimed": len(state.node_owner),
        "nodes": len(state.map.nodes),
        "lead_changes": swings,
        **events,
    }


def report(label: str, rows: list) -> None:
    endings = {}
    for row in rows:
        endings[row["ending"]] = endings.get(row["ending"], 0) + 1
    mix = "  ".join(f"{n} {k}" for k, n in sorted(endings.items()))
    print(f"{label:26s} {statistics.median(r['turns'] for r in rows):5.0f} turns"
          f"  {statistics.mean(r['ms'] for r in rows):6.0f} ms/turn"
          f"  peak {max(r['peak_units'] for r in rows):3d}"
          f"  margin {statistics.median(r['margin'] for r in rows):4.0%}"
          f"  swings {statistics.median(r['lead_changes'] for r in rows):3.0f}"
          f"  map used {statistics.median(r['claimed'] / max(1, r['nodes']) for r in rows):4.0%}"
          f"  |  {mix}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map", default="reach")
    parser.add_argument("--all", action="store_true",
                        help="every shipped map, smallest first")
    parser.add_argument("--roster", default="mixed", choices=sorted(ROSTERS))
    parser.add_argument("--games", type=int, default=6)
    parser.add_argument("--teams", action="store_true")
    parser.add_argument("--cap", type=int, default=250)
    parser.add_argument("--workers", type=int, default=mp.cpu_count())
    args = parser.parse_args()

    from standing_orders.game import available_maps, load_map
    if args.all:
        maps = sorted(available_maps(),
                      key=lambda n: load_map(n).width * load_map(n).height)
    else:
        maps = [args.map]

    roster = ROSTERS[args.roster]
    print(f"{args.games} games a map, roster {args.roster} "
          f"{'(teams)' if args.teams else '(free-for-all)'}\n")
    print(f"{'map':26s} {'length':>11s} {'cost':>13s} {'':>8s} {'margin':>11s}"
          f" {'swings':>10s} {'map used':>13s}  |  endings")
    with mp.Pool(args.workers) as pool:
        for name in maps:
            tilemap = load_map(name)
            players = min(len(roster), tilemap.info.players)
            tasks = [(name, roster[:players], args.teams, seed, args.cap)
                     for seed in range(args.games)]
            rows = pool.map(play, tasks)
            report(f"{name} ({tilemap.width}x{tilemap.height})", rows)


if __name__ == "__main__":
    main()
