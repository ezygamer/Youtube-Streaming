"""Offline benchmark of SumLink strategies (no browser).

Simulates full runs (stage 2 onwards, score carried across stages) with the
ported game rules and reports how far each strategy gets.

Usage: .venv\\Scripts\\python tools\\bench_sumlink.py [--runs 50] [--max-stage 30]
"""
import argparse
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from games.sumlink import rules  # noqa: E402
from games.sumlink.strategies import STRATEGIES, make_strategy  # noqa: E402


def simulate_run(strategy, rng: random.Random, max_stage: int) -> tuple[int, int]:
    """Returns (stage_reached, score). Stage 1 is a fixed 2-match tutorial, so start at 2."""
    score, stage = 20, 2
    while stage <= max_stage:
        board, add_rows = rules.generate_board(stage, rng), 5
        while not rules.is_stage_cleared(board):
            d = strategy.choose(board, add_rows)
            if d.kind == "match":
                board = rules.apply_match(board, *d.pair)
                score += 10
            elif d.kind == "add_row":
                board = rules.add_numbers(board)
                add_rows -= 1
            else:
                return stage, score
        stage += 1
    return stage, score


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--runs", type=int, default=50)
    p.add_argument("--max-stage", type=int, default=30)
    args = p.parse_args()

    for name in STRATEGIES:
        strategy = make_strategy(name)
        rng = random.Random(42)  # same boards for every strategy
        t0 = time.perf_counter()
        results = [simulate_run(strategy, rng, args.max_stage) for _ in range(args.runs)]
        dt = time.perf_counter() - t0
        stages = [s for s, _ in results]
        scores = [sc for _, sc in results]
        capped = sum(s > args.max_stage for s in stages)
        print(f"{name:11s} avg stage {statistics.mean(stages):5.1f}  median {statistics.median(stages):4.0f}  "
              f"avg score {statistics.mean(scores):6.0f}  max {max(scores):5d}  "
              f"hit cap {capped}/{args.runs}  ({dt:.1f}s)")


if __name__ == "__main__":
    main()
