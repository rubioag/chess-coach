from __future__ import annotations

import pytest

from chess_coach.pgn_parser import (
    PgnParseError,
    end_time_iso,
    parse_pgn,
    player_color,
    player_result,
)


def test_parses_full_headers(pgn_full: str) -> None:
    parsed = parse_pgn(pgn_full)
    assert parsed.white_username == "TestPlayer"
    assert parsed.black_username == "Opponent"
    assert parsed.white_rating == 1500
    assert parsed.black_rating == 1480
    assert parsed.result == "1-0"
    assert parsed.time_control == "600"
    assert parsed.eco == "C50"
    assert parsed.opening == "Italian Game"
    assert parsed.variation == "Giuoco Piano"
    assert parsed.utc_date == "2024.03.01"
    assert parsed.utc_time == "12:00:00"


def test_ply_count_and_final_fen(pgn_full: str) -> None:
    parsed = parse_pgn(pgn_full)
    assert parsed.ply_count == 6
    # 3. Bc4 Bc5 reached; Black just moved, so it is White to play on move 4.
    assert parsed.final_fen.split()[1] == "w"
    assert parsed.final_fen.startswith(
        "r1bqk1nr/pppp1ppp/2n5/2b1p3/2B1P3/5N2/PPPP1PPP/RNBQK2R"
    )


def test_missing_eco_headers_are_null_not_unknown(pgn_no_eco: str) -> None:
    """Header absent entirely -> NULL, so we can measure the real gap."""
    parsed = parse_pgn(pgn_no_eco)
    assert parsed.eco is None
    assert parsed.opening is None
    assert parsed.variation is None
    assert parsed.result == "0-1"


def test_present_but_empty_eco_headers_are_unknown(pgn_empty_eco: str) -> None:
    """Header present but unclassifiable -> 'Unknown', distinct from NULL."""
    parsed = parse_pgn(pgn_empty_eco)
    assert parsed.eco == "Unknown"
    assert parsed.opening == "Unknown"
    assert parsed.variation is None


def test_unreadable_pgn_raises() -> None:
    with pytest.raises(PgnParseError):
        parse_pgn("")


def test_player_color_is_case_insensitive(pgn_full: str, pgn_no_eco: str) -> None:
    assert player_color(parse_pgn(pgn_full), "testplayer") == "white"
    assert player_color(parse_pgn(pgn_no_eco), "TESTPLAYER") == "black"
    assert player_color(parse_pgn(pgn_full), "somebody-else") is None


@pytest.mark.parametrize(
    "color,result,expected",
    [
        ("white", "1-0", "win"),
        ("white", "0-1", "loss"),
        ("black", "0-1", "win"),
        ("black", "1-0", "loss"),
        ("white", "1/2-1/2", "draw"),
        ("black", "1/2-1/2", "draw"),
        ("white", "*", None),
        (None, "1-0", None),
        ("white", None, None),
    ],
)
def test_player_result(color, result, expected) -> None:
    assert player_result(color, result) == expected


def test_end_time_iso() -> None:
    assert end_time_iso({"end_time": 1709294400}).startswith("2024-03-01T")
    assert end_time_iso({}) is None
