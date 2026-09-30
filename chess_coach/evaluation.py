"""Pure, deterministic evaluation logic: normalization, classification, phase.

Nothing in this module talks to an engine or a database. Every function here is
a pure function of its arguments so it can be unit-tested exhaustively, which is
what sections 7-9 of the V1 spec require.

EVALUATION SCALE
----------------
All evaluations are integers in centipawns (100 cp = one pawn), always
normalized to *the perspective of the player who is about to move* (the mover).
Positive means good for the mover, negative means bad for the mover. A White
eval of +1.2 and a Black eval of -1.2 both become +120 in the mover's own
column, so White-perspective and Black-perspective numbers are never mixed.

    White: before +1.2 -> after +0.1  =>  before 120, after  10, loss 110
    Black: before -1.2 -> after -0.1  =>  before 120, after  10, loss 110

MATE SCORES
-----------
A forced mate is not a centipawn quantity, so it is stored twice: `mate` keeps
the signed distance in moves (+3 = mover mates in 3, -2 = mover gets mated in
2), and `cp` carries a mapped magnitude near +/- MATE_SCORE_CP so that ordering
and arithmetic still work. Mate-in-1 outranks mate-in-9; being mated always
ranks below any finite evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import chess

# Version of the pure rule set in this module: the loss formula, the
# classification ladder and the phase rules. Bump it whenever the MEANING of a
# stored label changes, so rows produced under different semantics can be told
# apart. Threshold VALUES are recorded per analysis run separately - this
# version tracks the shape of the rules, not their tuning.
RULES_VERSION = "1.0"

# Classification labels (V1 is deliberately limited to these five).
BEST = "BEST"
GOOD = "GOOD"
INACCURACY = "INACCURACY"
MISTAKE = "MISTAKE"
BLUNDER = "BLUNDER"
CLASSIFICATIONS = (BEST, GOOD, INACCURACY, MISTAKE, BLUNDER)

OPENING = "OPENING"
MIDDLEGAME = "MIDDLEGAME"
ENDGAME = "ENDGAME"
PHASES = (OPENING, MIDDLEGAME, ENDGAME)

Color = Literal["white", "black"]


@dataclass(frozen=True)
class Thresholds:
    """Classification thresholds, in centipawns of mover-perspective loss.

    Rationale for the defaults (evaluation_loss is always >= 0, so the scheme is
    symmetric between colors by construction):

      inaccuracy_cp = 50   half a pawn. Below this, engine search noise at
                           shallow depth is the same order as the "error", so
                           calling it a mistake would be unjustified.
      mistake_cp    = 100  a full pawn of value handed over.
      blunder_cp    = 300  a minor piece. At this magnitude the game's expected
                           result genuinely changes.

    Boundaries are inclusive on the lower edge: loss == 50 is an INACCURACY,
    loss == 100 is a MISTAKE, loss == 300 is a BLUNDER.
    """

    inaccuracy_cp: int = 50
    mistake_cp: int = 100
    blunder_cp: int = 300

    def __post_init__(self) -> None:
        if not (0 < self.inaccuracy_cp < self.mistake_cp < self.blunder_cp):
            raise ValueError(
                "thresholds must be strictly increasing and positive: "
                f"{self.inaccuracy_cp} < {self.mistake_cp} < {self.blunder_cp}"
            )


@dataclass(frozen=True)
class PhaseRules:
    """Deterministic OPENING / MIDDLEGAME / ENDGAME rules.

    V1 keeps this intentionally crude and fully documented rather than clever.
    `material` is the sum of non-pawn, non-king material for BOTH sides using
    Q=9, R=5, B=3, N=3, so the starting position scores 62.

      ENDGAME  when material <= endgame_max_material (queens generally traded
               and few pieces left).
      OPENING  when we are still inside the first `opening_max_ply` plies AND at
               most one minor piece has left the board
               (material >= opening_min_material).
      MIDDLEGAME otherwise.

    Phase is evaluated on the position the mover faced (fen_before). The DB
    stores the resulting label, so a better classifier can replace these rules
    later without a schema change.
    """

    opening_max_ply: int = 20
    opening_min_material: int = 58
    endgame_max_material: int = 20


# Non-pawn, non-king material weights used only for phase detection.
PHASE_PIECE_VALUES = {
    chess.QUEEN: 9,
    chess.ROOK: 5,
    chess.BISHOP: 3,
    chess.KNIGHT: 3,
}
STARTING_PHASE_MATERIAL = 62


@dataclass(frozen=True)
class Evaluation:
    """An evaluation already normalized to one player's perspective."""

    cp: int
    mate: int | None = None

    @property
    def is_mate(self) -> bool:
        return self.mate is not None


