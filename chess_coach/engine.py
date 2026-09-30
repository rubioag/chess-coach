"""Stockfish UCI wrapper.

Thin layer over python-chess. Its only jobs are:

  * own the engine process lifecycle (so a crashed or hung engine cannot leave
    an orphan holding a pipe),
  * evaluate one position at a fixed depth with MultiPV 1,
  * convert python-chess score objects into perspective-normalized
    `Evaluation` values, including the mate cases,
  * answer for terminal positions the engine refuses to analyse.

V1 runs the engine with a single thread. That is not a performance oversight:
a single-threaded search is reproducible, and correctness has to be verified
before throughput is tuned.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import chess
import chess.engine

from .evaluation import Evaluation

# Magnitude a forced mate maps to on the centipawn scale. Far above any
# material evaluation, so mate always outranks a finite advantage.
MATE_SCORE_CP = 10_000


class EngineError(RuntimeError):
    pass


@dataclass(frozen=True)
class PositionAnalysis:
    """Engine verdict on one position, from the side-to-move's perspective."""

    evaluation: Evaluation
    best_move: chess.Move | None
    depth: int
    # Principal variation from this position. pv[0] is always best_move, so the
    # two can never disagree. Empty for a terminal position.
    pv: tuple[chess.Move, ...] = ()


def score_to_evaluation(
    pov_score: chess.engine.PovScore, color: chess.Color
) -> Evaluation:
    """Normalize an engine score to `color`'s perspective.

    `PovScore.pov(color)` does the sign flip, which is what keeps White- and
    Black-perspective numbers out of the same column (spec section 7).

    Mate handling: python-chess reports `mate() == 0` for both "mate has been
    delivered" (MateGiven, +MATE_SCORE_CP) and "I am checkmated" (Mate(-0),
    -MATE_SCORE_CP). The two are told apart by the sign of `cp`, which is why
    both fields are stored rather than just the mate distance.
    """
    score = pov_score.pov(color)
    cp = score.score(mate_score=MATE_SCORE_CP)
    if cp is None:  # defensive: python-chess only returns None without mate_score
        raise EngineError(f"engine score could not be scaled: {score!r}")
    return Evaluation(cp=int(cp), mate=score.mate())


def terminal_evaluation(board: chess.Board, color: chess.Color) -> Evaluation:
    """Evaluation of a finished position, from `color`'s perspective.

    The engine refuses to search a position with no legal moves, so checkmate
    and stalemate are answered from the board itself. Games that ended by
    resignation, timeout or agreement are NOT terminal by these rules - the
    position still has legal moves and is analysed normally, which is correct:
    we grade the moves that were played, not the reason the clock stopped.
    """
    outcome = board.outcome(claim_draw=False)
    if outcome is None:
        raise EngineError("terminal_evaluation called on a live position")
    if outcome.winner is None:
        return Evaluation(cp=0, mate=None)
    if outcome.winner == color:
        return Evaluation(cp=MATE_SCORE_CP, mate=0)
    return Evaluation(cp=-MATE_SCORE_CP, mate=0)


class StockfishEngine:
    """Context manager owning one Stockfish process."""

    def __init__(
        self,
        path: str | Path,
        threads: int = 1,
        hash_mb: int = 128,
        multipv: int = 1,
    ) -> None:
        self.path = str(path)
        self.threads = threads
        self.hash_mb = hash_mb
        self.multipv = multipv
        self._engine: chess.engine.SimpleEngine | None = None
        self.name: str = "unknown"

    def __enter__(self) -> "StockfishEngine":
        try:
            self._engine = chess.engine.SimpleEngine.popen_uci(self.path)
        except (OSError, chess.engine.EngineError) as exc:
            raise EngineError(f"could not start Stockfish at {self.path}: {exc}") from exc
        self.name = str(self._engine.id.get("name", "unknown"))
        self._engine.configure({"Threads": self.threads, "Hash": self.hash_mb})
        return self

    @property
    def engine_name(self) -> str:
        """First token of the UCI id, e.g. 'Stockfish' from 'Stockfish 18'."""
        return self.name.split()[0] if self.name.split() else self.name

    @property
    def engine_version(self) -> str:
        """Remainder of the UCI id. Free text: a new release needs no schema change."""
        parts = self.name.split(maxsplit=1)
        return parts[1] if len(parts) > 1 else "unknown"

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._engine is not None:
            try:
                self._engine.quit()
            except chess.engine.EngineError:
                pass
            self._engine = None

    def analyse_position(self, board: chess.Board, depth: int) -> PositionAnalysis:
        """Search one position at a fixed depth, MultiPV 1.

        The returned evaluation is from the perspective of the side to move.
        Terminal positions are answered without invoking the engine.
        """
        if self._engine is None:
            raise EngineError("engine is not running")

        if board.is_game_over(claim_draw=False):
            return PositionAnalysis(
                evaluation=terminal_evaluation(board, board.turn),
                best_move=None,
                depth=0,
                pv=(),
            )

        try:
            infos = self._engine.analyse(
                board, chess.engine.Limit(depth=depth), multipv=self.multipv
            )
        except chess.engine.EngineError as exc:
            raise EngineError(f"analysis failed on {board.fen()}: {exc}") from exc

        info = infos[0] if isinstance(infos, list) else infos
        pov_score = info.get("score")
        if pov_score is None:
            raise EngineError(f"engine returned no score for {board.fen()}")

        pv = tuple(info.get("pv") or ())
        return PositionAnalysis(
            evaluation=score_to_evaluation(pov_score, board.turn),
            best_move=pv[0] if pv else None,
            depth=int(info.get("depth", depth)),
            pv=pv,
        )
