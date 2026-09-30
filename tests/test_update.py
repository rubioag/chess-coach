"""Integration tests for `update`: the thin ingest -> extract -> analyze cycle.

The point of these is not that the three stages work - they have their own
tests. It is that chaining them changes nothing: repeated runs must not
duplicate rows, must not destroy anything, and must stop rather than continue
past a broken stage.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from chess_coach import update as update_module
from chess_coach.chesscom import ArchiveResponse, ChessComError
from chess_coach.db import init_db
from chess_coach.update import update

from .helpers import SCHOLARS_MATE, StubEngine

# The CURRENT month: it is never marked `complete`, which is exactly the
# scenario `update` exists for - coming back after playing and re-checking the
# month still in progress. A past month that parsed cleanly is closed for good
# and would never be refetched.
_NOW = datetime.now(tz=timezone.utc)
ARCHIVE = (
    "https://api.chess.com/pub/player/testplayer/games/"
    f"{_NOW.year:04d}/{_NOW.month:02d}"
)

PGN_TEMPLATE = """[Event "Live Chess"]
[Site "Chess.com"]
[Date "2024.03.01"]
[White "TestPlayer"]
[Black "Opponent{n}"]
[Result "1-0"]
[WhiteElo "1500"]
[BlackElo "1480"]
[TimeControl "300+5"]
[ECO "C20"]
[Termination "TestPlayer won by resignation"]
[UTCDate "2024.03.01"]
[UTCTime "12:00:00"]

