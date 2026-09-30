"""Raw move extraction: the layer that owes nothing to an engine."""

from __future__ import annotations

import io

import chess
import chess.pgn
import pytest

from chess_coach.db import init_db
from chess_coach.moves import (
    MoveExtractionError,
    extract_moves,
    extract_raw_moves,
    read_game_pgn,
)

from .helpers import NO_CLOCK_GAME, SCHOLARS_MATE, seed_game


def parse(pgn_text: str) -> chess.pgn.Game:
    return chess.pgn.read_game(io.StringIO(pgn_text))


def test_extract_raw_moves_replays_the_game() -> None:
    raws = extract_raw_moves(parse(SCHOLARS_MATE))
    assert [r.san for r in raws] == ["e4", "e5", "Bc4", "Nc6", "Qh5", "Nf6", "Qxf7#"]
    assert [r.color for r in raws][:3] == ["white", "black", "white"]
    assert raws[0].fen_before == chess.Board().fen()
    for previous, nxt in zip(raws, raws[1:]):
        assert previous.fen_after == nxt.fen_before


def test_extract_raw_moves_carries_clock_columns() -> None:
    raws = extract_raw_moves(parse(SCHOLARS_MATE))
    # White: 300.0 -> 298.0 with a 5 s increment means 7 s of thought.
    assert raws[0].clock_before_ms == 300_000
    assert raws[0].clock_after_ms == 298_000
    assert raws[0].time_spent_ms == 7_000
    # Ply 3 is White again, starting from their ply-1 clock.
    assert raws[2].clock_before_ms == 298_000


def test_game_without_clocks_still_extracts(caplog) -> None:
    raws = extract_raw_moves(parse(NO_CLOCK_GAME))
    assert len(raws) == 4
    assert all(r.clock_after_ms is None for r in raws)
    assert all(r.time_spent_ms is None for r in raws)
    # The base time is still known, so the first plies have a "before".
    assert raws[0].clock_before_ms == 600_000


def test_empty_game_is_rejected() -> None:
    with pytest.raises(MoveExtractionError):
        extract_raw_moves(parse('[Event "t"]\n[Result "*"]\n\n*\n'))


def test_missing_pgn_file_is_reported(tmp_path) -> None:
    with pytest.raises(MoveExtractionError):
        read_game_pgn(str(tmp_path / "nope.pgn"))


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def test_extract_moves_populates_the_table_and_time_control(config) -> None:
    gid, _ = seed_game(config, SCHOLARS_MATE)
    report = extract_moves(config, log=lambda _: None)
    assert (report.games_processed, report.games_failed) == (1, 0)
    assert report.moves_written == 7
    assert report.moves_with_clock == 7

    conn = init_db(config.database)
    game = conn.execute("SELECT * FROM games WHERE id = ?", (gid,)).fetchone()
    assert (game["base_time_ms"], game["increment_ms"]) == (300_000, 5_000)
    assert game["moves_extracted_at"] is not None
    rows = conn.execute(
        "SELECT * FROM game_moves WHERE game_id = ? ORDER BY ply", (gid,)
    ).fetchall()
    assert [r["ply"] for r in rows] == [1, 2, 3, 4, 5, 6, 7]
    conn.close()


def test_extraction_is_idempotent_and_never_duplicates(config) -> None:
    gid, _ = seed_game(config, SCHOLARS_MATE)
    extract_moves(config, log=lambda _: None)
    extract_moves(config, force=True, log=lambda _: None)
    extract_moves(config, force=True, log=lambda _: None)

    conn = init_db(config.database)
    assert conn.execute(
        "SELECT COUNT(*) FROM game_moves WHERE game_id = ?", (gid,)
    ).fetchone()[0] == 7
    assert conn.execute(
        "SELECT COUNT(*) FROM (SELECT game_id, ply FROM game_moves "
        "GROUP BY 1,2 HAVING COUNT(*) > 1)"
    ).fetchone()[0] == 0
    conn.close()


def test_second_run_skips_games_that_already_have_moves(config) -> None:
    seed_game(config, SCHOLARS_MATE)
    extract_moves(config, log=lambda _: None)
    again = extract_moves(config, log=lambda _: None)
    assert again.games_processed == 0


def test_extraction_never_touches_the_raw_pgn(config) -> None:
    """The source of truth stays byte-identical across re-extraction."""
    _, path = seed_game(config, SCHOLARS_MATE)
    before = path.read_bytes()
    extract_moves(config, force=True, log=lambda _: None)
    extract_moves(config, force=True, log=lambda _: None)
    assert path.read_bytes() == before


def test_one_bad_game_does_not_stop_extraction(config) -> None:
    seed_game(config, SCHOLARS_MATE, "https://www.chess.com/game/live/1")
    seed_game(
        config, '[Event "t"]\n[Result "*"]\n\n*\n',
        "https://www.chess.com/game/live/2", name="bad",
    )
    report = extract_moves(config, log=lambda _: None)
    assert (report.games_processed, report.games_failed) == (1, 1)
    assert len(report.errors) == 1
