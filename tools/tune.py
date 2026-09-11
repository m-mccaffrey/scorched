"""CMA-ES over the bot's numbers.

    python3 -m tools.tune --generations 40 --seeds 4 --workers 4

The bot's difficulty tiers do not currently form a ladder: Moderate beats both
Veteran and Cyborg, and hand-sweeping single constants does not move it. Each
knob is defensible on its own and the interactions between thirty-five of them
are not something anyone can hold in their head, which is exactly the shape of
problem a black-box optimiser is for.

This never edits the game. It prints the candidate it found and the evidence
for it, and a human decides whether to put those numbers in ``ai.py``.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import time

import cma

from tools import aiparams
from tools.arena import (TARGETS, format_report, jobs, ladder_holds, play,
                         score)


def _normalise(values: dict) -> list[float]:
    return [(values[key] - low) / (high - low)
            for key, low, high, _whole in aiparams.spec()]


def _denormalise(vector) -> dict:
    out = {}
    for value, (key, low, high, whole) in zip(vector, aiparams.spec()):
        raw = low + min(1.0, max(0.0, float(value))) * (high - low)
        out[key] = int(round(raw)) if whole else float(raw)
    return out


# -- worker ---------------------------------------------------------------

def _run_one(task):
    index, values, weaker, stronger, seed, swap = task
    aiparams.apply(values)
    won, unfinished = play(weaker, stronger, seed, swap)
    return index, (weaker, stronger), won, unfinished


def evaluate(pool, candidates: list[dict], seeds) -> list[tuple]:
    """Score a whole generation at once, so every core stays busy."""
    tasks = [(index, values, weaker, stronger, seed, swap)
             for index, values in enumerate(candidates)
             for weaker, stronger, seed, swap in jobs(seeds)]
    tallies = [{pair: [0, 0, 0] for pair in TARGETS} for _ in candidates]
    for index, pair, won, unfinished in pool.imap_unordered(_run_one, tasks,
                                                            chunksize=4):
        tally = tallies[index][pair]
        tally[0] += won
        tally[1] += 1
        tally[2] += unfinished
    return [score({pair: tuple(counts) for pair, counts in tally.items()})
            for tally in tallies]


def validate(pool, values: dict, seeds) -> tuple[float, dict]:
    """Re-score one candidate on seeds it has never played."""
    return evaluate(pool, [values], seeds)[0]


# -- driver ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generations", type=int, default=40)
    parser.add_argument("--seeds", type=int, default=4,
                        help="seeds per pairing during the search")
    parser.add_argument("--holdout", type=int, default=16,
                        help="fresh seeds the winner is re-checked on")
    parser.add_argument("--workers", type=int, default=mp.cpu_count())
    parser.add_argument("--sigma", type=float, default=0.25)
    parser.add_argument("--out", default="tools/tuned.json")
    args = parser.parse_args()

    search_seeds = list(range(args.seeds))
    holdout_seeds = list(range(1000, 1000 + args.holdout))
    base = aiparams.baseline()

    strategy = cma.CMAEvolutionStrategy(
        _normalise(base), args.sigma,
        {"bounds": [0, 1], "seed": 7, "verbose": -9})

    per_eval = len(jobs(search_seeds))
    print(f"{len(aiparams.spec())} knobs, population {strategy.popsize}, "
          f"{per_eval} matches per candidate "
          f"({strategy.popsize * per_eval} a generation)")

    with mp.Pool(args.workers) as pool:
        loss, report = validate(pool, base, search_seeds)
        print(f"\nbaseline (search seeds): loss {loss:.4f}")
        print(format_report(report))

        best = (loss, base, report)
        started = time.time()
        for generation in range(1, args.generations + 1):
            vectors = strategy.ask()
            candidates = [_denormalise(v) for v in vectors]
            scored = evaluate(pool, candidates, search_seeds)
            strategy.tell(vectors, [s[0] for s in scored])

            rank = min(range(len(scored)), key=lambda i: scored[i][0])
            if scored[rank][0] < best[0]:
                best = (scored[rank][0], candidates[rank], scored[rank][1])
                marker = "  <- new best"
            else:
                marker = ""
            elapsed = (time.time() - started) / 60
            print(f"gen {generation:3d}  best {scored[rank][0]:.4f}  "
                  f"overall {best[0]:.4f}  [{elapsed:.0f} min]{marker}",
                  flush=True)
            # Checkpoint every generation: a three-hour search that has to be
            # stopped early should still hand back its best answer so far.
            with open(args.out, "w") as handle:
                json.dump({"values": best[1], "search_loss": best[0],
                           "generation": generation}, handle, indent=2)

        print("\n" + "=" * 62)
        print("BEST CANDIDATE, on the seeds it was searched against:")
        print(format_report(best[2]))

        print(f"\nHELD OUT ({args.holdout} seeds it has never played):")
        held_loss, held_report = validate(pool, best[1], holdout_seeds)
        print(format_report(held_report))

        print(f"\nbaseline on the same held-out seeds:")
        base_loss, base_report = validate(pool, base, holdout_seeds)
        print(format_report(base_report))

        print(f"\nheld-out loss: baseline {base_loss:.4f} -> "
              f"tuned {held_loss:.4f}")
        print(f"ladder holds: baseline {ladder_holds(base_report)} -> "
              f"tuned {ladder_holds(held_report)}")

        print("\nknobs the search moved:")
        for line in aiparams.describe(best[1], base):
            print(line)

        with open(args.out, "w") as handle:
            json.dump({"values": best[1],
                       "search_loss": best[0],
                       "holdout_loss": held_loss,
                       "baseline_holdout_loss": base_loss}, handle, indent=2)
        print(f"\nwritten to {args.out} -- nothing in the game has changed")


if __name__ == "__main__":
    main()
