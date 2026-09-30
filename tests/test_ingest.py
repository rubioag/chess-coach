"""Ingestion behaviour: idempotency, dedup, ETag handling, failure isolation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from chess_coach.chesscom import ArchiveResponse
from chess_coach.db import init_db
from chess_coach.ingest import _is_past_month, ingest, raw_pgn_path

PAST = "https://api.chess.com/pub/player/testplayer/games/2024/03"


class FakeClient:
    """Stand-in for ChessComClient. Records every archive request."""

    def __init__(self, archives, games_by_archive, etags=None):
        self.archives = archives
        self.games_by_archive = games_by_archive
        self.etags = etags or {}
        self.requests: list[tuple[str, str | None]] = []
        self.not_modified_for: set[str] = set()

    def list_archives(self):
        return list(self.archives)

    def fetch_archive(self, archive_url, etag=None):
        self.requests.append((archive_url, etag))
        if archive_url in self.not_modified_for and etag is not None:
            return ArchiveResponse(304, etag, [])
        return ArchiveResponse(
            200,
            self.etags.get(archive_url, 'W/"etag-1"'),
            list(self.games_by_archive.get(archive_url, [])),
        )


def game(url: str, pgn: str, **extra):
    obj = {"url": url, "pgn": pgn, "time_class": "rapid", "rated": True,
           "end_time": 1709294400}
    obj.update(extra)
    return obj


def rows(config):
    conn = init_db(config.database)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM games ORDER BY external_game_id"
        )]
    finally:
        conn.close()


def sync_rows(config):
    conn = init_db(config.database)
    try:
        return {r["archive_url"]: dict(r) for r in conn.execute("SELECT * FROM sync_state")}
    finally:
        conn.close()


def test_is_past_month() -> None:
    now = datetime(2024, 6, 15, tzinfo=timezone.utc)
    assert _is_past_month(2024, 5, now)
    assert not _is_past_month(2024, 6, now)   # current month is never complete
    assert not _is_past_month(2024, 7, now)


def test_imports_games_and_writes_raw_pgn(config, pgn_full, pgn_no_eco) -> None:
    client = FakeClient(
        [PAST],
        {PAST: [game("https://www.chess.com/game/live/1", pgn_full),
                game("https://www.chess.com/game/live/2", pgn_no_eco)]},
    )
    report = ingest(config, client=client, log=lambda _: None)

    assert (report.games_imported, report.games_duplicate, report.games_failed) == (2, 0, 0)
    assert report.pgn_files_written == 2

    path = raw_pgn_path(config, 2024, 3, "https://www.chess.com/game/live/1")
    assert path.exists()
    assert path.read_text(encoding="utf-8") == pgn_full   # stored unmodified
    assert path.parent == config.raw_games_dir / "2024" / "03"

    imported = rows(config)
    assert [r["external_game_id"] for r in imported] == [
        "https://www.chess.com/game/live/1",
        "https://www.chess.com/game/live/2",
    ]
    assert imported[0]["player_color"] == "white"
    assert imported[0]["player_result"] == "win"
    assert imported[0]["opening"] == "Italian Game"
    assert imported[0]["time_class"] == "rapid"
    assert imported[0]["rated"] == 1
    assert imported[0]["analysis_status"] == "pending"
    assert imported[1]["player_color"] == "black"
    assert imported[1]["player_result"] == "win"
    assert imported[1]["opening"] is None       # header absent -> NULL, not 'Unknown'


def test_second_run_is_idempotent(config, pgn_full) -> None:
    games = {PAST: [game("https://www.chess.com/game/live/1", pgn_full)]}
    first = ingest(config, client=FakeClient([PAST], games), log=lambda _: None)
    assert first.games_imported == 1

    # Month is in the past and parsed cleanly -> marked complete, so a second
    # run must not even request it again.
    client = FakeClient([PAST], games)
    second = ingest(config, client=client, log=lambda _: None)
    assert second.archives_skipped_complete == 1
    assert client.requests == []
    assert second.games_imported == 0
    assert len(rows(config)) == 1


def test_dedup_runs_even_when_month_is_refetched(config, pgn_full, pgn_no_eco) -> None:
    """ETag saves bandwidth; it is not the only duplicate protection."""
    current = datetime.now(tz=timezone.utc)
    archive = (
        "https://api.chess.com/pub/player/testplayer/games/"
        f"{current.year:04d}/{current.month:02d}"
    )
    g1 = game("https://www.chess.com/game/live/1", pgn_full)
    client = FakeClient([archive], {archive: [g1]})
    ingest(config, client=client, log=lambda _: None)

    # Current month is never 'complete', so it is refetched with the stored
    # ETag; the archive replays game 1 and adds game 2.
    g2 = game("https://www.chess.com/game/live/2", pgn_no_eco)
    client.games_by_archive[archive] = [g1, g2]
    report = ingest(config, client=client, log=lambda _: None)

    assert client.requests[-1][1] == 'W/"etag-1"'   # If-None-Match was sent
    assert report.games_duplicate == 1
    assert report.games_imported == 1
    assert len(rows(config)) == 2


def test_304_skips_parsing(config, pgn_full) -> None:
    current = datetime.now(tz=timezone.utc)
    archive = (
        "https://api.chess.com/pub/player/testplayer/games/"
        f"{current.year:04d}/{current.month:02d}"
    )
    client = FakeClient([archive], {archive: [game("https://www.chess.com/game/live/1", pgn_full)]})
    ingest(config, client=client, log=lambda _: None)

    client.not_modified_for.add(archive)
    report = ingest(config, client=client, log=lambda _: None)
    assert report.archives_not_modified == 1
    assert report.games_seen == 0
    assert sync_rows(config)[archive]["last_status_code"] == 304


def test_bad_game_is_recorded_and_month_stays_pending(config, pgn_full) -> None:
    client = FakeClient(
        [PAST],
        {PAST: [game("https://www.chess.com/game/live/1", pgn_full),
                {"url": "https://www.chess.com/game/live/2"}]},  # no pgn
    )
    report = ingest(config, client=client, log=lambda _: None)

    assert report.games_imported == 1
    assert report.games_failed == 1
    assert "live/2" in report.errors[0]
    # A month with failures is never silently marked complete.
    assert sync_rows(config)[PAST]["status"] == "pending"


def test_max_games_leaves_month_pending(config, pgn_full, pgn_no_eco) -> None:
    client = FakeClient(
        [PAST],
        {PAST: [game("https://www.chess.com/game/live/1", pgn_full),
                game("https://www.chess.com/game/live/2", pgn_no_eco)]},
    )
    report = ingest(config, client=client, max_games=1, log=lambda _: None)
    assert report.games_imported == 1
    assert sync_rows(config)[PAST]["status"] == "pending"


def test_existing_raw_pgn_is_never_rewritten(config, pgn_full) -> None:
    path = raw_pgn_path(config, 2024, 3, "https://www.chess.com/game/live/1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ORIGINAL", encoding="utf-8")

    client = FakeClient([PAST], {PAST: [game("https://www.chess.com/game/live/1", pgn_full)]})
    report = ingest(config, client=client, log=lambda _: None)

    assert report.pgn_files_written == 0
    assert path.read_text(encoding="utf-8") == "ORIGINAL"


def test_truncated_month_does_not_cache_etag(config, pgn_full, pgn_no_eco) -> None:
    """A month cut short by --max-games must re-fetch with a 200, not a 304.

    Caching the ETag there would make the next run skip parsing and silently
    lose the games the truncated run never reached.
    """
    client = FakeClient(
        [PAST],
        {PAST: [game("https://www.chess.com/game/live/1", pgn_full),
                game("https://www.chess.com/game/live/2", pgn_no_eco)]},
    )
    ingest(config, client=client, max_games=1, log=lambda _: None)
    assert sync_rows(config)[PAST]["etag"] is None

    client.not_modified_for.add(PAST)   # would 304 only if an ETag were sent
    report = ingest(config, client=client, log=lambda _: None)
    assert client.requests[-1][1] is None
    assert report.games_imported == 1
    assert report.games_duplicate == 1
    assert len(rows(config)) == 2


def test_failed_month_does_not_cache_etag(config, pgn_full) -> None:
    client = FakeClient(
        [PAST],
        {PAST: [game("https://www.chess.com/game/live/1", pgn_full),
                {"url": "https://www.chess.com/game/live/2"}]},
    )
    ingest(config, client=client, log=lambda _: None)
    assert sync_rows(config)[PAST]["etag"] is None


def test_fully_processed_month_caches_etag(config, pgn_full) -> None:
    client = FakeClient([PAST], {PAST: [game("https://www.chess.com/game/live/1", pgn_full)]})
    ingest(config, client=client, log=lambda _: None)
    assert sync_rows(config)[PAST]["etag"] == 'W/"etag-1"'
