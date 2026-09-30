"""chess.com PubAPI client.

Read-only, no API key. Requests are strictly serial: parallel requests
against this API trigger 429 / abnormal-activity blocks.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Iterator

import requests

from .config import Config

BASE = "https://api.chess.com/pub"
ARCHIVE_RE = re.compile(r"/games/(\d{4})/(\d{2})/?$")


class ChessComError(RuntimeError):
    pass


@dataclass
class ArchiveResponse:
    """Result of fetching one monthly archive."""

    status_code: int
    etag: str | None
    games: list[dict[str, Any]]

    @property
    def not_modified(self) -> bool:
        return self.status_code == 304


def parse_archive_url(url: str) -> tuple[int, int]:
    """Extract (year, month) from a monthly archive URL."""
    match = ARCHIVE_RE.search(url)
    if not match:
        raise ChessComError(f"unrecognized archive url: {url}")
    return int(match.group(1)), int(match.group(2))


def external_game_id(game: dict[str, Any]) -> str:
    """Stable identifier for a game.

    The `url` field is used verbatim as the key; its last path segment is the
    filesystem-safe form. We never parse or assume the internal id format,
    which chess.com has already changed once (integer -> uuid).
    """
    url = game.get("url")
    if not url:
        raise ChessComError("game object has no 'url' field")
    return str(url)


def game_id_slug(external_id: str) -> str:
    """Filesystem-safe filename stem derived from external_game_id."""
    slug = external_id.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", slug)
    if not slug:
        raise ChessComError(f"cannot derive filename from id: {external_id}")
    return slug


class ChessComClient:
    def __init__(self, config: Config, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": config.http.user_agent,
            "Accept": "application/json",
        })
        self._last_request_at = 0.0

    # -- internals ---------------------------------------------------------

    def _throttle(self) -> None:
        delay = self.config.http.request_delay_seconds
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < delay:
            time.sleep(delay - elapsed)

    def _get(self, url: str, headers: dict[str, str] | None = None) -> requests.Response:
        attempts = 0
        while True:
            attempts += 1
            self._throttle()
            response = self.session.get(
                url, headers=headers or {}, timeout=self.config.http.timeout_seconds
            )
            self._last_request_at = time.monotonic()

            if response.status_code in (200, 304, 404):
                return response

            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or attempts > self.config.http.max_retries:
                raise ChessComError(
                    f"GET {url} failed: HTTP {response.status_code}"
                )

            retry_after = response.headers.get("Retry-After")
            wait = float(retry_after) if retry_after and retry_after.isdigit() else 2.0 ** attempts
            time.sleep(min(wait, 60.0))

    # -- public API --------------------------------------------------------

    def list_archives(self) -> list[str]:
        url = f"{BASE}/player/{self.config.username_lower}/games/archives"
        response = self._get(url)
        if response.status_code == 404:
            raise ChessComError(
                f"player not found on chess.com: {self.config.username}"
            )
        payload = response.json()
        return list(payload.get("archives", []))

    def fetch_archive(self, archive_url: str, etag: str | None = None) -> ArchiveResponse:
        headers = {"If-None-Match": etag} if etag else {}
        response = self._get(archive_url, headers=headers)
        if response.status_code == 304:
            return ArchiveResponse(304, etag, [])
        if response.status_code == 404:
            return ArchiveResponse(404, None, [])
        payload = response.json()
        return ArchiveResponse(200, response.headers.get("ETag"), list(payload.get("games", [])))

    def iter_archives(self, archives: list[str]) -> Iterator[str]:
        """Serial iteration helper; kept explicit to discourage concurrency."""
        for archive in archives:
            yield archive
