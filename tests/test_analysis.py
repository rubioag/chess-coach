"""Analysis orchestration: provenance, observations, resume, failure handling.

These tests use a stub engine so they exercise the state machine
deterministically and without a Stockfish binary. The real engine is verified
separately against real games.
"""

from __future__ import annotations

import chess
import pytest

from chess_coach.analysis import (
    AnalysisError,
    analyze,
    analyze_raw_moves,
    open_run,
    reset_stale_running,
    select_games,
)
from chess_coach.db import init_db
from chess_coach.engine import MATE_SCORE_CP, EngineError, PositionAnalysis
from chess_coach.evaluation import RULES_VERSION, BEST, OPENING, Evaluation
from chess_coach.moves import extract_moves

from .helpers import SCHOLARS_MATE, StubEngine, raw_rows, seed_game


# --------------------------------------------------------------------------
# Observations
# --------------------------------------------------------------------------

def test_one_search_per_position(config) -> None:
    """N moves need N+1 searches, never 2N: each position is scored once."""
    gid, _ = seed_game(config, SCHOLARS_MATE)
    extract_moves(config, log=lambda _: None)
    engine = StubEngine()
    records = analyze_raw_moves(raw_rows(config, gid), engine, config, depth=14)
    assert len(records) == 7
    assert engine.calls == 8


def test_records_carry_every_required_field(config) -> None:
    gid, _ = seed_game(config, SCHOLARS_MATE)
    extract_moves(config, log=lambda _: None)
    records = analyze_raw_moves(raw_rows(config, gid), StubEngine(), config, depth=14)

    first = records[0]
    assert first.ply == 1
    assert first.phase == OPENING
    assert first.analysis_depth == 14
    assert first.pv and first.pv.split()[0] == first.best_move
    assert first.pv_length == len(first.pv.split())
    assert first.pv_san


def test_perspective_alternates_between_movers(config) -> None:
    """after(i) flipped must equal before(i+1): same position, other side."""
    gid, _ = seed_game(config, SCHOLARS_MATE)
    extract_moves(config, log=lambda _: None)
    records = analyze_raw_moves(
        raw_rows(config, gid),
        StubEngine(scores=[30, -40, 55, -60, 70, -80, 90, 100]),
        config,
        depth=14,
    )
    for previous, nxt in zip(records, records[1:]):
        assert -previous.evaluation_after == nxt.evaluation_before


def test_final_checkmate_is_recorded_as_mate_delivered(config) -> None:
    gid, _ = seed_game(config, SCHOLARS_MATE)
    extract_moves(config, log=lambda _: None)
    records = analyze_raw_moves(raw_rows(config, gid), StubEngine(), config, depth=14)
    last = records[-1]
    assert last.evaluation_after == MATE_SCORE_CP
    assert last.mate_in_after == 0
    assert last.evaluation_loss == 0
    # The PV belongs to the position BEFORE the move, which is not terminal;
    # the terminal search that follows contributes only evaluation_after.
    assert last.pv is not None


def test_pv_starts_with_best_move_and_is_legal(config) -> None:
    gid, _ = seed_game(config, SCHOLARS_MATE)
    extract_moves(config, log=lambda _: None)
    rows = raw_rows(config, gid)
    records = analyze_raw_moves(rows, StubEngine(), config, depth=14)

    for row, record in zip(rows, records):
        if not record.pv:
            continue
        ucis = record.pv.split()
        assert ucis[0] == record.best_move
        board = chess.Board(row["fen_before"])
        for uci in ucis:
            move = chess.Move.from_uci(uci)
            assert move in board.legal_moves
            board.push(move)


def test_analysis_refuses_inconsistent_raw_rows(config) -> None:
    """The observation layer must not drift from the raw layer."""
    gid, _ = seed_game(config, SCHOLARS_MATE)
    extract_moves(config, log=lambda _: None)
    rows = [dict(r) for r in raw_rows(config, gid)]
    rows[0]["fen_before"] = chess.Board("8/8/8/8/8/8/8/K6k w - - 0 1").fen()
    with pytest.raises(AnalysisError):
        analyze_raw_moves(rows, StubEngine(), config, depth=14)


def test_analysis_without_raw_moves_is_rejected(config) -> None:
    with pytest.raises(AnalysisError):
        analyze_raw_moves([], StubEngine(), config, depth=14)


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------

