"""Ingestion: chess.com PubAPI -> immutable raw PGN files -> SQLite.

Flow per sync:
  1. GET /pub/player/{u}/games/archives  -> monthly archive URLs
  2. Skip months already marked 'complete' in sync_state
  3. Otherwise request with If-None-Match; 304 means skip parsing
  4. Dedup by external_game_id regardless of ETag state
  5. Mark a month 'complete' only once it is in the past and parsed cleanly
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .chesscom import ChessComClient, external_game_id, game_id_slug, parse_archive_url
from .config import Config, PROJECT_ROOT
from .db import open_db
from .pgn_parser import PgnParseError, end_time_iso, parse_pgn, player_color, player_result


@dataclass
class IngestReport:
    archives_total: int = 0
    archives_fetched: int = 0
    archives_skipped_complete: int = 0
    archives_not_modified: int = 0
    games_seen: int = 0
    games_imported: int = 0
    games_duplicate: int = 0
    games_failed: int = 0
    pgn_files_written: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"archives: {self.archives_fetched} fetched, "
            f"{self.archives_skipped_complete} already complete, "
            f"{self.archives_not_modified} unchanged (304) "
            f"of {self.archives_total}\n"
            f"games: {self.games_seen} seen, {self.games_imported} imported, "
            f"{self.games_duplicate} duplicates, {self.games_failed} failed\n"
            f"raw PGN files written: {self.pgn_files_written}"
        )


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _is_past_month(year: int, month: int, now: datetime | None = None) -> bool:
    now = now or datetime.now(tz=timezone.utc)
    return (year, month) < (now.year, now.month)


def _stored_path(path: Path) -> str:
    """Store the PGN location relative to the project root when possible.

    Tests (and anyone pointing raw_games_dir outside the repo) get the
    absolute path instead of a relative_to() crash.
    """
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def raw_pgn_path(config: Config, year: int, month: int, external_id: str) -> Path:
    return (
        config.raw_games_dir
        / f"{year:04d}"
        / f"{month:02d}"
        / f"{game_id_slug(external_id)}.pgn"
    )


def write_raw_pgn(path: Path, pgn_text: str) -> bool:
    """Write the PGN unmodified. Existing files are never rewritten."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pgn_text, encoding="utf-8", newline="\n")
    return True


def _known_game_ids(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT external_game_id FROM games")}


def insert_game(
    conn: sqlite3.Connection,
    config: Config,
    game_obj: dict[str, Any],
    year: int,
    month: int,
    pgn_path: Path,
) -> None:
    parsed = parse_pgn(game_obj["pgn"])
    color = player_color(parsed, config.username)
    rel_path = _stored_path(pgn_path)

    # The API object is authoritative for platform metadata that never appears
    # in the PGN headers (time_class, rated, end_time, eco url).
    rated = game_obj.get("rated")
    conn.execute(
        """
        INSERT INTO games (
            external_game_id, platform, game_url, pgn_path,
            archive_year, archive_month,
            white_username, black_username, white_rating, black_rating,
            player_color, result, player_result, termination,
            time_control, time_class, rated,
            eco, eco_url, opening, variation,
            utc_date, utc_time, end_time_utc, ply_count, final_fen,
            imported_at, analysis_status
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')
        """,
        (
            external_game_id(game_obj),
            config.platform,
            game_obj.get("url"),
            rel_path,
            year,
            month,
            parsed.white_username,
            parsed.black_username,
            parsed.white_rating,
            parsed.black_rating,
            color,
            parsed.result,
            player_result(color, parsed.result),
            parsed.termination,
            parsed.time_control,
            game_obj.get("time_class"),
            None if rated is None else int(bool(rated)),
            parsed.eco,
            parsed.eco_url or game_obj.get("eco"),
            parsed.opening,
            parsed.variation,
            parsed.utc_date,
            parsed.utc_time,
            end_time_iso(game_obj),
            parsed.ply_count,
            parsed.final_fen,
            _now(),
        ),
    )


def ingest(
    config: Config,
    client: ChessComClient | None = None,
    limit_archives: int | None = None,
    max_games: int | None = None,
    log: Callable[[str], None] = print,
) -> IngestReport:
    """Run one incremental sync. Idempotent and deduplicated."""
    client = client or ChessComClient(config)
    conn = open_db(config)
    report = IngestReport()

    archives = client.list_archives()
    report.archives_total = len(archives)
    # Newest months first: the most useful data lands early if a run is cut short.
    archives = sorted(archives, reverse=True)
    if limit_archives is not None:
        archives = archives[:limit_archives]

    known = _known_game_ids(conn)

    for archive_url in archives:
        if max_games is not None and report.games_imported >= max_games:
            break

        year, month = parse_archive_url(archive_url)
        row = conn.execute(
            "SELECT etag, status FROM sync_state WHERE archive_url = ?", (archive_url,)
        ).fetchone()
        if row and row["status"] == "complete":
            report.archives_skipped_complete += 1
            continue

        etag = row["etag"] if row else None
        conn.execute(
            """
            INSERT INTO sync_state (archive_url, year, month, etag, status)
            VALUES (?,?,?,?, 'pending')
            ON CONFLICT(archive_url) DO NOTHING
            """,
            (archive_url, year, month, etag),
        )
        conn.commit()

        response = client.fetch_archive(archive_url, etag=etag)
        report.archives_fetched += 1
        log(f"{year:04d}-{month:02d}: HTTP {response.status_code}")

        if response.not_modified:
            report.archives_not_modified += 1
            conn.execute(
                """UPDATE sync_state
                      SET last_fetched_at = ?, last_status_code = 304,
                          status = CASE WHEN ? THEN 'complete' ELSE status END
                    WHERE archive_url = ?""",
                (_now(), 1 if _is_past_month(year, month) else 0, archive_url),
            )
            conn.commit()
            continue

        month_failures = 0
        for game_obj in response.games:
            if max_games is not None and report.games_imported >= max_games:
                break
            report.games_seen += 1
            try:
                gid = external_game_id(game_obj)
                if gid in known:
                    report.games_duplicate += 1
                    continue
                pgn_text = game_obj.get("pgn")
                if not pgn_text:
                    raise PgnParseError("game object carries no 'pgn' field")

                path = raw_pgn_path(config, year, month, gid)
                if write_raw_pgn(path, pgn_text):
                    report.pgn_files_written += 1
                insert_game(conn, config, game_obj, year, month, path)
                known.add(gid)
                report.games_imported += 1
            except Exception as exc:  # noqa: BLE001 - one bad game must not kill the run
                month_failures += 1
                report.games_failed += 1
                report.errors.append(f"{game_obj.get('url', '<no url>')}: {exc}")
        conn.commit()

        truncated = max_games is not None and report.games_imported >= max_games
        fully_processed = not truncated and month_failures == 0
        complete = fully_processed and _is_past_month(year, month)
        # Only cache the ETag once every game in the month was handled. A month
        # cut short by --max-games or by a parse failure must re-fetch with a
        # 200 next time; otherwise a 304 would skip the games we never imported.
        conn.execute(
            """UPDATE sync_state
                  SET etag = ?, games_seen = ?, last_fetched_at = ?,
                      last_status_code = ?, status = ?
                WHERE archive_url = ?""",
            (
                response.etag if fully_processed else None,
                len(response.games),
                _now(),
                response.status_code,
                "complete" if complete else "pending",
                archive_url,
            ),
        )
        conn.commit()

    conn.close()
    return report