1. e4 {{[%clk 0:04:58]}} 1... e5 {{[%clk 0:04:57]}} 2. Bc4 {{[%clk 0:04:55]}} 2... Nc6 {{[%clk 0:04:50]}} 3. Qh5 {{[%clk 0:04:52]}} 3... Nf6 {{[%clk 0:04:40]}} 4. Qxf7# {{[%clk 0:04:50]}} 1-0
"""


def game(n: int) -> dict:
    return {
        "url": f"https://www.chess.com/game/live/{n}",
        "pgn": PGN_TEMPLATE.format(n=n),
        "time_class": "blitz",
        "rated": True,
        "end_time": 1709294400 + n,
    }


class FakeClient:
    """Serves a fixed archive. Can be told to fail the way the network does."""

    def __init__(self, games, raise_on_list=None):
        self.games = list(games)
        self.raise_on_list = raise_on_list
        self.calls = 0

    def list_archives(self):
        if self.raise_on_list:
            raise self.raise_on_list
        return [ARCHIVE]

    def fetch_archive(self, archive_url, etag=None):
        self.calls += 1
        return ArchiveResponse(200, 'W/"etag-1"', list(self.games))


def counts(config) -> dict:
    conn = init_db(config.database)
    try:
        return {
            "games": conn.execute("SELECT COUNT(*) FROM games").fetchone()[0],
            "raw": conn.execute("SELECT COUNT(*) FROM game_moves").fetchone()[0],
            "obs": conn.execute("SELECT COUNT(*) FROM move_analysis").fetchone()[0],
            "runs": conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0],
            "canonical": conn.execute(
                "SELECT COUNT(*) FROM current_move_analysis"
            ).fetchone()[0],
            "completed": conn.execute(
                "SELECT COUNT(*) FROM games WHERE analysis_status = 'completed'"
            ).fetchone()[0],
        }
    finally:
        conn.close()


def run(config, client, **kwargs):
    return update(
        config, client=client, engine_factory=StubEngine, log=lambda _: None, **kwargs
    )


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------

def test_update_runs_all_three_stages_in_order(config) -> None:
    report = run(config, FakeClient([game(1), game(2)]))

    assert report.stages_run == ["ingest", "extract-moves", "analyze"]
    assert not report.aborted
    assert report.games_new == 2
    assert report.moves_extracted == 14        # 7 plies x 2 games
    assert report.games_analyzed == 2
    assert report.moves_analyzed == 14
    assert report.total_failures == 0
    assert report.run_id is not None
    assert report.seconds >= 0

    assert counts(config) == {
        "games": 2, "raw": 14, "obs": 14, "runs": 1, "canonical": 14, "completed": 2,
    }


def test_update_summary_reports_the_required_numbers(config) -> None:
    report = run(config, FakeClient([game(1)]))
    text = report.summary()
    for label in ("new games", "pending before", "games analyzed",
                  "moves analyzed", "failures", "total time"):
        assert label in text


# --------------------------------------------------------------------------
# Idempotency: the whole reason this command exists
# --------------------------------------------------------------------------

def test_running_update_twice_duplicates_nothing(config) -> None:
    client = FakeClient([game(1), game(2)])
    run(config, client)
    first = counts(config)

    second_report = run(config, client)
    assert counts(config) == first          # byte-for-byte the same row counts
    assert second_report.games_new == 0
    assert second_report.games_analyzed == 0
    assert second_report.nothing_to_do
    assert "analyze" not in second_report.stages_run   # no empty run opened


def test_running_update_five_times_is_stable(config) -> None:
    client = FakeClient([game(1), game(2), game(3)])
    run(config, client)
    baseline = counts(config)
    for _ in range(4):
        run(config, client)
    assert counts(config) == baseline

    conn = init_db(config.database)
    assert conn.execute(
        "SELECT COUNT(*) FROM (SELECT game_id, ply FROM game_moves "
        "GROUP BY 1,2 HAVING COUNT(*) > 1)"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM (SELECT run_id, game_id, ply FROM move_analysis "
        "GROUP BY 1,2,3 HAVING COUNT(*) > 1)"
    ).fetchone()[0] == 0
    conn.close()


def test_update_never_destroys_earlier_analysis(config) -> None:
    client = FakeClient([game(1)])
    first = run(config, client)

    client.games.append(game(2))
    second = run(config, client)

    conn = init_db(config.database)
    # The first game's original observations are still there, under their run.
    assert conn.execute(
        "SELECT COUNT(*) FROM move_analysis WHERE run_id = ?", (first.run_id,)
    ).fetchone()[0] == 7
    assert second.run_id != first.run_id
    assert conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 2
    conn.close()


def test_update_never_rewrites_the_raw_pgns(config) -> None:
    client = FakeClient([game(1), game(2)])
    run(config, client)
    before = {
        p: p.read_bytes() for p in sorted(config.raw_games_dir.rglob("*.pgn"))
    }
    assert before

    run(config, client)
    run(config, client)
    after = {p: p.read_bytes() for p in sorted(config.raw_games_dir.rglob("*.pgn"))}
    assert after == before


def test_new_games_are_picked_up_on_a_later_cycle(config) -> None:
    client = FakeClient([game(1)])
    run(config, client)

    client.games.append(game(2))
    report = run(config, client)
    assert report.games_new == 1
    assert report.games_analyzed == 1          # only the new one
    assert counts(config)["completed"] == 2


def test_nothing_to_do_on_an_empty_account(config) -> None:
    report = run(config, FakeClient([]))
    assert not report.aborted
    assert report.games_new == 0
    assert report.nothing_to_do
    assert report.games_analyzed == 0
    assert counts(config)["runs"] == 0         # no empty analysis run created


# --------------------------------------------------------------------------
# Stage failures must stop the pipeline
# --------------------------------------------------------------------------

def test_ingest_failure_aborts_before_the_other_stages(config) -> None:
    client = FakeClient([game(1)], raise_on_list=ChessComError("network down"))
    report = run(config, client)

    assert report.aborted
    assert report.failed_stage == "ingest"
    assert "network down" in report.error
    assert report.stages_run == []
    assert counts(config)["raw"] == 0
    assert counts(config)["runs"] == 0


def test_extract_failure_aborts_before_analyze(config, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("raw layer is broken")

    monkeypatch.setattr(update_module, "extract_moves", boom)
    report = run(config, FakeClient([game(1)]))

    assert report.aborted
    assert report.failed_stage == "extract-moves"
    assert report.stages_run == ["ingest"]
    assert counts(config)["runs"] == 0          # analyze never ran
    assert counts(config)["games"] == 1         # ingest's work is kept


def test_analyze_failure_is_reported_not_swallowed(config, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("engine died")

    monkeypatch.setattr(update_module, "analyze", boom)
    report = run(config, FakeClient([game(1)]))

    assert report.aborted
    assert report.failed_stage == "analyze"
    assert "engine died" in report.error
    assert report.stages_run == ["ingest", "extract-moves"]
    # The raw layer survives, so the next cycle resumes from it.
    assert counts(config)["raw"] == 7


def test_an_aborted_cycle_recovers_on_the_next_one(config, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("engine died")

    client = FakeClient([game(1)])
    monkeypatch.setattr(update_module, "analyze", boom)
    assert run(config, client).aborted

    monkeypatch.undo()
    report = run(config, client)
    assert not report.aborted
    assert report.games_new == 0                # already ingested
    assert report.games_analyzed == 1           # picked up where it stopped
    assert counts(config)["completed"] == 1


# --------------------------------------------------------------------------
# Item failures are surfaced but do not stop the cycle
# --------------------------------------------------------------------------

def test_one_bad_game_does_not_abort_the_cycle(config) -> None:
    broken = game(2)
    broken["pgn"] = '[Event "t"]\n[Result "*"]\n\n*\n'
    report = run(config, FakeClient([game(1), broken]))

    assert not report.aborted
    assert report.stages_run == ["ingest", "extract-moves", "analyze"]
    assert report.games_analyzed == 1
    assert report.total_failures >= 1
    assert report.errors


def test_retry_failed_is_passed_through(config) -> None:
    broken = game(2)
    broken["pgn"] = PGN_TEMPLATE.format(n=2)
    client = FakeClient([game(1), broken])
    run(config, client)

    conn = init_db(config.database)
    conn.execute(
        "UPDATE games SET analysis_status = 'failed', analysis_error = 'x' WHERE id = 2"
    )
    conn.commit()
    conn.close()

    skipped = run(config, client)
    assert skipped.games_analyzed == 0

    retried = run(config, client, retry_failed=True)
    assert retried.games_analyzed == 1
    assert counts(config)["completed"] == 2


def test_update_adds_no_analysis_logic_of_its_own(config) -> None:
    """Guards the boundary: `update` orchestrates, it does not decide anything."""
    import inspect

    source = inspect.getsource(update_module)
    for forbidden in (
        "classify", "evaluation_loss", "detect_phase", "Thresholds",
        "pattern", "weakness", "priority", "mastery",
    ):
        assert forbidden not in source
