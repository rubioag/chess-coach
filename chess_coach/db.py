"""SQLite schema, migrations and connection helpers.

SQLite holds parsed/structured data only. The raw PGN directory under
data/games is the immutable source of truth and is never rewritten here.

SCHEMA LAYERS
-------------
The tables mirror the analytical integrity ladder documented in ARCHITECTURE.md:

    games          -- one row per imported game (platform facts + PGN headers)
    game_moves     -- RAW: the moves actually played. SAN/UCI/FEN/clock.
                      Objective, engine-independent, one row per (game, ply).
    analysis_runs  -- PROVENANCE: which engine, depth and rules produced a set
                      of observations, and when.
    move_analysis  -- ENGINE OBSERVATION: evaluations, best move, PV,
                      classification and phase, one row per (run, game, ply).

Raw facts are stored once and never duplicated per analysis run. Engine
observations are always attributed to a run, so re-analysing a game at a
different depth or with a different engine version adds rows instead of
destroying the previous ones. `games.analysis_run_id` names the run currently
treated as canonical for that game; the `current_move_analysis` view joins the
raw and observation layers for exactly that run, so aggregation never
double-counts a game that has been analysed more than once.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per imported game. external_game_id is the chess.com game `url`
-- field verbatim: it stays a stable unique key across their internal id
-- format changes (integer -> uuid), which is why we never parse it.
CREATE TABLE IF NOT EXISTS games (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    external_game_id   TEXT NOT NULL UNIQUE,
    platform           TEXT NOT NULL,
    game_url           TEXT,
    pgn_path           TEXT NOT NULL,
    archive_year       INTEGER NOT NULL,
    archive_month      INTEGER NOT NULL,

    white_username     TEXT,
    black_username     TEXT,
    white_rating       INTEGER,
    black_rating       INTEGER,
    player_color       TEXT,        -- 'white' | 'black' | NULL if user not in game
    result             TEXT,        -- PGN Result header: 1-0 / 0-1 / 1/2-1/2 / *
    player_result      TEXT,        -- 'win' | 'loss' | 'draw' | NULL
    termination        TEXT,

    time_control       TEXT,
    time_class         TEXT,
    rated              INTEGER,     -- 0/1/NULL

    -- ECO / opening headers. NULL means the header was absent entirely;
    -- 'Unknown' means the header was present but carried no classification.
    -- These are the PLATFORM's opinion and are kept only as a cross-check:
    -- opening families are derived from game_moves, not from these strings.
    eco                TEXT,
    eco_url            TEXT,
    opening            TEXT,
    variation          TEXT,

    utc_date           TEXT,
    utc_time           TEXT,
    end_time_utc       TEXT,
    ply_count          INTEGER,
    final_fen          TEXT,

    -- Base time and increment parsed from the TimeControl header, in
    -- milliseconds. Needed to turn PGN clock readings into time spent.
    base_time_ms       INTEGER,
    increment_ms       INTEGER,

    imported_at        TEXT NOT NULL,
    moves_extracted_at TEXT,        -- when game_moves was last (re)built

    analysis_status    TEXT NOT NULL DEFAULT 'pending',
    analysis_depth     INTEGER,
    analysis_error     TEXT,
    analyzed_at        TEXT,
    -- The run currently treated as canonical for this game.
    analysis_run_id    INTEGER REFERENCES analysis_runs(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_games_analysis_status ON games(analysis_status);
CREATE INDEX IF NOT EXISTS idx_games_archive ON games(archive_year, archive_month);
CREATE INDEX IF NOT EXISTS idx_games_end_time ON games(end_time_utc);

-- One row per monthly archive. Incremental sync uses the archive list plus
-- ETags; the chess.com endpoint has no `since` filter.
CREATE TABLE IF NOT EXISTS sync_state (
    archive_url      TEXT PRIMARY KEY,
    year             INTEGER NOT NULL,
    month            INTEGER NOT NULL,
    etag             TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'complete'
    games_seen       INTEGER NOT NULL DEFAULT 0,
    last_fetched_at  TEXT,
    last_status_code INTEGER
);

-- RAW LAYER. The moves actually played, plus the clock readings that were
-- already present in the PGN. Nothing here depends on an engine, so these rows
-- survive every re-analysis untouched and are rebuilt only by re-reading the
-- immutable raw PGN from disk.
CREATE TABLE IF NOT EXISTS game_moves (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id         INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,

    ply             INTEGER NOT NULL,   -- 1-based; ply 1 is White's first move
    move_number     INTEGER NOT NULL,
    color           TEXT NOT NULL,      -- 'white' | 'black' (the mover)
    san             TEXT NOT NULL,
    uci             TEXT NOT NULL,
    fen_before      TEXT NOT NULL,
    fen_after       TEXT NOT NULL,

    -- Clock readings in milliseconds.
    --   clock_after_ms  : verbatim [%clk] for this move, i.e. what the mover
    --                     had left once the move was made (increment included).
    --   clock_before_ms : what the mover had when they started thinking. It is
    --                     the same player's previous clock_after_ms, or the
    --                     base time on their first move. This is the field that
    --                     answers "how much time was left when this was played".
    --   time_spent_ms   : clock_before_ms - clock_after_ms + increment_ms.
    clock_before_ms INTEGER,
    clock_after_ms  INTEGER,
    time_spent_ms   INTEGER,

    UNIQUE(game_id, ply)
);

CREATE INDEX IF NOT EXISTS idx_game_moves_game ON game_moves(game_id);
CREATE INDEX IF NOT EXISTS idx_game_moves_color ON game_moves(color);
CREATE INDEX IF NOT EXISTS idx_game_moves_clock ON game_moves(clock_before_ms);

-- PROVENANCE. One row per analysis pass. Everything that could change a
-- measurement lives here, so two rows taken months apart can be compared only
-- when their runs agree - or deliberately compared when they do not.
--
-- engine_name / engine_version are free text: a new Stockfish release, or a
-- different engine entirely, needs no schema change.
CREATE TABLE IF NOT EXISTS analysis_runs (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,

    engine_name                TEXT NOT NULL,
    engine_version             TEXT NOT NULL,
    engine_id_string           TEXT,     -- full UCI `id name` as reported

    depth                      INTEGER NOT NULL,
    multipv                    INTEGER NOT NULL,
    threads                    INTEGER NOT NULL,
    hash_mb                    INTEGER NOT NULL,

    eval_cap_cp                INTEGER NOT NULL,
    rules_version              TEXT NOT NULL,   -- version of the pure rule set
    inaccuracy_cp              INTEGER NOT NULL,
    mistake_cp                 INTEGER NOT NULL,
    blunder_cp                 INTEGER NOT NULL,
    phase_opening_max_ply      INTEGER NOT NULL,
    phase_opening_min_material INTEGER NOT NULL,
    phase_endgame_max_material INTEGER NOT NULL,

    started_at                 TEXT NOT NULL,
    finished_at                TEXT,
    status                     TEXT NOT NULL,   -- running|completed|failed|aborted
    games_analyzed             INTEGER NOT NULL DEFAULT 0,
    games_failed               INTEGER NOT NULL DEFAULT 0,
    moves_analyzed             INTEGER NOT NULL DEFAULT 0,
    notes                      TEXT
);

CREATE INDEX IF NOT EXISTS idx_analysis_runs_status ON analysis_runs(status);

-- ENGINE OBSERVATION LAYER. Every row is attributed to the run that produced
-- it. Re-analysis inserts a new run's rows; it never overwrites another run's.
-- All evaluation columns are centipawns normalized to the perspective of the
-- player who made the move.
CREATE TABLE IF NOT EXISTS move_analysis (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    game_id           INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    ply               INTEGER NOT NULL,

    evaluation_before INTEGER NOT NULL,   -- raw, uncapped, mover perspective
    evaluation_after  INTEGER NOT NULL,   -- raw, uncapped, mover perspective
    evaluation_loss   INTEGER NOT NULL,   -- clamped and floored at 0

    -- Signed mate distance when the evaluation is a forced mate, else NULL.
    -- mate == 0 is disambiguated by the sign of the matching evaluation column.
    mate_in_before    INTEGER,
    mate_in_after     INTEGER,

    best_move         TEXT,               -- engine's preferred move, UCI
    best_move_san     TEXT,
    -- Principal variation from the position before the move, space-separated
    -- UCI. Its first element is always best_move.
    pv                TEXT,
    pv_san            TEXT,
    pv_length         INTEGER,

    classification    TEXT NOT NULL,      -- BEST/GOOD/INACCURACY/MISTAKE/BLUNDER
    phase             TEXT NOT NULL,      -- OPENING/MIDDLEGAME/ENDGAME
    analysis_depth    INTEGER NOT NULL,

    UNIQUE(run_id, game_id, ply)
);

CREATE INDEX IF NOT EXISTS idx_move_analysis_game ON move_analysis(game_id, ply);
CREATE INDEX IF NOT EXISTS idx_move_analysis_run ON move_analysis(run_id);
CREATE INDEX IF NOT EXISTS idx_move_analysis_class ON move_analysis(classification);
CREATE INDEX IF NOT EXISTS idx_move_analysis_phase ON move_analysis(phase);

"""

