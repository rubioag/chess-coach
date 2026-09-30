"""Multi-player isolation.

Two players share the code, the engine and the analysis settings. They share
nothing else: separate database, separate raw PGN directory, separate analysis
runs. These tests exist to prove that mixing them is impossible, not merely
unlikely.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from chess_coach.chesscom import ArchiveResponse
import shutil

from chess_coach import config as config_module
from chess_coach.config import ConfigError, DEFAULT_PROFILE, load_config
from chess_coach.db import ProfileMismatch, init_db, open_db
from chess_coach.update import update

from .helpers import StubEngine

_NOW = datetime.now(tz=timezone.utc)


def archive_for(user: str) -> str:
    return (
        f"https://api.chess.com/pub/player/{user.lower()}/games/"
        f"{_NOW.year:04d}/{_NOW.month:02d}"
    )


PGN = """[Event "Live Chess"]
[Site "Chess.com"]
[Date "2024.03.01"]
[White "{white}"]
[Black "{black}"]
[Result "1-0"]
[WhiteElo "1500"]
[BlackElo "1480"]
[TimeControl "300+5"]
[ECO "C20"]
[Termination "{white} won by resignation"]
[UTCDate "2024.03.01"]
[UTCTime "12:00:00"]

1. e4 {{[%clk 0:04:58]}} 1... e5 {{[%clk 0:04:57]}} 2. Bc4 {{[%clk 0:04:55]}} 2... Nc6 {{[%clk 0:04:50]}} 3. Qh5 {{[%clk 0:04:52]}} 3... Nf6 {{[%clk 0:04:40]}} 4. Qxf7# {{[%clk 0:04:50]}} 1-0
"""


def game(n: int, white: str, black: str = "SomeOpponent") -> dict:
    return {
        "url": f"https://www.chess.com/game/live/{n}",
        "pgn": PGN.format(white=white, black=black),
        "time_class": "blitz",
        "rated": True,
        "end_time": 1709294400 + n,
    }


class FakeClient:
    def __init__(self, user: str, games):
        self.user = user
        self.games = list(games)

    def list_archives(self):
        return [archive_for(self.user)]

    def fetch_archive(self, archive_url, etag=None):
        return ArchiveResponse(200, f'W/"etag-{self.user}"', list(self.games))


def run(config, client):
    return update(
        config, client=client, engine_factory=StubEngine, log=lambda _: None
    )


def snapshot(config) -> dict:
    conn = open_db(config)
    try:
        return {
            "games": [
                r[0] for r in conn.execute(
                    "SELECT external_game_id FROM games ORDER BY external_game_id"
                )
            ],
            "usernames": sorted({
                r[0] for r in conn.execute(
                    "SELECT white_username FROM games UNION "
                    "SELECT black_username FROM games"
                )
            }),
            "raw": conn.execute("SELECT COUNT(*) FROM game_moves").fetchone()[0],
            "obs": conn.execute("SELECT COUNT(*) FROM move_analysis").fetchone()[0],
            "runs": conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0],
            "canonical": conn.execute(
                "SELECT COUNT(*) FROM current_move_analysis"
            ).fetchone()[0],
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Storage is physically separate
# --------------------------------------------------------------------------

def test_profiles_use_different_files(config, other_config) -> None:
    assert config.database != other_config.database
    assert config.raw_games_dir != other_config.raw_games_dir
    assert config.username != other_config.username


def test_each_profile_only_holds_its_own_games(config, other_config) -> None:
    run(config, FakeClient("TestPlayer", [game(1, "TestPlayer"), game(2, "TestPlayer")]))
    run(other_config, FakeClient("OtherPlayer", [game(9, "OtherPlayer")]))

    mine = snapshot(config)
    theirs = snapshot(other_config)

    assert mine["games"] == [
        "https://www.chess.com/game/live/1",
        "https://www.chess.com/game/live/2",
    ]
    assert theirs["games"] == ["https://www.chess.com/game/live/9"]

    # Neither player's game id appears in the other's database.
    assert not set(mine["games"]) & set(theirs["games"])
    # And neither player's username appears in the other's game rows.
    assert "OtherPlayer" not in mine["usernames"]
    assert "TestPlayer" not in theirs["usernames"]


def test_raw_pgn_directories_do_not_overlap(config, other_config) -> None:
    run(config, FakeClient("TestPlayer", [game(1, "TestPlayer")]))
    run(other_config, FakeClient("OtherPlayer", [game(9, "OtherPlayer")]))

    mine = {p.name for p in config.raw_games_dir.rglob("*.pgn")}
    theirs = {p.name for p in other_config.raw_games_dir.rglob("*.pgn")}
    assert mine == {"1.pgn"}
    assert theirs == {"9.pgn"}
    assert config.raw_games_dir.resolve() != other_config.raw_games_dir.resolve()


# --------------------------------------------------------------------------
# One profile's work never touches the other
# --------------------------------------------------------------------------

def test_update_for_one_profile_does_not_modify_the_other(config, other_config) -> None:
    run(config, FakeClient("TestPlayer", [game(1, "TestPlayer"), game(2, "TestPlayer")]))
    before = snapshot(config)
    before_bytes = config.database.read_bytes()

    run(other_config, FakeClient("OtherPlayer", [game(9, "OtherPlayer")]))

    # Bytes first: merely OPENING a SQLite file rewrites its header, so the
    # byte check is only meaningful before we read the other profile again.
    assert config.database.read_bytes() == before_bytes
    assert snapshot(config) == before


def test_the_other_profile_is_untouched_by_repeated_cycles(config, other_config) -> None:
    run(other_config, FakeClient("OtherPlayer", [game(9, "OtherPlayer")]))
    theirs = snapshot(other_config)

    client = FakeClient("TestPlayer", [game(1, "TestPlayer")])
    for _ in range(3):
        run(config, client)

    assert snapshot(other_config) == theirs


def test_a_new_profile_starts_empty_regardless_of_the_other(config, other_config) -> None:
    run(config, FakeClient("TestPlayer", [game(n, "TestPlayer") for n in (1, 2, 3)]))
    empty = snapshot(other_config)
    assert empty["games"] == []
    assert (empty["raw"], empty["obs"], empty["runs"], empty["canonical"]) == (0, 0, 0, 0)


# --------------------------------------------------------------------------
# Analysis stays isolated
# --------------------------------------------------------------------------

def test_analysis_runs_of_both_players_coexist_without_conflict(config, other_config) -> None:
    mine = run(config, FakeClient("TestPlayer", [game(1, "TestPlayer")]))
    theirs = run(other_config, FakeClient("OtherPlayer", [game(9, "OtherPlayer")]))

    # Run ids are per-database, so both are free to be 1: they cannot collide.
    assert mine.run_id is not None and theirs.run_id is not None
    assert snapshot(config)["runs"] == 1
    assert snapshot(other_config)["runs"] == 1

    conn = open_db(config)
    assert conn.execute(
        "SELECT COUNT(*) FROM move_analysis"
    ).fetchone()[0] == 7
    conn.close()

    conn = open_db(other_config)
    assert conn.execute(
        "SELECT COUNT(*) FROM move_analysis"
    ).fetchone()[0] == 7
    conn.close()


def test_statistics_are_computed_per_profile(config, other_config) -> None:
    from chess_coach.aggregate import report

    run(config, FakeClient("TestPlayer", [game(n, "TestPlayer") for n in (1, 2)]))
    run(other_config, FakeClient("OtherPlayer", [game(9, "OtherPlayer")]))

    conn = open_db(config)
    mine = report(conn)
    conn.close()
    conn = open_db(other_config)
    theirs = report(conn)
    conn.close()

    assert "games imported               : 2" in mine
    assert "games imported               : 1" in theirs


def test_reanalysis_in_one_profile_leaves_the_other_alone(config, other_config) -> None:
    run(config, FakeClient("TestPlayer", [game(1, "TestPlayer")]))
    run(other_config, FakeClient("OtherPlayer", [game(9, "OtherPlayer")]))
    theirs = snapshot(other_config)

    from chess_coach.analysis import analyze

    analyze(config, engine_factory=StubEngine, force=True, depth=18, log=lambda _: None)

    assert snapshot(config)["runs"] == 2
    assert snapshot(other_config) == theirs


# --------------------------------------------------------------------------
# The guard against a configuration mistake
# --------------------------------------------------------------------------

def test_a_database_refuses_to_open_for_the_wrong_player(config, other_config) -> None:
    """The one way physical isolation could fail is a path typo. This catches it."""
    run(config, FakeClient("TestPlayer", [game(1, "TestPlayer")]))

    from dataclasses import replace

    impostor = replace(other_config, database=config.database)
    with pytest.raises(ProfileMismatch) as excinfo:
        open_db(impostor)
    assert "TestPlayer" in str(excinfo.value)
    assert "OtherPlayer" in str(excinfo.value)


def test_the_owning_username_is_stamped_on_first_open(config) -> None:
    conn = open_db(config)
    owner = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'profile_username'"
    ).fetchone()[0]
    name = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'profile_name'"
    ).fetchone()[0]
    conn.close()
    assert owner == "TestPlayer"
    assert name == "default"


def test_username_comparison_is_case_insensitive(config) -> None:
    """chess.com treats usernames case-insensitively; so must the guard."""
    from dataclasses import replace

    open_db(config).close()
    same_person = replace(config, username="testPLAYER")
    open_db(same_person).close()          # must not raise


def test_an_existing_unstamped_database_is_adopted_not_rejected(config) -> None:
    """Databases created before profiles existed must keep working."""
    conn = init_db(config.database)       # no binding
    conn.execute(
        """INSERT INTO games (external_game_id, platform, pgn_path, archive_year,
                              archive_month, imported_at)
           VALUES ('https://x/1', 'chess.com', 'p.pgn', 2024, 3, 'now')"""
    )
    conn.commit()
    conn.close()

    conn = open_db(config)                # adopts it
    assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 1
    assert conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'profile_username'"
    ).fetchone()[0] == "TestPlayer"
    conn.close()


# --------------------------------------------------------------------------
# Config layering
# --------------------------------------------------------------------------

_TEST_CONTACT = "test@example.com"


def _identity(username: str) -> str:
    """A profile file carries one player's identity: the name and the
    User-Agent chess.com asks for. Same shape as a real profile file."""
    return f'''username: "{username}"
http:
  user_agent: "chess-coach/test (username: {username}; contact: {_TEST_CONTACT})"
'''


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A complete, self-contained project root.

    Layering is exercised for real: the repository's own `config.yaml` is the
    base layer, a generated `config.local.yaml` supplies the default profile's
    identity, and one extra profile file sits in the profiles directory. Only
    the two module constants that point at the checkout are redirected, so
    `load_config` runs its genuine merge and path-derivation logic — it simply
    reads this directory instead of the developer's own git-ignored files.
    """
    shutil.copy(config_module.PROJECT_ROOT / "config.yaml", tmp_path / "config.yaml")
    (tmp_path / "config.local.yaml").write_text(_identity("DefaultPlayer"), encoding="utf-8")

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "second-account.yaml").write_text(_identity("SecondPlayer"), encoding="utf-8")

    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config_module, "PROFILES_DIR", profiles)
    return tmp_path


