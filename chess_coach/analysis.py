"""Stockfish analysis pass: raw move rows -> attributed engine observations.

Ingestion is not touched here, and neither is the raw layer. This module reads
`game_moves` (built from the immutable PGNs by `moves.py`) and writes
`move_analysis` rows, every one of them tagged with the `analysis_runs` row that
produced it.

PROVENANCE
----------
Each invocation opens exactly one run recording the engine name and version, the
search parameters, the evaluation cap, the rule-set version and every
classification and phase threshold in force. A move is therefore never ambiguous
about the configuration that produced it, and two runs - different depths,
different engine versions, retuned thresholds - coexist without either
destroying the other. `games.analysis_run_id` names the run treated as canonical
for that game, which is what the `current_move_analysis` view reads.

SEARCH ECONOMY
--------------
A game of N moves has N+1 positions. Each position is searched exactly once and
its result serves two roles: it is `evaluation_before` for the move played from
it, and (after a perspective flip) `evaluation_after` for the move that led into
it. Searching each position twice would double the cost and, because a fixed
depth search is not perfectly stable, could also produce two different numbers
for the same position.

RESUMABILITY
------------
Every game carries `analysis_status`: pending -> running -> completed | failed.
A game is marked `running` before its first search and only becomes `completed`
after all of its moves are written in a single transaction. A crash therefore
leaves a game in `running`, which the next run resets to `pending`. A failure is
recorded as `failed` with its error message and is never silently treated as
done; `--retry-failed` picks those back up.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Sequence

import chess

from .config import Config
from .db import open_db
from .engine import EngineError, StockfishEngine
from .evaluation import (
    RULES_VERSION,
    Evaluation,
    classify,
    detect_phase,
    evaluation_loss,
    flip,
)
from .moves import extract_game


class AnalysisError(RuntimeError):
    pass


@dataclass
class MoveAnalysisRecord:
    ply: int
    evaluation_before: int
    evaluation_after: int
    evaluation_loss: int
    mate_in_before: int | None
    mate_in_after: int | None
    best_move: str | None
    best_move_san: str | None
    pv: str | None
    pv_san: str | None
    pv_length: int
    classification: str
    phase: str
    analysis_depth: int


@dataclass
class AnalysisReport:
    run_id: int | None = None
    games_selected: int = 0
    games_analyzed: int = 0
    games_failed: int = 0
    moves_analyzed: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def avg_seconds_per_game(self) -> float:
        return self.seconds / self.games_analyzed if self.games_analyzed else 0.0

    def summary(self) -> str:
        return (
            f"run {self.run_id}\n"
            f"games: {self.games_analyzed} analyzed, {self.games_failed} failed "
            f"of {self.games_selected} selected\n"
            f"moves analyzed: {self.moves_analyzed}\n"
            f"runtime: {self.seconds:.1f}s "
            f"({self.avg_seconds_per_game:.1f}s per analyzed game)"
        )


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------

def open_run(
    conn: sqlite3.Connection,
    config: Config,
    engine_name: str,
    engine_version: str,
    engine_id_string: str,
    depth: int,
    notes: str | None = None,
) -> int:
    """Record a new analysis run and return its id."""
    thresholds = config.analysis.thresholds
    phase = config.analysis.phase_rules
    cursor = conn.execute(
        """
        INSERT INTO analysis_runs (
            engine_name, engine_version, engine_id_string,
            depth, multipv, threads, hash_mb,
            eval_cap_cp, rules_version,
            inaccuracy_cp, mistake_cp, blunder_cp,
            phase_opening_max_ply, phase_opening_min_material,
            phase_endgame_max_material,
            started_at, status, notes
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'running',?)
        """,
        (
            engine_name, engine_version, engine_id_string,
            depth, config.stockfish.multipv, config.stockfish.threads,
            config.stockfish.hash_mb,
            config.analysis.eval_cap_cp, RULES_VERSION,
            thresholds.inaccuracy_cp, thresholds.mistake_cp, thresholds.blunder_cp,
            phase.opening_max_ply, phase.opening_min_material,
            phase.endgame_max_material,
            _now(), notes,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def close_run(
    conn: sqlite3.Connection, run_id: int, report: AnalysisReport, status: str
) -> None:
    conn.execute(
        """UPDATE analysis_runs
              SET finished_at = ?, status = ?, games_analyzed = ?,
                  games_failed = ?, moves_analyzed = ?
            WHERE id = ?""",
        (
            _now(), status, report.games_analyzed, report.games_failed,
            report.moves_analyzed, run_id,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Per-game analysis
# ---------------------------------------------------------------------------

def analyze_raw_moves(
    raw_rows: Sequence[sqlite3.Row],
    engine: StockfishEngine,
    config: Config,
    depth: int,
) -> list[MoveAnalysisRecord]:
    """Produce one observation record per raw move row.

    Boards are rebuilt from the stored `fen_before` plus the stored UCI, so the
    observation layer can never drift out of alignment with the raw layer: both
    are keyed by the same (game_id, ply) and derived from the same PGN.
    """
    if not raw_rows:
        raise AnalysisError("game has no raw moves; run extract-moves first")

    board = chess.Board(raw_rows[0]["fen_before"])
    current = engine.analyse_position(board, depth)
    records: list[MoveAnalysisRecord] = []

    for row in raw_rows:
        if board.fen() != row["fen_before"]:
            raise AnalysisError(
                f"raw move rows are inconsistent at ply {row['ply']}: "
                f"expected {row['fen_before']}, replayed {board.fen()}"
            )
        move = chess.Move.from_uci(row["uci"])
        if move not in board.legal_moves:
            raise AnalysisError(f"illegal stored move at ply {row['ply']}: {row['uci']}")

        best_move = current.best_move
        best_move_san = board.san(best_move) if best_move else None
        pv_moves = current.pv
        pv_uci = " ".join(m.uci() for m in pv_moves) or None
        pv_san = board.variation_san(pv_moves) if pv_moves else None
        phase = detect_phase(board, row["ply"], config.analysis.phase_rules)
        before: Evaluation = current.evaluation

        board.push(move)

        # One search per position: this becomes the next move's "before".
        current = engine.analyse_position(board, depth)
        # ... and, flipped, this move's "after" in the mover's own perspective.
        after = flip(current.evaluation)

        loss = evaluation_loss(before, after, config.analysis.eval_cap_cp)
        records.append(
            MoveAnalysisRecord(
                ply=row["ply"],
                evaluation_before=before.cp,
                evaluation_after=after.cp,
                evaluation_loss=loss,
                mate_in_before=before.mate,
                mate_in_after=after.mate,
                best_move=best_move.uci() if best_move else None,
                best_move_san=best_move_san,
                pv=pv_uci,
                pv_san=pv_san,
                pv_length=len(pv_moves),
                classification=classify(
                    loss,
                    row["uci"],
                    best_move.uci() if best_move else None,
                    config.analysis.thresholds,
                ),
                phase=phase,
                analysis_depth=depth,
            )
        )

    return records


def _store_analysis(
    conn: sqlite3.Connection,
    run_id: int,
    game_id: int,
    records: Iterable[MoveAnalysisRecord],
    depth: int,
) -> None:
    """Write one run's observations for a game and make that run canonical.

    Rows from OTHER runs are left alone: re-analysis adds, it does not destroy.
    Only this run's own rows for this game are cleared, so a retry cannot leave
    half a run behind.
    """
    conn.execute(
        "DELETE FROM move_analysis WHERE run_id = ? AND game_id = ?", (run_id, game_id)
    )
    conn.executemany(
        """
        INSERT INTO move_analysis (
            run_id, game_id, ply,
            evaluation_before, evaluation_after, evaluation_loss,
            mate_in_before, mate_in_after,
            best_move, best_move_san, pv, pv_san, pv_length,
            classification, phase, analysis_depth
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                run_id, game_id, r.ply,
                r.evaluation_before, r.evaluation_after, r.evaluation_loss,
                r.mate_in_before, r.mate_in_after,
                r.best_move, r.best_move_san, r.pv, r.pv_san, r.pv_length,
                r.classification, r.phase, r.analysis_depth,
            )
            for r in records
        ],
    )
    conn.execute(
        """UPDATE games
              SET analysis_status = 'completed', analysis_depth = ?,
                  analyzed_at = ?, analysis_error = NULL, analysis_run_id = ?
            WHERE id = ?""",
        (depth, _now(), run_id, game_id),
    )


