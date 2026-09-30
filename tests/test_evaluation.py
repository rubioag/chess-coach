"""Pure evaluation logic: sign normalization, classification, mate, phase."""

from __future__ import annotations

import chess
import chess.engine
import pytest

from chess_coach.engine import (
    MATE_SCORE_CP,
    EngineError,
    score_to_evaluation,
    terminal_evaluation,
)
from chess_coach.evaluation import (
    BEST,
    BLUNDER,
    ENDGAME,
    GOOD,
    INACCURACY,
    MIDDLEGAME,
    MISTAKE,
    OPENING,
    STARTING_PHASE_MATERIAL,
    Evaluation,
    PhaseRules,
    Thresholds,
    classify,
    clamp,
    detect_phase,
    evaluation_loss,
    flip,
    move_number_and_color,
    phase_material,
)

CAP = 1000
T = Thresholds()
RULES = PhaseRules()


# --------------------------------------------------------------------------
# C. Evaluation sign normalization (spec section 7)
# --------------------------------------------------------------------------

def test_white_loss_is_mover_perspective() -> None:
    """White: before +1.2 -> after +0.1 = loss 1.1"""
    before, after = Evaluation(120), Evaluation(10)
    assert evaluation_loss(before, after, CAP) == 110


def test_black_loss_is_mover_perspective() -> None:
    """Black: before -1.2 -> after -0.1 = loss 1.1

    The engine reports both positions in White's frame (-120 then -10).
    Normalized to Black - the mover - they become +120 and +10, giving the same
    loss of 110 as the mirrored White case. This is the exact bug section 7
    warns about: the two colors must never land in one column unnormalized.
    """
    white_frame_before = chess.engine.PovScore(chess.engine.Cp(-120), chess.WHITE)
    white_frame_after = chess.engine.PovScore(chess.engine.Cp(-10), chess.WHITE)

    before = score_to_evaluation(white_frame_before, chess.BLACK)
    after = score_to_evaluation(white_frame_after, chess.BLACK)

    assert (before.cp, after.cp) == (120, 10)
    assert evaluation_loss(before, after, CAP) == 110


def test_both_colors_give_identical_loss_for_mirrored_positions() -> None:
    for cp_before, cp_after in [(120, 10), (300, -50), (0, -250)]:
        white = evaluation_loss(Evaluation(cp_before), Evaluation(cp_after), CAP)
        black_before = score_to_evaluation(
            chess.engine.PovScore(chess.engine.Cp(-cp_before), chess.WHITE), chess.BLACK
        )
        black_after = score_to_evaluation(
            chess.engine.PovScore(chess.engine.Cp(-cp_after), chess.WHITE), chess.BLACK
        )
        assert evaluation_loss(black_before, black_after, CAP) == white


def test_score_to_evaluation_flips_sign_for_black() -> None:
    pov = chess.engine.PovScore(chess.engine.Cp(75), chess.WHITE)
    assert score_to_evaluation(pov, chess.WHITE).cp == 75
    assert score_to_evaluation(pov, chess.BLACK).cp == -75


def test_flip_inverts_perspective() -> None:
    assert flip(Evaluation(120)).cp == -120
    assert flip(flip(Evaluation(120))) == Evaluation(120)


def test_flip_turns_mate_delivered_into_being_mated() -> None:
    delivered = Evaluation(cp=MATE_SCORE_CP, mate=0)
    mated = flip(delivered)
    assert mated.cp == -MATE_SCORE_CP and mated.mate == 0
    assert flip(Evaluation(cp=9997, mate=3)) == Evaluation(cp=-9997, mate=-3)


def test_loss_never_goes_negative() -> None:
    """Independent searches can score the played move above the engine's best."""
    assert evaluation_loss(Evaluation(10), Evaluation(60), CAP) == 0