def test_run_records_every_parameter_that_shapes_a_measurement(config) -> None:
    seed_game(config, SCHOLARS_MATE)
    report = analyze(config, engine_factory=StubEngine, log=lambda _: None)

    conn = init_db(config.database)
    run = conn.execute(
        "SELECT * FROM analysis_runs WHERE id = ?", (report.run_id,)
    ).fetchone()
    assert run["engine_name"] == "StubEngine"
    assert run["engine_version"] == "1.0"
    assert run["engine_id_string"] == "StubEngine 1.0"
    assert run["depth"] == 14
    assert run["multipv"] == config.stockfish.multipv
    assert run["threads"] == config.stockfish.threads
    assert run["hash_mb"] == config.stockfish.hash_mb
    assert run["eval_cap_cp"] == config.analysis.eval_cap_cp
    assert run["rules_version"] == RULES_VERSION
    assert run["inaccuracy_cp"] == config.analysis.thresholds.inaccuracy_cp
    assert run["mistake_cp"] == config.analysis.thresholds.mistake_cp
    assert run["blunder_cp"] == config.analysis.thresholds.blunder_cp
    assert run["phase_opening_max_ply"] == config.analysis.phase_rules.opening_max_ply
    assert run["started_at"] and run["finished_at"]
    assert run["status"] == "completed"
    assert run["moves_analyzed"] == 7
    conn.close()


def test_every_observation_is_attributed_to_a_run(config) -> None:
    seed_game(config, SCHOLARS_MATE)
    report = analyze(config, engine_factory=StubEngine, log=lambda _: None)
    conn = init_db(config.database)
    assert conn.execute(
        "SELECT COUNT(*) FROM move_analysis WHERE run_id IS NOT ?", (report.run_id,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT analysis_run_id FROM games"
    ).fetchone()[0] == report.run_id
    conn.close()


def test_reanalysis_adds_a_run_and_keeps_the_previous_one(config) -> None:
    """Re-analysis must never destroy an earlier measurement."""
    gid, _ = seed_game(config, SCHOLARS_MATE)
    first = analyze(config, engine_factory=StubEngine, log=lambda _: None)
    second = analyze(
        config, engine_factory=StubEngine, depth=18, force=True, log=lambda _: None
    )
    assert first.run_id != second.run_id

    conn = init_db(config.database)
    assert conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM move_analysis WHERE run_id = ?", (first.run_id,)
    ).fetchone()[0] == 7
    assert conn.execute(
        "SELECT COUNT(*) FROM move_analysis WHERE run_id = ?", (second.run_id,)
    ).fetchone()[0] == 7
    # Depths coexist rather than overwrite.
    assert {r[0] for r in conn.execute("SELECT DISTINCT analysis_depth FROM move_analysis")} == {14, 18}
    # ... but only the canonical run reaches the view, so nothing double-counts.
    assert conn.execute("SELECT COUNT(*) FROM current_move_analysis").fetchone()[0] == 7
    assert conn.execute(
        "SELECT DISTINCT run_id FROM current_move_analysis"
    ).fetchone()[0] == second.run_id
    conn.close()


def test_a_new_engine_version_needs_no_schema_change(config) -> None:
    seed_game(config, SCHOLARS_MATE)

    def future_engine():
        return StubEngine(name="Stockfish 19.1-dev")

    report = analyze(config, engine_factory=future_engine, log=lambda _: None)
    conn = init_db(config.database)
    run = conn.execute(
        "SELECT engine_name, engine_version FROM analysis_runs WHERE id = ?",
        (report.run_id,),
    ).fetchone()
    assert (run["engine_name"], run["engine_version"]) == ("Stockfish", "19.1-dev")
    conn.close()


def test_retrying_a_run_does_not_duplicate_its_own_rows(config) -> None:
    gid, _ = seed_game(config, SCHOLARS_MATE)
    extract_moves(config, log=lambda _: None)
    conn = init_db(config.database)
    run_id = open_run(conn, config, "StubEngine", "1.0", "StubEngine 1.0", 14)

    from chess_coach.analysis import _store_analysis

    records = analyze_raw_moves(raw_rows(config, gid), StubEngine(), config, 14)
    with conn:
        _store_analysis(conn, run_id, gid, records, 14)
    with conn:
        _store_analysis(conn, run_id, gid, records, 14)   # same run again

    assert conn.execute(
        "SELECT COUNT(*) FROM move_analysis WHERE run_id = ?", (run_id,)
    ).fetchone()[0] == 7
    conn.close()