def test_unknown_profile_is_rejected_with_the_known_ones_listed(project) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(profile="nobody-by-that-name")
    message = str(excinfo.value)
    assert "profile not found" in message
    assert "second-account" in message


def test_default_profile_keeps_the_original_paths(project) -> None:
    cfg = load_config()
    assert cfg.profile == DEFAULT_PROFILE
    assert cfg.username == "DefaultPlayer"
    assert cfg.database.name == "chess_coach.db"
    assert cfg.database.parent.name == "data"


def test_a_named_profile_gets_isolated_storage_without_declaring_paths(project) -> None:
    default = load_config()
    other = load_config(profile="second-account")

    assert other.profile == "second-account"
    assert other.username != default.username
    assert other.database != default.database
    assert other.raw_games_dir != default.raw_games_dir
    assert "second-account" in other.database.as_posix()
    # Analysis settings ARE shared: one engine, one rule set, many players.
    assert other.analysis.thresholds == default.analysis.thresholds
    assert other.stockfish.path == default.stockfish.path


def test_username_never_leaks_between_profiles(project) -> None:
    default = load_config()
    other = load_config(profile="second-account")
    assert default.username not in other.http.user_agent
    assert other.username not in default.http.user_agent


def test_a_game_between_the_two_players_belongs_to_both_perspectives(
    config, other_config
) -> None:
    """Two players who face each other is not contamination.

    A game they played together is a fact about both of them, so it legitimately
    exists in both databases. What must never cross over is the PERSPECTIVE:
    each profile records the game from its own owner's side, so the same result
    is a win in one database and a loss in the other.
    """
    head_to_head = game(42, white="TestPlayer", black="OtherPlayer")
    run(config, FakeClient("TestPlayer", [head_to_head]))
    run(other_config, FakeClient("OtherPlayer", [head_to_head]))

    def perspective(cfg):
        conn = open_db(cfg)
        try:
            return conn.execute(
                "SELECT player_color, player_result FROM games "
                "WHERE external_game_id = ?",
                ("https://www.chess.com/game/live/42",),
            ).fetchone()
        finally:
            conn.close()

    mine = perspective(config)
    theirs = perspective(other_config)

    assert (mine["player_color"], mine["player_result"]) == ("white", "win")
    assert (theirs["player_color"], theirs["player_result"]) == ("black", "loss")
