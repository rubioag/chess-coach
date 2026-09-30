"""Clock parsing and derivation. Pure functions, no PGN file needed."""

from __future__ import annotations

import io

import chess.pgn
import pytest

from chess_coach.clocks import (
    TimeControl,
    clock_readings,
    derive_clock_columns,
    parse_time_control,
)


@pytest.mark.parametrize(
    "header,base,inc",
    [
        ("300+5", 300_000, 5_000),
        ("180+2", 180_000, 2_000),
        ("600", 600_000, 0),
        ("60+0", 60_000, 0),
        ("1/86400", 86_400_000, 0),
    ],
)
def test_parse_time_control(header, base, inc) -> None:
    tc = parse_time_control(header)
    assert (tc.base_ms, tc.increment_ms) == (base, inc)
    assert tc.known


@pytest.mark.parametrize("header", [None, "", "-", "weird", "300+5+7"])
def test_unparseable_time_control_is_unknown_not_an_error(header) -> None:
    """A game we cannot time must still be analysable."""
    tc = parse_time_control(header)
    assert not tc.known
    assert tc.base_ms is None


def test_clock_readings_are_milliseconds_from_the_pgn() -> None:
    pgn = ('[Event "t"]\n[TimeControl "300+5"]\n\n'
           '1. e4 {[%clk 0:05:02.7]} e5 {[%clk 0:05:03.7]} *\n')
    game = chess.pgn.read_game(io.StringIO(pgn))
    assert clock_readings(game) == [302_700, 303_700]


def test_missing_clock_comments_stay_none() -> None:
    """Partial coverage must be visible, never silently filled in."""
    pgn = '[Event "t"]\n\n1. e4 {[%clk 0:05:00]} e5 2. Nf3 *\n'
    game = chess.pgn.read_game(io.StringIO(pgn))
    assert clock_readings(game) == [300_000, None, None]


def test_clock_before_is_the_same_players_previous_reading() -> None:
    """The PGN records the clock AFTER the move; coaching needs BEFORE."""
    tc = TimeControl(300_000, 5_000)
    readings = [302_700, 303_700, 306_500, 307_400]
    derived = derive_clock_columns(readings, tc)

    # Plies 1 and 2 start from the base time.
    assert derived[0][0] == 300_000
    assert derived[1][0] == 300_000
    # Ply 3 is White again: their clock after ply 1.
    assert derived[2][0] == 302_700
    # Ply 4 is Black again: their clock after ply 2.
    assert derived[3][0] == 303_700


def test_time_spent_adds_the_increment_back() -> None:
    """clock_after already includes the increment, so it must be corrected."""
    tc = TimeControl(300_000, 5_000)
    # White starts on 300.0 and ends on 302.7: they thought for 2.3 s, not -2.7.
    (before, after, spent), *_ = derive_clock_columns([302_700], tc)
    assert (before, after) == (300_000, 302_700)
    assert spent == 2_300


def test_time_spent_without_increment() -> None:
    tc = TimeControl(600_000, 0)
    (_, _, spent), *_ = derive_clock_columns([595_000], tc)
    assert spent == 5_000


def test_inconsistent_readings_produce_no_time_spent() -> None:
    """Better a NULL than a nonsense number."""
    tc = TimeControl(300_000, 0)
    (_, _, spent), *_ = derive_clock_columns([400_000], tc)   # gained time somehow
    assert spent is None


def test_unknown_time_control_leaves_derived_columns_empty() -> None:
    derived = derive_clock_columns([302_700, 303_700], TimeControl(None, None))
    assert derived[0][0] is None      # no base time to start from
    assert derived[0][1] == 302_700   # the raw reading survives
    assert derived[0][2] is None      # and no time spent is invented


def test_missing_reading_yields_no_time_spent() -> None:
    derived = derive_clock_columns([None, 303_700], TimeControl(300_000, 5_000))
    assert derived[0] == (300_000, None, None)