def test_loss_is_clamped_in_decided_positions() -> None:
    """+30.0 down to +12.0 is still winning: it must not read as a blunder."""
    assert evaluation_loss(Evaluation(3000), Evaluation(1200), CAP) == 0
    # Falling out of a decided position into equality is still a real loss.
    assert evaluation_loss(Evaluation(3000), Evaluation(0), CAP) == CAP


def test_clamp() -> None:
    assert clamp(5000, CAP) == CAP
    assert clamp(-5000, CAP) == -CAP
    assert clamp(250, CAP) == 250


# --------------------------------------------------------------------------
# D. Classification boundaries and mate handling
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "loss,expected",
    [
        (0, GOOD),
        (49, GOOD),
        (50, INACCURACY),      # lower edge inclusive
        (99, INACCURACY),
        (100, MISTAKE),
        (299, MISTAKE),
        (300, BLUNDER),
        (5000, BLUNDER),
    ],
)
def test_classification_boundaries(loss: int, expected: str) -> None:
    assert classify(loss, "e2e4", "d2d4", T) == expected


def test_engine_best_move_is_BEST_even_with_small_measured_loss() -> None:
    """Two independent fixed-depth searches can disagree by a few centipawns."""
    assert classify(0, "e2e4", "e2e4", T) == BEST
    assert classify(30, "e2e4", "e2e4", T) == BEST


def test_matching_best_move_does_not_mask_a_real_blunder_from_another_move() -> None:
    assert classify(400, "e2e4", "d2d4", T) == BLUNDER


def test_classification_without_a_best_move_falls_back_to_thresholds() -> None:
    assert classify(400, "e2e4", None, T) == BLUNDER
    assert classify(10, "e2e4", None, T) == GOOD


def test_classification_is_deterministic() -> None:
    calls = [classify(120, "e2e4", "d2d4", T) for _ in range(50)]
    assert set(calls) == {MISTAKE}


def test_thresholds_must_be_strictly_increasing() -> None:
    with pytest.raises(ValueError):
        Thresholds(inaccuracy_cp=100, mistake_cp=100, blunder_cp=300)
    with pytest.raises(ValueError):
        Thresholds(inaccuracy_cp=0, mistake_cp=100, blunder_cp=300)


def test_mate_scores_map_to_a_magnitude_above_any_material_eval() -> None:
    mate_in_3 = score_to_evaluation(
        chess.engine.PovScore(chess.engine.Mate(3), chess.WHITE), chess.WHITE
    )
    mate_in_1 = score_to_evaluation(
        chess.engine.PovScore(chess.engine.Mate(1), chess.WHITE), chess.WHITE
    )
    assert mate_in_3.is_mate and mate_in_3.mate == 3
    assert mate_in_1.cp > mate_in_3.cp > 2000     # faster mate ranks higher
    getting_mated = score_to_evaluation(
        chess.engine.PovScore(chess.engine.Mate(-2), chess.WHITE), chess.WHITE
    )
    assert getting_mated.cp < -2000 and getting_mated.mate == -2


def test_throwing_away_a_forced_mate_is_a_blunder() -> None:
    before = score_to_evaluation(
        chess.engine.PovScore(chess.engine.Mate(2), chess.WHITE), chess.WHITE
    )
    after = Evaluation(cp=20)
    loss = evaluation_loss(before, after, CAP)
    assert loss == CAP - 20
    assert classify(loss, "a2a3", "h5f7", T) == BLUNDER


def test_converting_a_mate_faster_is_not_penalized() -> None:
    """Mate in 5 -> mate in 3 must not register as a loss."""
    before = score_to_evaluation(
        chess.engine.PovScore(chess.engine.Mate(5), chess.WHITE), chess.WHITE
    )
    after = score_to_evaluation(
        chess.engine.PovScore(chess.engine.Mate(3), chess.WHITE), chess.WHITE
    )
    assert evaluation_loss(before, after, CAP) == 0