def reset_stale_running(conn: sqlite3.Connection) -> int:
    """A 'running' game is the fingerprint of a crash. Make it retryable."""
    cursor = conn.execute(
        "UPDATE games SET analysis_status = 'pending' WHERE analysis_status = 'running'"
    )
    conn.commit()
    return cursor.rowcount


def select_games(
    conn: sqlite3.Connection,
    retry_failed: bool = False,
    force: bool = False,
    limit: int | None = None,
    game_id: int | None = None,
) -> list[sqlite3.Row]:
    if game_id is not None:
        rows = conn.execute(
            "SELECT id, external_game_id, pgn_path, time_control FROM games WHERE id = ?",
            (game_id,),
        ).fetchall()
        if not rows:
            raise AnalysisError(f"no game with id {game_id}")
        return rows

    statuses = ["pending"]
    if retry_failed:
        statuses.append("failed")
    if force:
        statuses = ["pending", "failed", "completed"]

    placeholders = ",".join("?" for _ in statuses)
    sql = (
        "SELECT id, external_game_id, pgn_path, time_control FROM games "
        f"WHERE analysis_status IN ({placeholders}) "
        "ORDER BY end_time_utc IS NULL, end_time_utc DESC, id"
    )
    params: list[object] = list(statuses)
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def _raw_moves_for(conn: sqlite3.Connection, game_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT ply, uci, san, fen_before, fen_after FROM game_moves "
        "WHERE game_id = ? ORDER BY ply",
        (game_id,),
    ).fetchall()


