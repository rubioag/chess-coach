"""Shared test fixtures: a stub engine and DB seeding helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import chess

from chess_coach.db import init_db
from chess_coach.engine import MATE_SCORE_CP, EngineError, PositionAnalysis
from chess_coach.evaluation import Evaluation

SCHOLARS_MATE = """[Event "t"]
[White "TestPlayer"]
[Black "Opponent"]
[Result "1-0"]
[TimeControl "300+5"]

1. e4 {[%clk 0:04:58]} 1... e5 {[%clk 0:04:57]} 2. Bc4 {[%clk 0:04:55]} 2... Nc6 {[%clk 0:04:50]} 3. Qh5 {[%clk 0:04:52]} 3... Nf6 {[%clk 0:04:40]} 4. Qxf7# {[%clk 0:04:50]} 1-0
"""

NO_CLOCK_GAME = """[Event "t"]
[White "TestPlayer"]
[Black "Opponent"]
[Result "1-0"]
[TimeControl "600"]

1. e4 e5 2. Nf3 Nc6 1-0
"""


class StubEngine:
    """Duck-types StockfishEngine. Scores come from a scripted list."""

    def __init__(self, scores=None, fail_on_fen=None, name="StubEngine 1.0"):
        self.scores = list(scores) if scores else None
        self.fail_on_fen = fail_on_fen
        self.calls = 0
        self.name = name
        self.closed = False

    # Mirrors StockfishEngine's provenance properties.
    @property
    def engine_name(self) -> str:
        return self.name.split()[0] if self.name.split() else self.name

    @property
    def engine_version(self) -> str:
        parts = self.name.split(maxsplit=1)
        return parts[1] if len(parts) > 1 else "unknown"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def analyse_position(self, board: chess.Board, depth: int) -> PositionAnalysis:
        if self.fail_on_fen is not None and board.fen() == self.fail_on_fen:
            raise EngineError("stub engine failure")
        self.calls += 1
        if board.is_game_over(claim_draw=False):
            outcome = board.outcome(claim_draw=False)
            cp = 0 if outcome.winner is None else -MATE_SCORE_CP
            mate = None if outcome.winner is None else 0
            return PositionAnalysis(Evaluation(cp, mate), None, 0, ())

        cp = self.scores.pop(0) if self.scores else 0
        # A short but real PV: the first legal move, then a reply if one exists.
        pv = [next(iter(board.legal_moves))]
        probe = board.copy()
        probe.push(pv[0])
        if probe.legal_moves:
            pv.append(next(iter(probe.legal_moves)))
        return PositionAnalysis(Evaluation(cp), pv[0], depth, tuple(pv))


def seed_game(
    config,
    pgn_text: str = SCHOLARS_MATE,
    external_id: str = "https://www.chess.com/game/live/1",
    name: str = "1",
    time_control: str | None = "300+5",
):
    """Insert one game row plus its raw PGN file, mimicking ingestion output."""
    path = config.raw_games_dir / "2024" / "03" / f"{name}.pgn"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pgn_text, encoding="utf-8")

    # Mirror what ingestion stores: the platform's ECO header, if the PGN has one.
    eco = None
    for line in pgn_text.splitlines():
        if line.startswith('[ECO "'):
            eco = line.split('"')[1]
            break

    conn = init_db(config.database)
    conn.execute(
        """INSERT INTO games (external_game_id, platform, pgn_path, archive_year,
                              archive_month, imported_at, analysis_status,
                              player_color, time_control, ply_count, eco)
           VALUES (?, 'chess.com', ?, 2024, 3, '2024-03-01T00:00:00+00:00',
                   'pending', 'white', ?, 0, ?)""",
        (external_id, str(path), time_control, eco),
    )
    conn.commit()
    gid = conn.execute(
        "SELECT id FROM games WHERE external_game_id = ?", (external_id,)
    ).fetchone()[0]
    conn.close()
    return gid, path


def raw_rows(config, game_id: int) -> list[sqlite3.Row]:
    conn = init_db(config.database)
    try:
        return conn.execute(
            "SELECT ply, uci, san, fen_before, fen_after FROM game_moves "
            "WHERE game_id = ? ORDER BY ply",
            (game_id,),
        ).fetchall()
    finally:
        conn.close()
