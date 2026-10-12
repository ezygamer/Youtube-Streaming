"""Deterministic move-selection strategies for SumLink.

A strategy looks at the board and returns a Decision; it never touches the
browser, so strategies can be benchmarked offline (tools/bench_sumlink.py).
"""
from dataclasses import dataclass

from games.sumlink import rules


@dataclass(frozen=True)
class Decision:
    kind: str                         # "match" | "add_row" | "none"
    pair: tuple[int, int] | None = None


class Strategy:
    name = "base"

    def choose(self, board: rules.Board, add_rows_left: int) -> Decision:
        raise NotImplementedError

    @staticmethod
    def _fallback(add_rows_left: int) -> Decision:
        # Add row is only allowed by the game when no match exists.
        return Decision("add_row") if add_rows_left > 0 else Decision("none")


class FirstPairStrategy(Strategy):
    """Plays the first valid pair - exactly the move the game's Hint shows."""
    name = "first_pair"

    def choose(self, board, add_rows_left):
        pair = rules.find_valid_pair(board)
        return Decision("match", pair) if pair else self._fallback(add_rows_left)


def playout(board: rules.Board, add_rows_left: int, max_moves: int = 300) -> tuple[bool, int]:
    """Play the rest of the stage with FirstPairStrategy.
    Returns (stage_cleared, matches_made)."""
    matches = 0
    for _ in range(max_moves):
        if rules.is_stage_cleared(board):
            return True, matches
        pair = rules.find_valid_pair(board)
        if pair:
            board = rules.apply_match(board, *pair)
            matches += 1
        elif add_rows_left > 0:
            board = rules.add_numbers(board)
            add_rows_left -= 1
        else:
            return False, matches
    return rules.is_stage_cleared(board), matches


class RolloutStrategy(Strategy):
    """Tries each candidate match, plays the stage out greedily from there,
    and picks the match whose playout clears the stage (or, failing that,
    makes the most matches). Fully deterministic.

    Clearing the stage matters most for a high score: each new stage brings
    36 fresh numbers and 5 fresh add-rows.
    """
    name = "rollout"
    CLEAR_BONUS = 10_000

    def __init__(self, max_candidates: int = 12):
        self.max_candidates = max_candidates

    def choose(self, board, add_rows_left):
        pairs = rules.find_valid_pairs(board)
        if not pairs:
            return self._fallback(add_rows_left)
        if len(pairs) == 1:
            return Decision("match", pairs[0])
        best_pair, best_value = pairs[0], -1
        for pair in pairs[: self.max_candidates]:
            cleared, matches = playout(rules.apply_match(board, *pair), add_rows_left)
            value = matches + (self.CLEAR_BONUS if cleared else 0)
            if value > best_value:  # strict: ties keep the earlier (hint-order) pair
                best_pair, best_value = pair, value
        return Decision("match", best_pair)


STRATEGIES = {
    FirstPairStrategy.name: FirstPairStrategy,
    RolloutStrategy.name: RolloutStrategy,
}


def make_strategy(name: str, **kwargs) -> Strategy:
    try:
        return STRATEGIES[name](**kwargs)
    except KeyError:
        raise ValueError(f"Unknown strategy '{name}'. Available: {', '.join(STRATEGIES)}") from None
