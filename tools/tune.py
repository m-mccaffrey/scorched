"""CMA-ES over the bot's numbers.

    python3 -m tools.tune --generations 4          # runs, or continues
    python3 -m tools.tune --report                 # holdout check, no search

The bot's difficulty tiers do not form a ladder: Moderate beats both Veteran
and Cyborg, and hand-sweeping single constants does not move it. Each knob is
defensible on its own and the interactions between thirty of them are not
something anyone can hold in their head, which is the shape of problem a
black-box optimiser is for.

**Every run resumes.** This is not a nicety: the search takes roughly forty
minutes and it lives in a container that gets reclaimed whenever the session
goes quiet, which has already eaten one run at generation two. The full
optimiser state is checkpointed after every generation, so the search proceeds
in whatever size chunks its host can actually finish -- and a chunk that dies
halfway costs one generation, not the run.

This never edits the game. It prints the candidate and the evidence, and a
human decides whether the numbers go into ``ai.py``.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import pickle
import time

import cma

from tools import aiparams
from tools.arena import (TARGETS, format_report, jobs, ladder_holds, play,
                         score)

STATE = "tools/.tune_state.pkl"
RESULT = "tools/tuned.json"


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
    index, values, weaker, stronger, seed, swap, map_name = task
    aiparams.apply(values)
    won, unfinished = play(weaker, stronger, seed, swap, map_name=map_name)
    return index, (weaker, stronger), won, unfinished


def evaluate(pool, candidates: list[dict], seeds) -> list[tuple]:
    """Score a whole generation at once, so every core stays busy."""
    tasks = [(index, values, *job)
             for index, values in enumerate(candidates)
             for job in jobs(seeds)]
    tallies = [{pair: [0, 0, 0] for pair in TARGETS} for _ in candidates]
    for index, pair, won, unfinished in pool.imap_unordered(_run_one, tasks,
                                                            chunksize=4):
        tally = tallies[index][pair]
        tally[0] += won
        tally[1] += 1
        tally[2] += unfinished
    return [score({pair: tuple(counts) for pair, counts in tally.items()})
            for tally in tallies]


# -- checkpointing --------------------------------------------------------

def load_state(dimensions: int, sigma: float, base: dict):
    if os.path.exists(STATE):
        with open(STATE, "rb") as handle:
            state = pickle.load(handle)
        print(f"resuming from generation {state['generation']} "
              f"(best {state['best_loss']:.4f})")
        return state
    strategy = cma.CMAEvolutionStrategy(
        _normalise(base), sigma,
        {"bounds": [0, 1], "seed": 7, "verbose": -9})
    return {"strategy": strategy, "generation": 0, "best_loss": float("inf"),
            "best_values": base, "best_report": None, "minutes": 0.0}


def save_state(state) -> None:
    tmp = STATE + ".tmp"
    with open(tmp, "wb") as handle:
        pickle.dump(state, handle)
    os.replace(tmp, STATE)          # never leave a half-written checkpoint


# -- driver ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generations", type=int, default=4,
                        help="generations to run in THIS invocation")
    parser.add_argument("--seeds", type=int, default=6)
    parser.add_argument("--holdout", type=int, default=18)
    parser.add_argument("--workers", type=int, default=mp.cpu_count())
    parser.add_argument("--sigma", type=float, default=0.25)
    parser.add_argument("--report", action="store_true",
                        help="validate the checkpointed best and stop")
    parser.add_argument("--restart", action="store_true",
                        help="discard any checkpoint and start over")
    args = parser.parse_args()

    if args.restart and os.path.exists(STATE):
        os.remove(STATE)

    search_seeds = list(range(args.seeds))
    holdout_seeds = list(range(1000, 1000 + args.holdout))
    base = aiparams.baseline()
    state = load_state(len(aiparams.spec()), args.sigma, base)
    strategy = state["strategy"]

    with mp.Pool(args.workers) as pool:
        if args.report:
            report_on(pool, state, base, holdout_seeds)
            return

        per_eval = len(jobs(search_seeds))
        print(f"{len(aiparams.spec())} knobs, population {strategy.popsize}, "
              f"{per_eval} matches per candidate "
              f"({strategy.popsize * per_eval} a generation)")
        if state["generation"] == 0:
            loss, report = evaluate(pool, [base], search_seeds)[0]
            print(f"\nbaseline (search seeds): loss {loss:.4f}")
            print(format_report(report))
            state["best_loss"], state["best_report"] = loss, report
            print()

        started = time.time()
        target = state["generation"] + args.generations
        while state["generation"] < target:
            vectors = strategy.ask()
            candidates = [_denormalise(v) for v in vectors]
            scored = evaluate(pool, candidates, search_seeds)
            strategy.tell(vectors, [s[0] for s in scored])
            state["generation"] += 1

            rank = min(range(len(scored)), key=lambda i: scored[i][0])
            marker = ""
            if scored[rank][0] < state["best_loss"]:
                state["best_loss"] = scored[rank][0]
                state["best_values"] = candidates[rank]
                state["best_report"] = scored[rank][1]
                marker = "  <- new best"
            state["minutes"] += (time.time() - started) / 60
            started = time.time()
            print(f"gen {state['generation']:3d}  this {scored[rank][0]:.4f}  "
                  f"best {state['best_loss']:.4f}  "
                  f"[{state['minutes']:.0f} min total]{marker}", flush=True)
            save_state(state)

        print(f"\nstopped at generation {state['generation']}. Run again to "
              f"continue, or --report for the held-out check.")


def report_on(pool, state, base: dict, holdout_seeds) -> None:
    """The only number that decides anything: unseen seeds, against baseline."""
    if state["best_report"] is None:
        print("nothing searched yet")
        return
    print(f"generation {state['generation']}, "
          f"{state['minutes']:.0f} minutes of search\n")
    print("BEST CANDIDATE on the seeds it was searched against:")
    print(format_report(state["best_report"]))

    tuned_loss, tuned = evaluate(pool, [state["best_values"]],
                                 holdout_seeds)[0]
    base_loss, base_report = evaluate(pool, [base], holdout_seeds)[0]
    print(f"\nHELD OUT ({len(holdout_seeds)} seeds it has never played):")
    print(format_report(tuned))
    print("\nbaseline on those same seeds:")
    print(format_report(base_report))
    print(f"\nheld-out loss: baseline {base_loss:.4f} -> tuned {tuned_loss:.4f}")
    print(f"ladder holds:  baseline {ladder_holds(base_report)} -> "
          f"tuned {ladder_holds(tuned)}")

    print("\nthe difficulty table this proposes:")
    for tier, skill in aiparams.profiles(state["best_values"]).items():
        print(f"  {tier:9s} {skill}")
    print("\nshared constants it moved:")
    for line in aiparams.describe(aiparams.clamp(state["best_values"]), base):
        print(line)

    with open(RESULT, "w") as handle:
        json.dump({"values": state["best_values"],
                   "generation": state["generation"],
                   "search_loss": state["best_loss"],
                   "holdout_loss": tuned_loss,
                   "baseline_holdout_loss": base_loss,
                   "ladder_holds": ladder_holds(tuned)}, handle, indent=2)
    print(f"\nwritten to {RESULT} -- nothing in the game has changed")


if __name__ == "__main__":
    main()