# --------------------------------------------------------------------------
# End-to-end over the DB
# --------------------------------------------------------------------------

def test_analyze_writes_observations_and_marks_completed(config) -> None:
    gid, _ = seed_game(config, SCHOLARS_MATE)
    report = analyze(config, engine_factory=StubEngine, log=lambda _: None)

    assert (report.games_analyzed, report.games_failed) == (1, 0)
    assert report.moves_analyzed == 7

    conn = init_db(config.database)
    row = conn.execute("SELECT * FROM games WHERE id = ?", (gid,)).fetchone()
    assert row["analysis_status"] == "completed"
    assert row["analysis_depth"] == 14
    assert row["analyzed_at"] is not None and row["analysis_error"] is None
    assert conn.execute(
        "SELECT COUNT(*) FROM move_analysis WHERE game_id = ?", (gid,)
    ).fetchone()[0] == 7
    conn.close()


def test_analyze_extracts_raw_moves_when_missing(config) -> None:
    """The raw layer is a prerequisite, produced on demand from the PGN."""
    gid, _ = seed_game(config, SCHOLARS_MATE)
    conn = init_db(config.database)
    assert conn.execute("SELECT COUNT(*) FROM game_moves").fetchone()[0] == 0
    conn.close()

    analyze(config, engine_factory=StubEngine, log=lambda _: None)
    conn = init_db(config.database)
    assert conn.execute("SELECT COUNT(*) FROM game_moves").fetchone()[0] == 7
    conn.close()


def test_missing_raw_pgn_fails_the_game_not_the_run(config) -> None:
    gid, path = seed_game(config, SCHOLARS_MATE)
    path.unlink()
    report = analyze(config, engine_factory=StubEngine, log=lambda _: None)
    assert report.games_failed == 1
    conn = init_db(config.database)
    assert "raw PGN missing" in conn.execute(
        "SELECT analysis_error FROM games WHERE id = ?", (gid,)
    ).fetchone()[0]
    conn.close()


# --------------------------------------------------------------------------
# Resume behaviour
# --------------------------------------------------------------------------

def test_completed_games_are_not_reanalyzed(config) -> None:
    seed_game(config, SCHOLARS_MATE)
    analyze(config, engine_factory=StubEngine, log=lambda _: None)
    second = analyze(config, engine_factory=StubEngine, log=lambda _: None)
    assert second.games_selected == 0
    assert second.run_id is None      # nothing to do: no run is opened


def test_crash_leaves_running_and_next_run_resets_it(config) -> None:
    """A 'running' game is a crash fingerprint and must become retryable."""
    gid, _ = seed_game(config, SCHOLARS_MATE)
    conn = init_db(config.database)
    conn.execute("UPDATE games SET analysis_status = 'running' WHERE id = ?", (gid,))
    conn.commit()

    assert reset_stale_running(conn) == 1
    assert conn.execute(
        "SELECT analysis_status FROM games WHERE id = ?", (gid,)
    ).fetchone()[0] == "pending"
    conn.close()

    assert analyze(config, engine_factory=StubEngine, log=lambda _: None).games_analyzed == 1


def test_resume_continues_where_it_stopped(config) -> None:
    """Stopping after game 1 must restart at game 2, not at game 1."""
    seed_game(config, SCHOLARS_MATE, "https://www.chess.com/game/live/1")
    seed_game(config, SCHOLARS_MATE, "https://www.chess.com/game/live/2", name="2")
    seed_game(config, SCHOLARS_MATE, "https://www.chess.com/game/live/3", name="3")

    first = analyze(config, engine_factory=StubEngine, limit=1, log=lambda _: None)
    assert first.games_analyzed == 1

    second = analyze(config, engine_factory=StubEngine, log=lambda _: None)
    assert second.games_selected == 2
    assert second.games_analyzed == 2

    conn = init_db(config.database)
    assert conn.execute(
        "SELECT COUNT(*) FROM games WHERE analysis_status = 'completed'"
    ).fetchone()[0] == 3
    # Two separate runs, both preserved.
    assert conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 2
    conn.close()


