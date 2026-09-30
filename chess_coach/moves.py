"""Raw move extraction: immutable PGN on disk -> `game_moves` rows.

This is the RAW layer of the integrity ladder. Everything written here is an
objective fact about the game as played - which move, from which position, with
how much time on the clock. No engine is involved, so these rows are unaffected
by re-analysis, engine upgrades or rule changes, and they can be rebuilt at any
time from the PGN files without touching the network.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import chess
import chess.pgn

from .clocks import TimeControl, clock_readings, derive_clock_columns, parse_time_control
from .config import PROJECT_ROOT, Config
from .db import open_db
from .evaluation import move_number_and_color
from .pgn_parser import PgnParseError


class MoveExtractionError(RuntimeError):
    pass


@dataclass
class RawMove:
    ply: int
    move_number: int
    color: str
    san: str
    uci: str
    fen_before: str
    fen_after: str
    clock_before_ms: int | None
    clock_after_ms: int | None
    time_spent_ms: int | None


@dataclass
class ExtractionReport:
    games_processed: int = 0
    games_failed: int = 0
    moves_written: int = 0
    moves_with_clock: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        coverage = (
            100.0 * self.moves_with_clock / self.moves_written
            if self.moves_written
            else 0.0
        )
        return (
            f"games: {self.games_processed} processed, {self.games_failed} failed\n"
            f"moves written: {self.moves_written}\n"
            f"clock coverage: {self.moves_with_clock}/{self.moves_written} "
            f"({coverage:.1f}%)"
        )


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def resolve_pgn_path(stored: str) -> Path:
    path = Path(stored)
    return path if path.is_absolute() else PROJECT_ROOT / path


def read_game_pgn(stored_path: str) -> chess.pgn.Game:
    """Load a game from its immutable raw PGN file."""
    path = resolve_pgn_path(stored_path)
    if not path.exists():
        raise MoveExtractionError(f"raw PGN missing on disk: {path}")
    with path.open("r", encoding="utf-8") as handle:
        game = chess.pgn.read_game(handle)
    if game is None:
        raise PgnParseError(f"PGN could not be read: {path}")
    return game


def extract_raw_moves(
    game: chess.pgn.Game, time_control: TimeControl | None = None
) -> list[RawMove]:
    """Replay a game into per-ply raw facts, including derived clock columns."""
    moves = list(game.mainline_moves())
    if not moves:
        raise MoveExtractionError("game has no moves")

    if time_control is None:
        time_control = parse_time_control(game.headers.get("TimeControl"))

    clocks = derive_clock_columns(clock_readings(game), time_control)

    board = game.board()
    out: list[RawMove] = []
    for index, move in enumerate(moves):
        ply = index + 1
        move_number, color = move_number_and_color(ply)
        fen_before = board.fen()
        san = board.san(move)
        board.push(move)
        before, after, spent = clocks[index] if index < len(clocks) else (None, None, None)
        out.append(
            RawMove(
                ply=ply,
                move_number=move_number,
                color=color,
                san=san,
                uci=move.uci(),
                fen_before=fen_before,
                fen_after=board.fen(),
                clock_before_ms=before,
                clock_after_ms=after,
                time_spent_ms=spent,
            )
        )
    return out


def store_game_moves(
    conn: sqlite3.Connection, game_id: int, raws: list[RawMove], time_control: TimeControl
) -> None:
    """Replace a game's raw move rows. Caller owns the transaction.

    Deleting first keeps `UNIQUE(game_id, ply)` honest across re-extraction and
    guarantees no duplicate rows: a game always has exactly one raw move set.
    Engine observations in `move_analysis` are keyed by (game_id, ply) too, so
    they continue to line up.
    """
    conn.execute("DELETE FROM game_moves WHERE game_id = ?", (game_id,))
    conn.executemany(
        """
        INSERT INTO game_moves (
            game_id, ply, move_number, color, san, uci, fen_before, fen_after,
            clock_before_ms, clock_after_ms, time_spent_ms
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                game_id, r.ply, r.move_number, r.color, r.san, r.uci,
                r.fen_before, r.fen_after,
                r.clock_before_ms, r.clock_after_ms, r.time_spent_ms,
            )
            for r in raws
        ],
    )
    conn.execute(
        """UPDATE games SET base_time_ms = ?, increment_ms = ?, moves_extracted_at = ?
            WHERE id = ?""",
        (time_control.base_ms, time_control.increment_ms, _now(), game_id),
    )


def extract_game(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    """Extract and store one game's raw moves. Returns the move count."""
    game = read_game_pgn(row["pgn_path"])
    time_control = parse_time_control(
        row["time_control"] or game.headers.get("TimeControl")
    )
    raws = extract_raw_moves(game, time_control)
    with conn:
        store_game_moves(conn, row["id"], raws, time_control)
    return len(raws)


def extract_moves(
    config: Config,
    force: bool = False,
    game_id: int | None = None,
    log: Callable[[str], None] = print,
) -> ExtractionReport:
    """Populate `game_moves` for every game that does not have it yet.

    Never calls the platform API: the raw PGNs on disk are the source.
    """
    conn = open_db(config)
    report = ExtractionReport()

    if game_id is not None:
        sql = "SELECT * FROM games WHERE id = ?"
        params: tuple = (game_id,)
    elif force:
        sql, params = "SELECT * FROM games ORDER BY id", ()
    else:
        sql = """SELECT * FROM games g
                  WHERE NOT EXISTS (SELECT 1 FROM game_moves m WHERE m.game_id = g.id)
                  ORDER BY g.id"""
        params = ()

    for row in conn.execute(sql, params).fetchall():
        try:
            count = extract_game(conn, row)
            report.games_processed += 1
            report.moves_written += count
        except Exception as exc:  # noqa: BLE001 - one bad game must not stop the run
            report.games_failed += 1
            report.errors.append(f"game {row['id']} ({row['external_game_id']}): {exc}")
            log(f"  game {row['id']}: FAILED - {exc}")

    report.moves_with_clock = conn.execute(
        "SELECT COUNT(*) FROM game_moves WHERE clock_before_ms IS NOT NULL"
    ).fetchone()[0]
    conn.close()
    return report
