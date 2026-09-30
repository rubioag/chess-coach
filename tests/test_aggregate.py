"""Descriptive aggregation. It must count honestly and score nothing."""

from __future__ import annotations

from chess_coach.aggregate import report
from chess_coach.analysis import analyze
from chess_coach.db import init_db

from .helpers import SCHOLARS_MATE, StubEngine, seed_game


def test_report_counts_only_the_canonical_run(config) -> None:
    """A game analysed three times must contribute one set of rows, not three."""
    seed_game(config, SCHOLARS_MATE)
    analyze(config, engine_factory=StubEngine, log=lambda _: None)
    analyze(config, engine_factory=StubEngine, force=True, log=lambda _: None)
    last = analyze(config, engine_factory=StubEngine, depth=18, force=True, log=lambda _: None)

    conn = init_db(config.database)
    assert conn.execute("SELECT COUNT(*) FROM move_analysis").fetchone()[0] == 21
    assert conn.execute("SELECT COUNT(*) FROM current_move_analysis").fetchone()[0] == 7

    text = report(conn)
    assert "observation rows (all runs)  : 21" in text
    assert "observation rows (canonical) : 7" in text
    # All three runs are listed, so the history stays visible.
    assert "run 1" in text and "run 2" in text and f"run {last.run_id}" in text
    conn.close()


def test_report_restricts_breakdowns_to_the_users_own_moves(config) -> None:
    seed_game(config, SCHOLARS_MATE)      # player_color = 'white'
    analyze(config, engine_factory=StubEngine, log=lambda _: None)

    conn = init_db(config.database)
    own = conn.execute(
        """SELECT COUNT(*) FROM current_move_analysis c JOIN games g ON g.id = c.game_id
            WHERE c.color = g.player_color"""
    ).fetchone()[0]
    assert own == 4                        # 4 white moves out of 7 plies
    assert f"the user's own moves" in report(conn)
    assert f": {own}" in report(conn)
    conn.close()


def test_report_contains_no_scoring_or_recommendation(config) -> None:
    """Guards the boundary in ARCHITECTURE.md section 2."""
    seed_game(config, SCHOLARS_MATE)
    analyze(config, engine_factory=StubEngine, log=lambda _: None)

    conn = init_db(config.database)
    text = report(conn)
    # The closing disclaimer names these concepts in order to disown them, so
    # the body is checked separately from the note that follows it.
    body = text.split("NOTE:")[0].lower()
    for forbidden in (
        "weakness score", "priority score", "mastery", "we recommend",
        "you should play", "your weakness", "recommended opening",
    ):
        assert forbidden not in body
    # ... and the report says so out loud.
    assert "no weakness, priority, mastery or opening" in text.lower()
    conn.close()


def test_report_survives_an_empty_database(config) -> None:
    conn = init_db(config.database)
    text = report(conn)
    assert "games imported" in text
    conn.close()


def test_report_includes_clock_and_opening_sections(config) -> None:
    seed_game(config, SCHOLARS_MATE)
    analyze(config, engine_factory=StubEngine, log=lambda _: None)
    conn = init_db(config.database)
    text = report(conn)
    assert "PERFORMANCE BY TIME REMAINING" in text
    assert "OPENING FAMILIES" in text
    assert "EVALUATION LOSS DISTRIBUTION" in text
    assert "PERFORMANCE BY PHASE" in text
    conn.close()