def test_walking_into_a_mate_is_a_blunder() -> None:
    before = Evaluation(cp=0)
    after = score_to_evaluation(
        chess.engine.PovScore(chess.engine.Mate(-1), chess.WHITE), chess.WHITE
    )
    loss = evaluation_loss(before, after, CAP)
    assert loss == CAP
    assert classify(loss, "g2g4", "d2d4", T) == BLUNDER


def test_terminal_evaluation_checkmate() -> None:
    # Fool's mate: White is checkmated, Black delivered it.
    board = chess.Board()
    for san in ["f3", "e5", "g4", "Qh4#"]:
        board.push_san(san)
    assert board.is_checkmate()
    assert terminal_evaluation(board, chess.WHITE) == Evaluation(-MATE_SCORE_CP, 0)
    assert terminal_evaluation(board, chess.BLACK) == Evaluation(MATE_SCORE_CP, 0)


def test_terminal_evaluation_stalemate_is_a_draw_for_both() -> None:
    board = chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
    assert board.is_stalemate()
    assert terminal_evaluation(board, chess.WHITE) == Evaluation(0, None)
    assert terminal_evaluation(board, chess.BLACK) == Evaluation(0, None)


def test_terminal_evaluation_refuses_a_live_position() -> None:
    with pytest.raises(EngineError):
        terminal_evaluation(chess.Board(), chess.WHITE)


# --------------------------------------------------------------------------
# Phase detection (spec section 9)
# --------------------------------------------------------------------------

def test_starting_material_is_62() -> None:
    assert phase_material(chess.Board()) == STARTING_PHASE_MATERIAL == 62


def test_opening_then_middlegame_by_ply() -> None:
    board = chess.Board()
    assert detect_phase(board, 1, RULES) == OPENING
    assert detect_phase(board, 20, RULES) == OPENING
    # Same untouched material, but past the opening ply window.
    assert detect_phase(board, 21, RULES) == MIDDLEGAME


def test_early_heavy_trades_leave_the_opening_immediately() -> None:
    # Both queens gone at ply 8: material 44, below opening_min_material.
    board = chess.Board("rnb1kbnr/pppppppp/8/8/8/8/PPPPPPPP/RNB1KBNR w KQkq - 0 1")
    assert phase_material(board) == 44
    assert detect_phase(board, 8, RULES) == MIDDLEGAME


def test_endgame_by_material() -> None:
    # King + rook vs king + rook = 10.
    board = chess.Board("4k2r/8/8/8/8/8/8/R3K3 w Qk - 0 1")
    assert phase_material(board) == 10
    assert detect_phase(board, 5, RULES) == ENDGAME   # material wins over ply
    assert detect_phase(board, 90, RULES) == ENDGAME


def test_endgame_boundary_is_inclusive() -> None:
    at_limit = chess.Board("4k3/8/8/8/8/8/8/RRRR1K2 w - - 0 1")     # 4 rooks = 20
    assert phase_material(at_limit) == 20
    assert detect_phase(at_limit, 40, RULES) == ENDGAME

    above = chess.Board("4k3/8/8/8/8/8/8/RRRRRK2 w - - 0 1")        # 5 rooks = 25
    assert phase_material(above) == 25
    assert detect_phase(above, 40, RULES) == MIDDLEGAME


def test_phase_is_deterministic() -> None:
    board = chess.Board()
    assert len({detect_phase(board, 5, RULES) for _ in range(50)}) == 1


# --------------------------------------------------------------------------
# Ply -> move number / color
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "ply,expected",
    [(1, (1, "white")), (2, (1, "black")), (3, (2, "white")),
     (4, (2, "black")), (93, (47, "white"))],
)
def test_move_number_and_color(ply: int, expected: tuple[int, str]) -> None:
    assert move_number_and_color(ply) == expected


def test_move_number_rejects_zero_ply() -> None:
    with pytest.raises(ValueError):
        move_number_and_color(0)
