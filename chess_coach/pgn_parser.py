"""Tolerant PGN parsing.

chess.com PGNs often lack ECO/Opening/Variation headers; lichess usually has
them. A missing header must never fail the row. We keep the distinction:

    NULL       -> header missing / unavailable
    'Unknown'  -> header present but the platform could not classify it

so we can later measure how many games actually lack opening data instead of
hiding the gap behind a default.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import chess
import chess.pgn

UNKNOWN = "Unknown"
_EMPTY_MARKERS = {"", "?", "??", "-", "unknown", "n/a", "none"}

RESULT_TO_PLAYER = {
    ("white", "1-0"): "win",
    ("white", "0-1"): "loss",
    ("black", "1-0"): "loss",
    ("black", "0-1"): "win",
    ("white", "1/2-1/2"): "draw",
    ("black", "1/2-1/2"): "draw",
}


@dataclass
class ParsedGame:
    headers: dict[str, str] = field(default_factory=dict)
    white_username: str | None = None
    black_username: str | None = None
    white_rating: int | None = None
    black_rating: int | None = None
    result: str | None = None
    termination: str | None = None
    time_control: str | None = None
    eco: str | None = None
    eco_url: str | None = None
    opening: str | None = None
    variation: str | None = None
    utc_date: str | None = None
    utc_time: str | None = None
    ply_count: int | None = None
    final_fen: str | None = None


class PgnParseError(RuntimeError):
    pass


def _header(headers: Any, key: str) -> str | None:
    """Return NULL for a missing header, 'Unknown' for a present-but-empty one."""
    if key not in headers:
        return None
    raw = str(headers[key]).strip()
    if raw.lower() in _EMPTY_MARKERS:
        return UNKNOWN
    return raw


def _plain(headers: Any, key: str) -> str | None:
    """Same as _header but collapses the unknown marker back to NULL.

    Used for fields where 'Unknown' carries no analytical meaning (ratings,
    dates, usernames) — there we only care whether we have a usable value.
    """
    value = _header(headers, key)
    return None if value == UNKNOWN else value


def _int(headers: Any, key: str) -> int | None:
    value = _plain(headers, key)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_pgn(pgn_text: str) -> ParsedGame:
    """Parse a single PGN into structured fields. Raises on unreadable PGN."""
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        raise PgnParseError("PGN could not be read (empty or malformed)")

    headers = game.headers
    parsed = ParsedGame(headers={k: v for k, v in headers.items()})

    parsed.white_username = _plain(headers, "White")
    parsed.black_username = _plain(headers, "Black")
    parsed.white_rating = _int(headers, "WhiteElo")
    parsed.black_rating = _int(headers, "BlackElo")
    parsed.result = _plain(headers, "Result")
    parsed.termination = _plain(headers, "Termination")
    parsed.time_control = _plain(headers, "TimeControl")

    parsed.eco = _header(headers, "ECO")
    parsed.eco_url = _plain(headers, "ECOUrl")
    parsed.opening = _header(headers, "Opening")
    parsed.variation = _header(headers, "Variation")

    parsed.utc_date = _plain(headers, "UTCDate") or _plain(headers, "Date")
    parsed.utc_time = _plain(headers, "UTCTime")

    board = game.board()
    ply = 0
    for move in game.mainline_moves():
        board.push(move)
        ply += 1
    parsed.ply_count = ply
    parsed.final_fen = board.fen()

    return parsed


def player_color(parsed: ParsedGame, username: str) -> str | None:
    target = username.lower()
    if (parsed.white_username or "").lower() == target:
        return "white"
    if (parsed.black_username or "").lower() == target:
        return "black"
    return None


def player_result(color: str | None, result: str | None) -> str | None:
    if color is None or result is None:
        return None
    return RESULT_TO_PLAYER.get((color, result))


def end_time_iso(game_obj: dict[str, Any]) -> str | None:
    """chess.com supplies `end_time` as a unix timestamp."""
    end_time = game_obj.get("end_time")
    if end_time is None:
        return None
    return datetime.fromtimestamp(int(end_time), tz=timezone.utc).isoformat()
