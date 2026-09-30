from __future__ import annotations

import pytest

from chess_coach.chesscom import (
    ChessComError,
    external_game_id,
    game_id_slug,
    parse_archive_url,
)


def test_parse_archive_url() -> None:
    assert parse_archive_url(
        "https://api.chess.com/pub/player/x/games/2024/03"
    ) == (2024, 3)
    assert parse_archive_url(
        "https://api.chess.com/pub/player/x/games/2019/12/"
    ) == (2019, 12)


def test_parse_archive_url_rejects_garbage() -> None:
    with pytest.raises(ChessComError):
        parse_archive_url("https://api.chess.com/pub/player/x/games")


def test_external_game_id_is_the_url_verbatim() -> None:
    """We never parse the internal id: chess.com already changed it once."""
    url = "https://www.chess.com/game/live/1234567890"
    assert external_game_id({"url": url}) == url

    uuid_url = "https://www.chess.com/game/live/0f8fad5b-d9cb-469f-a165-70867728950e"
    assert external_game_id({"url": uuid_url}) == uuid_url


def test_external_game_id_requires_url() -> None:
    with pytest.raises(ChessComError):
        external_game_id({"pgn": "..."})


def test_game_id_slug_is_filesystem_safe() -> None:
    assert game_id_slug("https://www.chess.com/game/live/1234567890") == "1234567890"
    assert (
        game_id_slug("https://www.chess.com/game/live/0f8fad5b-d9cb-469f-a165-708677289")
        == "0f8fad5b-d9cb-469f-a165-708677289"
    )
    assert game_id_slug("https://www.chess.com/game/live/abc/") == "abc"
    assert game_id_slug("https://example.com/game/a b:c") == "a_b_c"
