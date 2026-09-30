"""Opening identity derived from moves. Descriptive grouping only."""

from __future__ import annotations

from chess_coach.db import init_db
from chess_coach.moves import extract_moves
from chess_coach.openings import (
    coverage,
    opening_families,
    opening_key_for_game,
    opening_keys,
)

from .helpers import SCHOLARS_MATE, seed_game

ITALIAN = """[Event "t"]
[White "TestPlayer"]
[Black "Opponent"]
[Result "1-0"]
[TimeControl "300+5"]
[ECO "C50"]

1. e4 e5 2. Bc4 Nc6 3. Nf3 Nf6 4. d3 Bc5 1-0
"""

ITALIAN_OTHER_ENDING = """[Event "t"]
[White "TestPlayer"]
[Black "Other"]
[Result "0-1"]
[TimeControl "300+5"]
[ECO "C50"]

1. e4 e5 2. Bc4 Nc6 3. Nf3 Nf6 4. d3 d6 5. O-O Be7 0-1
"""

QUEENS_PAWN = """[Event "t"]
[White "TestPlayer"]
[Black "Opponent"]
[Result "1-0"]
[TimeControl "300+5"]
[ECO "D02"]

1. d4 d5 2. Nf3 Nf6 3. Bf4 e6 4. e3 Be7 1-0
"""


def prepared(config, games):
    for index, (pgn, external) in enumerate(games, start=1):
        seed_game(config, pgn, external, name=str(index))
    extract_moves(config, log=lambda _: None)
    return init_db(config.database)


def test_key_is_built_from_the_moves_not_the_platform_label(config) -> None:
    gid, _ = seed_game(config, ITALIAN, "https://x/1")
    extract_moves(config, log=lambda _: None)
    conn = init_db(config.database)

    key = opening_key_for_game(conn, gid, plies=8)
    assert key.key == "e2e4 e7e5 f1c4 b8c6 g1f3 g8f6 d2d3 f8c5"
    assert key.key_san == "e4 e5 Bc4 Nc6 Nf3 Nf6 d3 Bc5"
    assert key.root == "e4 e5"
    assert key.plies_used == 8
    assert key.eco == "C50"          # kept only as a cross-check
    conn.close()


def test_games_sharing_a_prefix_group_together(config) -> None:
    conn = prepared(config, [(ITALIAN, "https://x/1"), (ITALIAN_OTHER_ENDING, "https://x/2")])
    # The two games diverge at ply 8, so a 6-ply key merges them...
    families = opening_families(conn, plies=6)
    assert len(families) == 1
    assert families[0].games == 2
    # ... and an 8-ply key separates them. Depth is a query parameter.
    assert len(opening_families(conn, plies=8)) == 2
    conn.close()


def test_different_first_moves_never_share_a_family(config) -> None:
    conn = prepared(config, [(ITALIAN, "https://x/1"), (QUEENS_PAWN, "https://x/2")])
    families = opening_families(conn, plies=8)
    assert len(families) == 2
    assert {f.root for f in families} == {"e4 e5", "d4 d5"}
    assert all(f.games == 1 for f in families)
    conn.close()


def test_short_games_use_the_plies_they_have(config) -> None:
    gid, _ = seed_game(config, SCHOLARS_MATE, "https://x/1")
    extract_moves(config, log=lambda _: None)
    conn = init_db(config.database)
    key = opening_key_for_game(conn, gid, plies=20)
    assert key.plies_used == 7          # the game is only 7 plies long
    conn.close()


def test_eco_disagreement_within_a_family_is_visible(config) -> None:
    conn = prepared(config, [(ITALIAN, "https://x/1"), (ITALIAN_OTHER_ENDING, "https://x/2")])
    family = opening_families(conn, plies=6)[0]
    assert family.eco_agrees          # both are C50

    conn.execute("UPDATE games SET eco = 'C99' WHERE external_game_id = 'https://x/2'")
    conn.commit()
    family = opening_families(conn, plies=6)[0]
    assert not family.eco_agrees      # surfaced, not hidden
    conn.close()


def test_game_without_raw_moves_has_no_key(config) -> None:
    gid, _ = seed_game(config, ITALIAN, "https://x/1")
    conn = init_db(config.database)   # extraction deliberately not run
    assert opening_key_for_game(conn, gid) is None
    assert opening_keys(conn) == []
    conn.close()


def test_coverage_reports_the_grouping_honestly(config) -> None:
    conn = prepared(
        config,
        [(ITALIAN, "https://x/1"), (ITALIAN_OTHER_ENDING, "https://x/2"),
         (QUEENS_PAWN, "https://x/3")],
    )
    stats = coverage(conn, plies=6)
    assert stats["games"] == 3
    assert stats["games_with_key"] == 3
    assert stats["families"] == 2
    assert stats["largest_family"] == 2
    conn.close()
