"""Status reporting over the ingested / analyzed dataset."""

from __future__ import annotations

from pathlib import Path

from .config import Config
from .db import open_db


def status_report(config: Config) -> str:
    conn = open_db(config)
    lines: list[str] = []

    version = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    lines.append(f"schema version : {version[0] if version else '?'}")

    total = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    lines.append(f"games imported : {total}")

    by_status = dict(
        conn.execute(
            "SELECT analysis_status, COUNT(*) FROM games GROUP BY analysis_status"
        ).fetchall()
    )
    for key in ("completed", "pending", "running", "failed"):
        lines.append(f"  {key:<13}: {by_status.get(key, 0)}")

    raw_moves = conn.execute("SELECT COUNT(*) FROM game_moves").fetchone()[0]
    with_clock = conn.execute(
        "SELECT COUNT(*) FROM game_moves WHERE clock_before_ms IS NOT NULL"
    ).fetchone()[0]
    coverage = 100.0 * with_clock / raw_moves if raw_moves else 0.0
    lines.append(f"raw move rows  : {raw_moves}")
    lines.append(f"  with clock   : {with_clock} ({coverage:.1f}%)")

    observations = conn.execute("SELECT COUNT(*) FROM move_analysis").fetchone()[0]
    canonical = conn.execute("SELECT COUNT(*) FROM current_move_analysis").fetchone()[0]
    with_pv = conn.execute(
        "SELECT COUNT(*) FROM move_analysis WHERE pv IS NOT NULL"
    ).fetchone()[0]
    pv_pct = 100.0 * with_pv / observations if observations else 0.0
    lines.append(f"observations   : {observations} across all runs")
    lines.append(f"  canonical    : {canonical}")
    lines.append(f"  with PV      : {with_pv} ({pv_pct:.1f}%)")

    lines.append("analysis runs  :")
    runs = conn.execute(
        """SELECT id, engine_name, engine_version, depth, multipv, status,
                  moves_analyzed
             FROM analysis_runs ORDER BY id"""
    ).fetchall()
    if not runs:
        lines.append("  (none)")
    for r in runs:
        used = conn.execute(
            "SELECT COUNT(*) FROM games WHERE analysis_run_id = ?", (r["id"],)
        ).fetchone()[0]
        lines.append(
            f"  run {r['id']}: {r['engine_name']} {r['engine_version']} "
            f"depth {r['depth']} multipv {r['multipv']} "
            f"[{r['status']}] {r['moves_analyzed']} moves, canonical for {used} game(s)"
        )

    opening_missing = conn.execute(
        "SELECT COUNT(*) FROM games WHERE opening IS NULL"
    ).fetchone()[0]
    opening_unknown = conn.execute(
        "SELECT COUNT(*) FROM games WHERE opening = 'Unknown'"
    ).fetchone()[0]
    lines.append(
        f"opening headers: {opening_missing} missing (NULL), "
        f"{opening_unknown} present-but-unclassified"
    )

    archives = conn.execute(
        "SELECT status, COUNT(*) FROM sync_state GROUP BY status"
    ).fetchall()
    lines.append("archives       : " + (
        ", ".join(f"{s}={c}" for s, c in archives) or "none synced yet"
    ))

    db_file = Path(config.database)
    if db_file.exists():
        lines.append(f"database size  : {db_file.stat().st_size / 1024:.1f} KiB")

    raw_dir = Path(config.raw_games_dir)
    if raw_dir.exists():
        pgns = list(raw_dir.rglob("*.pgn"))
        raw_bytes = sum(p.stat().st_size for p in pgns)
        lines.append(f"raw PGN files  : {len(pgns)} ({raw_bytes / 1024:.1f} KiB)")

    conn.close()
    return "\n".join(lines)