def flip(evaluation: Evaluation) -> Evaluation:
    """Same evaluation seen from the other player's side.

    Needed because each position is searched exactly once, from the side-to-move
    perspective. The position *after* a move is scored for the opponent, so it
    must be flipped to become `evaluation_after` in the mover's column.

    The sign flip also resolves the mate(0) ambiguity correctly: "mate
    delivered" (+MATE, 0) becomes "I am checkmated" (-MATE, 0).
    """
    return Evaluation(
        cp=-evaluation.cp,
        mate=None if evaluation.mate is None else -evaluation.mate,
    )


def clamp(value: int, cap: int) -> int:
    return max(-cap, min(cap, value))


def evaluation_loss(before: Evaluation, after: Evaluation, eval_cap_cp: int) -> int:
    """Centipawns the mover gave up, from the mover's own perspective.

    Both evaluations must already be in the mover's perspective; mixing
    perspectives here is exactly the bug section 7 warns about.

    Two deliberate rules:

    * Evaluations are clamped to +/- `eval_cap_cp` before subtracting. Past
      roughly ten pawns the game is decided, and an unclamped scale would turn
      "+30.0 down to +12.0" into a 1800 cp blunder, flooding the dataset with
      meaningless events. Clamping keeps decided positions from generating
      false signal. The raw, unclamped evaluations are stored separately, so
      nothing is lost.
    * The loss floors at 0. A negative loss means the played move scored better
      than the engine's own best line, which happens because `before` and
      `after` come from two independent searches (see `classify`). That is
      search noise, not a gain, so it is recorded as zero.
    """
    raw = clamp(before.cp, eval_cap_cp) - clamp(after.cp, eval_cap_cp)
    return max(0, raw)


def classify(
    loss_cp: int,
    played_uci: str,
    best_uci: str | None,
    thresholds: Thresholds,
) -> str:
    """Pure classification. Deterministic for a given (loss, move, best move).

    A move that matches the engine's preferred move is BEST regardless of the
    measured loss. This is not a shortcut: `evaluation_before` comes from
    searching the position before the move and `evaluation_after` from an
    independent search of the position after it. At a fixed depth those two
    searches can disagree by a few centipawns, so even the engine's own top
    move can show a small non-zero loss. Move equality is the trustworthy
    signal there; the centipawn delta is not.
    """
    if best_uci is not None and played_uci == best_uci:
        return BEST
    if loss_cp >= thresholds.blunder_cp:
        return BLUNDER
    if loss_cp >= thresholds.mistake_cp:
        return MISTAKE
    if loss_cp >= thresholds.inaccuracy_cp:
        return INACCURACY
    return GOOD


def phase_material(board: chess.Board) -> int:
    """Non-pawn, non-king material for both sides. Starting position = 62."""
    total = 0
    for piece_type, value in PHASE_PIECE_VALUES.items():
        total += value * len(board.pieces(piece_type, chess.WHITE))
        total += value * len(board.pieces(piece_type, chess.BLACK))
    return total


def detect_phase(board: chess.Board, ply: int, rules: PhaseRules) -> str:
    """Deterministic phase label for the position the mover faced.

    `ply` is 1-based: the first move of the game is ply 1.
    """
    material = phase_material(board)
    if material <= rules.endgame_max_material:
        return ENDGAME
    if ply <= rules.opening_max_ply and material >= rules.opening_min_material:
        return OPENING
    return MIDDLEGAME


def move_number_and_color(ply: int) -> tuple[int, Color]:
    """Map a 1-based ply to its (move_number, color). Ply 1 = move 1, white."""
    if ply < 1:
        raise ValueError(f"ply must be >= 1, got {ply}")
    return (ply + 1) // 2, ("white" if ply % 2 == 1 else "black")