# Objects that reference columns added by `_ensure_columns` are created only
# after it has run: on an older database those columns do not exist yet.
POST_MIGRATION = """
CREATE INDEX IF NOT EXISTS idx_games_run ON games(analysis_run_id);

-- Convenience join of the raw layer with the canonical run's observations.
-- Aggregation reads this, so a game analysed three times still contributes
-- exactly one set of rows.
CREATE VIEW IF NOT EXISTS current_move_analysis AS
SELECT
    gm.game_id, gm.ply, gm.move_number, gm.color, gm.san, gm.uci,
    gm.fen_before, gm.fen_after,
    gm.clock_before_ms, gm.clock_after_ms, gm.time_spent_ms,
    ma.run_id, ma.evaluation_before, ma.evaluation_after, ma.evaluation_loss,
    ma.mate_in_before, ma.mate_in_after,
    ma.best_move, ma.best_move_san, ma.pv, ma.pv_san, ma.pv_length,
    ma.classification, ma.phase, ma.analysis_depth
FROM game_moves gm
JOIN games g          ON g.id = gm.game_id
JOIN move_analysis ma ON ma.game_id = gm.game_id
                     AND ma.ply     = gm.ply
                     AND ma.run_id  = g.analysis_run_id;
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (name,),
    ).fetchone() is not None


def _stored_version(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "schema_meta"):
        return 0
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    return int(row[0]) if row else 0


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Split the old `moves` table into raw facts + attributed observations.

    Schema v2 stored one row per (game, ply) with the raw move facts and the
    engine's verdict fused together and no record of which engine or rule set
    produced the verdict. Those rows are real measurements and are preserved:
    they are attributed to a single reconstructed run whose `notes` say plainly
    that its parameters were inferred from the code that produced them rather
    than recorded at the time.
    """
    from .evaluation import RULES_VERSION

    depths = [
        r[0] for r in conn.execute("SELECT DISTINCT analysis_depth FROM moves")
    ]
    legacy_depth = depths[0] if len(depths) == 1 else -1

    conn.execute(
        """
        INSERT INTO analysis_runs (
            engine_name, engine_version, engine_id_string,
            depth, multipv, threads, hash_mb,
            eval_cap_cp, rules_version,
            inaccuracy_cp, mistake_cp, blunder_cp,
            phase_opening_max_ply, phase_opening_min_material,
            phase_endgame_max_material,
            started_at, finished_at, status, notes
        ) VALUES (
            'Stockfish', '18', 'Stockfish 18',
            ?, 1, 1, 128,
            1000, ?,
            50, 100, 300,
            20, 58, 20,
            ?, ?, 'completed', ?
        )
        """,
        (
            legacy_depth,
            RULES_VERSION,
            "1970-01-01T00:00:00+00:00",
            "1970-01-01T00:00:00+00:00",
            "RECONSTRUCTED: migrated from schema v2, which stored no provenance. "
            "Engine and parameters were inferred from the code and config that "
            "produced these rows, not recorded at analysis time.",
        ),
    )
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        """
        INSERT INTO game_moves (
            game_id, ply, move_number, color, san, uci, fen_before, fen_after
        )
        SELECT game_id, ply, move_number, color, san, uci, fen_before, fen_after
          FROM moves
        """
    )
    conn.execute(
        """
        INSERT INTO move_analysis (
            run_id, game_id, ply,
            evaluation_before, evaluation_after, evaluation_loss,
            mate_in_before, mate_in_after,
            best_move, best_move_san, classification, phase, analysis_depth
        )
        SELECT ?, game_id, ply,
               evaluation_before, evaluation_after, evaluation_loss,
               mate_in_before, mate_in_after,
               best_move, best_move_san, classification, phase, analysis_depth
          FROM moves
        """,
        (run_id,),
    )
    conn.execute(
        "UPDATE games SET analysis_run_id = ? WHERE analysis_status = 'completed'",
        (run_id,),
    )
    conn.execute(
        """UPDATE analysis_runs
              SET games_analyzed = (SELECT COUNT(*) FROM games WHERE analysis_run_id = ?),
                  moves_analyzed = (SELECT COUNT(*) FROM move_analysis WHERE run_id = ?)
            WHERE id = ?""",
        (run_id, run_id, run_id),
    )
    conn.execute("DROP TABLE moves")


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a table was first created."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(games)")}
    for column, ddl in (
        ("base_time_ms", "INTEGER"),
        ("increment_ms", "INTEGER"),
        ("moves_extracted_at", "TEXT"),
        ("analysis_run_id", "INTEGER REFERENCES analysis_runs(id) ON DELETE SET NULL"),
    ):
        if column not in existing:
            conn.execute(f"ALTER TABLE games ADD COLUMN {column} {ddl}")


