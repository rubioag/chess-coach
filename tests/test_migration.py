"""Schema v2 -> v3 migration: historical measurements must survive it."""

from __future__ import annotations

import sqlite3

from chess_coach.db import SCHEMA_VERSION, connect, init_db, migrate
from chess_coach.evaluation import RULES_VERSION

# The v2 schema, reproduced verbatim: raw facts and engine verdict fused into
# one table, one row per (game, ply), and no record of what produced them.
V2_SCHEMA = """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_game_id TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    game_url TEXT,
    pgn_path TEXT NOT NULL,
    archive_year INTEGER NOT NULL,
    archive_month INTEGER NOT NULL,
    white_username TEXT, black_username TEXT,
    white_rating INTEGER, black_rating INTEGER,
    player_color TEXT, result TEXT, player_result TEXT, termination TEXT,
    time_control TEXT, time_class TEXT, rated INTEGER,
    eco TEXT, eco_url TEXT, opening TEXT, variation TEXT,
    utc_date TEXT, utc_time TEXT, end_time_utc TEXT,
    ply_count INTEGER, final_fen TEXT,
    imported_at TEXT NOT NULL,
    analysis_status TEXT NOT NULL DEFAULT 'pending',
    analysis_depth INTEGER, analysis_error TEXT, analyzed_at TEXT
);
CREATE TABLE sync_state (
    archive_url TEXT PRIMARY KEY, year INTEGER NOT NULL, month INTEGER NOT NULL,
    etag TEXT, status TEXT NOT NULL DEFAULT 'pending',
    games_seen INTEGER NOT NULL DEFAULT 0,
    last_fetched_at TEXT, last_status_code INTEGER
);
CREATE TABLE moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    ply INTEGER NOT NULL, move_number INTEGER NOT NULL, color TEXT NOT NULL,
    san TEXT NOT NULL, uci TEXT NOT NULL,
    fen_before TEXT NOT NULL, fen_after TEXT NOT NULL,
    evaluation_before INTEGER NOT NULL, evaluation_after INTEGER NOT NULL,
    evaluation_loss INTEGER NOT NULL,
    mate_in_before INTEGER, mate_in_after INTEGER,
    best_move TEXT, best_move_san TEXT,
    classification TEXT NOT NULL, phase TEXT NOT NULL,
    analysis_depth INTEGER NOT NULL,
    UNIQUE(game_id, ply)
);
INSERT INTO schema_meta VALUES ('schema_version', '2');
"""

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"


def build_v2(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(V2_SCHEMA)
    conn.execute(
        """INSERT INTO games (external_game_id, platform, pgn_path, archive_year,
                              archive_month, imported_at, analysis_status,
                              analysis_depth, player_color, ply_count)
           VALUES ('https://x/1', 'chess.com', 'data/games/2024/03/1.pgn', 2024, 3,
                   '2024-03-01T00:00:00+00:00', 'completed', 14, 'white', 2)"""
    )
    conn.execute(
        """INSERT INTO games (external_game_id, platform, pgn_path, archive_year,
                              archive_month, imported_at, analysis_status)
           VALUES ('https://x/2', 'chess.com', 'data/games/2024/03/2.pgn', 2024, 3,
                   '2024-03-01T00:00:00+00:00', 'pending')"""
    )
    conn.executemany(
        """INSERT INTO moves (game_id, ply, move_number, color, san, uci,
                              fen_before, fen_after, evaluation_before,
                              evaluation_after, evaluation_loss, best_move,
                              best_move_san, classification, phase, analysis_depth)
           VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,14)""",
        [
            (1, 1, "white", "e4", "e2e4", START_FEN, AFTER_E4, 48, 45, 3,
             "e2e4", "e4", "BEST", "OPENING"),
            (2, 1, "black", "e5", "e7e5", AFTER_E4, START_FEN, -45, -66, 21,
             "e7e6", "e6", "GOOD", "OPENING"),
        ],
    )
    conn.commit()
    conn.close()


def test_migration_preserves_every_historical_measurement(tmp_path) -> None:
    db = tmp_path / "v2.db"
    build_v2(db)

    conn = connect(db)
    assert migrate(conn) == SCHEMA_VERSION
    assert conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()[0] == "3"

    # Raw facts moved to the raw layer, unchanged.
    raw = conn.execute("SELECT * FROM game_moves ORDER BY ply").fetchall()
    assert [r["san"] for r in raw] == ["e4", "e5"]
    assert raw[0]["fen_before"] == START_FEN
    assert raw[0]["clock_before_ms"] is None      # v2 stored no clocks

    # Engine verdicts moved to the observation layer, unchanged.
    obs = conn.execute("SELECT * FROM move_analysis ORDER BY ply").fetchall()
    assert [o["evaluation_loss"] for o in obs] == [3, 21]
    assert [o["classification"] for o in obs] == ["BEST", "GOOD"]
    assert [o["best_move_san"] for o in obs] == ["e4", "e6"]
    assert all(o["pv"] is None for o in obs)     # v2 stored no PV
    conn.close()


def test_migration_attributes_old_rows_to_one_honest_run(tmp_path) -> None:
    db = tmp_path / "v2.db"
    build_v2(db)
    conn = connect(db)
    migrate(conn)

    runs = conn.execute("SELECT * FROM analysis_runs").fetchall()
    assert len(runs) == 1
    run = runs[0]
    assert run["engine_name"] == "Stockfish"
    assert run["depth"] == 14
    assert run["rules_version"] == RULES_VERSION
    assert run["moves_analyzed"] == 2
    assert run["games_analyzed"] == 1
    # The reconstruction is labelled as such rather than passed off as recorded.
    assert "RECONSTRUCTED" in run["notes"]

    attributed = {o["run_id"] for o in conn.execute("SELECT run_id FROM move_analysis")}
    assert attributed == {run["id"]}
    conn.close()


def test_migration_marks_only_analyzed_games_canonical(tmp_path) -> None:
    db = tmp_path / "v2.db"
    build_v2(db)
    conn = connect(db)
    migrate(conn)

    rows = conn.execute(
        "SELECT external_game_id, analysis_run_id FROM games ORDER BY id"
    ).fetchall()
    assert rows[0]["analysis_run_id"] is not None    # was 'completed'
    assert rows[1]["analysis_run_id"] is None        # was 'pending'
    assert conn.execute("SELECT COUNT(*) FROM current_move_analysis").fetchone()[0] == 2
    conn.close()


def test_migration_drops_the_old_fused_table(tmp_path) -> None:
    db = tmp_path / "v2.db"
    build_v2(db)
    conn = connect(db)
    migrate(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'moves'"
    ).fetchone()[0] == 0
    conn.close()


def test_migration_is_idempotent(tmp_path) -> None:
    db = tmp_path / "v2.db"
    build_v2(db)
    conn = connect(db)
    migrate(conn)
    migrate(conn)
    migrate(conn)
    assert conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM game_moves").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM move_analysis").fetchone()[0] == 2
    conn.close()


def test_fresh_database_is_created_at_the_current_version(tmp_path) -> None:
    conn = init_db(tmp_path / "fresh.db")
    assert conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()[0] == str(SCHEMA_VERSION)
    names = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
        if not r[0].startswith("sqlite_")
    }
    assert {"games", "game_moves", "analysis_runs", "move_analysis",
            "sync_state", "schema_meta", "current_move_analysis"} <= names
    assert "moves" not in names
    conn.close()