def analyze(
    config: Config,
    depth: int | None = None,
    limit: int | None = None,
    game_id: int | None = None,
    retry_failed: bool = False,
    force: bool = False,
    notes: str | None = None,
    log: Callable[[str], None] = print,
    engine_factory: Callable[[], "StockfishEngine"] | None = None,
) -> AnalysisReport:
    """Analyze pending games serially under one recorded run.

    Correctness first: no parallelism. `engine_factory` exists so the resume and
    failure paths can be tested against a stub engine, without a Stockfish
    binary and without waiting on real searches.
    """
    depth = depth or config.stockfish.historical_depth
    conn = open_db(config)
    report = AnalysisReport()

    reset = reset_stale_running(conn)
    if reset:
        log(f"reset {reset} game(s) stuck in 'running' from a previous run")

    games = select_games(
        conn, retry_failed=retry_failed, force=force, limit=limit, game_id=game_id
    )
    report.games_selected = len(games)
    if not games:
        conn.close()
        return report

    if engine_factory is None:
        def engine_factory() -> StockfishEngine:
            return StockfishEngine(
                config.stockfish.path,
                threads=config.stockfish.threads,
                hash_mb=config.stockfish.hash_mb,
                multipv=config.stockfish.multipv,
            )

    started = time.monotonic()
    run_id: int | None = None
    status = "completed"
    try:
        with engine_factory() as engine:
            run_id = open_run(
                conn, config,
                engine_name=engine.engine_name,
                engine_version=engine.engine_version,
                engine_id_string=engine.name,
                depth=depth,
                notes=notes,
            )
            report.run_id = run_id
            log(
                f"run {run_id}: {engine.name} | depth {depth} | "
                f"multipv {config.stockfish.multipv}"
            )

            for row in games:
                gid = row["id"]
                conn.execute(
                    "UPDATE games SET analysis_status = 'running' WHERE id = ?", (gid,)
                )
                conn.commit()

                game_started = time.monotonic()
                try:
                    raw = _raw_moves_for(conn, gid)
                    if not raw:
                        # The raw layer is a prerequisite, not an optional step.
                        extract_game(conn, conn.execute(
                            "SELECT * FROM games WHERE id = ?", (gid,)
                        ).fetchone())
                        raw = _raw_moves_for(conn, gid)

                    records = analyze_raw_moves(raw, engine, config, depth)
                    with conn:  # one transaction: observations + status flip
                        _store_analysis(conn, run_id, gid, records, depth)
                    report.games_analyzed += 1
                    report.moves_analyzed += len(records)
                    log(
                        f"  game {gid}: {len(records)} moves in "
                        f"{time.monotonic() - game_started:.1f}s"
                    )
                except EngineError:
                    # The engine process is no longer trustworthy; stop rather
                    # than mark every remaining game failed against a dead engine.
                    conn.execute(
                        """UPDATE games SET analysis_status = 'failed',
                                  analysis_error = 'engine error' WHERE id = ?""",
                        (gid,),
                    )
                    conn.commit()
                    report.games_failed += 1
                    status = "failed"
                    raise
                except Exception as exc:  # noqa: BLE001 - one bad game must not stop the run
                    conn.execute(
                        """UPDATE games SET analysis_status = 'failed',
                                  analysis_error = ? WHERE id = ?""",
                        (f"{type(exc).__name__}: {exc}"[:500], gid),
                    )
                    conn.commit()
                    report.games_failed += 1
                    report.errors.append(f"game {gid} ({row['external_game_id']}): {exc}")
                    log(f"  game {gid}: FAILED - {exc}")
    except EngineError:
        status = "failed"
        raise
    finally:
        report.seconds = time.monotonic() - started
        if run_id is not None:
            if status == "completed" and report.games_failed:
                status = "completed"  # per-game failures do not fail the run
            close_run(conn, run_id, report, status)
        conn.close()

    return report