def migrate(conn: sqlite3.Connection) -> int:
    """Bring an existing database up to SCHEMA_VERSION. Returns the version."""
    version = _stored_version(conn)
    needs_v3 = version < 3 and _table_exists(conn, "moves")

    # Creating the v3 tables is safe on a fresh database and on a v2 one; the
    # old `moves` table is only touched by the migration step below.
    conn.executescript(SCHEMA)
    _ensure_columns(conn)

    if needs_v3:
        with conn:
            _migrate_v2_to_v3(conn)

    conn.executescript(POST_MIGRATION)

    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return SCHEMA_VERSION


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = connect(db_path)
    migrate(conn)
    return conn


class ProfileMismatch(RuntimeError):
    """A database is being opened for a player it does not belong to."""


def bind_profile(conn: sqlite3.Connection, profile: str, username: str) -> None:
    """Tie a database file to one player, and refuse to open it for another.

    Profiles are isolated physically: each one has its own database and its own
    raw PGN directory. That makes mixing two players impossible by
    construction - unless a configuration mistake points two profiles at the
    same file. This is the guard against exactly that.

    The first open stamps the owning username into `schema_meta`; every later
    open verifies it. Usernames are compared case-insensitively because
    chess.com treats them that way. No schema change is involved: `schema_meta`
    already exists.
    """
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'profile_username'"
    ).fetchone()

    if row is None:
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) "
            "VALUES ('profile_username', ?)",
            (username,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) "
            "VALUES ('profile_name', ?)",
            (profile,),
        )
        conn.commit()
        return

    owner = str(row[0])
    if owner.lower() != username.lower():
        raise ProfileMismatch(
            f"database belongs to '{owner}' but profile '{profile}' is "
            f"configured for '{username}'. Refusing to open it: this would mix "
            "two players' games. Check the 'paths' in the profile file."
        )

    # The owning username matches; a renamed profile label is allowed.
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('profile_name', ?)",
        (profile,),
    )
    conn.commit()


def open_db(config) -> sqlite3.Connection:
    """Open (and migrate) the database belonging to `config`'s profile."""
    conn = init_db(config.database)
    try:
        bind_profile(conn, config.profile, config.username)
    except Exception:
        conn.close()
        raise
    return conn