# --------------------------------------------------------------------------
# Failed-game behaviour
# --------------------------------------------------------------------------

def test_failed_game_is_marked_failed_never_completed(config) -> None:
    gid, path = seed_game(config, SCHOLARS_MATE)
    path.write_text('[Event "t"]\n[Result "*"]\n\n*\n', encoding="utf-8")

    report = analyze(config, engine_factory=StubEngine, log=lambda _: None)
    assert (report.games_analyzed, report.games_failed) == (0, 1)

    conn = init_db(config.database)
    row = conn.execute("SELECT * FROM games WHERE id = ?", (gid,)).fetchone()
    assert row["analysis_status"] == "failed"
    assert row["analysis_error"]
    assert row["analyzed_at"] is None
    assert conn.execute("SELECT COUNT(*) FROM move_analysis").fetchone()[0] == 0
    conn.close()


def test_failed_games_are_skipped_until_retry_failed(config) -> None:
    gid, path = seed_game(config, SCHOLARS_MATE)
    good = path.read_text(encoding="utf-8")
    path.write_text('[Event "t"]\n[Result "*"]\n\n*\n', encoding="utf-8")
    analyze(config, engine_factory=StubEngine, log=lambda _: None)

    assert analyze(config, engine_factory=StubEngine, log=lambda _: None).games_selected == 0

    path.write_text(good, encoding="utf-8")
    retried = analyze(
        config, engine_factory=StubEngine, retry_failed=True, log=lambda _: None
    )
    assert retried.games_analyzed == 1

    conn = init_db(config.database)
    row = conn.execute("SELECT * FROM games WHERE id = ?", (gid,)).fetchone()
    assert row["analysis_status"] == "completed"
    assert row["analysis_error"] is None
    conn.close()


def test_one_bad_game_does_not_stop_the_others(config) -> None:
    seed_game(config, SCHOLARS_MATE, "https://www.chess.com/game/live/1")
    _, bad_path = seed_game(
        config, '[Event "t"]\n[Result "*"]\n\n*\n',
        "https://www.chess.com/game/live/2", name="bad",
    )

    report = analyze(config, engine_factory=StubEngine, log=lambda _: None)
    assert (report.games_analyzed, report.games_failed) == (1, 1)
    assert len(report.errors) == 1

    conn = init_db(config.database)
    # The run still completes and still reports honest counters.
    run = conn.execute(
        "SELECT status, games_analyzed, games_failed FROM analysis_runs WHERE id = ?",
        (report.run_id,),
    ).fetchone()
    assert (run["status"], run["games_analyzed"], run["games_failed"]) == ("completed", 1, 1)
    conn.close()


def test_engine_failure_stops_the_run_and_marks_it_failed(config) -> None:
    """A dead engine must not mark every remaining game failed against it."""
    gid, _ = seed_game(config, SCHOLARS_MATE)
    extract_moves(config, log=lambda _: None)

    def factory():
        return StubEngine(fail_on_fen=chess.Board().fen())

    with pytest.raises(EngineError):
        analyze(config, engine_factory=factory, log=lambda _: None)

    conn = init_db(config.database)
    assert conn.execute(
        "SELECT analysis_status FROM games WHERE id = ?", (gid,)
    ).fetchone()[0] == "failed"
    assert conn.execute(
        "SELECT status FROM analysis_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()[0] == "failed"
    conn.close()


def test_select_games_honours_status_filters(config) -> None:
    seed_game(config, SCHOLARS_MATE)
    conn = init_db(config.database)
    assert len(select_games(conn)) == 1

    conn.execute("UPDATE games SET analysis_status = 'failed'")
    conn.commit()
    assert select_games(conn) == []
    assert len(select_games(conn, retry_failed=True)) == 1

    conn.execute("UPDATE games SET analysis_status = 'completed'")
    conn.commit()
    assert select_games(conn) == []
    assert len(select_games(conn, force=True)) == 1
    conn.close()


def test_select_games_rejects_unknown_game_id(config) -> None:
    seed_game(config, SCHOLARS_MATE)
    conn = init_db(config.database)
    with pytest.raises(AnalysisError):
        select_games(conn, game_id=999)
    conn.close()
