from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chess_coach.config import (  # noqa: E402
    AnalysisConfig,
    Config,
    HttpConfig,
    StockfishConfig,
)
from chess_coach.evaluation import PhaseRules, Thresholds  # noqa: E402


def make_config(tmp_path: Path, profile: str = "default", username: str = "TestPlayer") -> Config:
    """A config whose storage is isolated per profile, like the real thing."""
    root = tmp_path / profile
    return Config(
        profile=profile,
        username=username,
        platform="chess.com",
        http=HttpConfig(user_agent="chess-coach/test (contact: test@example.com)"),
        raw_games_dir=root / "games",
        database=root / "chess_coach.db",
        stockfish=StockfishConfig(path="stockfish"),
        analysis=AnalysisConfig(
            eval_cap_cp=1000,
            thresholds=Thresholds(),
            phase_rules=PhaseRules(),
        ),
    )


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return make_config(tmp_path)


@pytest.fixture
def other_config(tmp_path: Path) -> Config:
    """A second player, sharing the code but nothing else."""
    return make_config(tmp_path, profile="otherplayer", username="OtherPlayer")


PGN_FULL = """[Event "Live Chess"]
[Site "Chess.com"]
[Date "2024.03.01"]
[White "TestPlayer"]
[Black "Opponent"]
[Result "1-0"]
[WhiteElo "1500"]
[BlackElo "1480"]
[TimeControl "600"]
[ECO "C50"]
[ECOUrl "https://www.chess.com/openings/Italian-Game"]
[Opening "Italian Game"]
[Variation "Giuoco Piano"]
[Termination "TestPlayer won by resignation"]
[UTCDate "2024.03.01"]
[UTCTime "12:00:00"]

1. e4 e5 2. Nf3 Nc6 3. Bc4 Bc5 1-0
"""

PGN_NO_ECO = """[Event "Live Chess"]
[Site "Chess.com"]
[Date "2024.04.02"]
[White "Opponent"]
[Black "TestPlayer"]
[Result "0-1"]
[TimeControl "180"]

1. d4 d5 2. c4 e6 0-1
"""

PGN_EMPTY_ECO = """[Event "Live Chess"]
[White "TestPlayer"]
[Black "Opponent"]
[Result "1/2-1/2"]
[ECO "?"]
[Opening ""]

1. e4 e5 1/2-1/2
"""


@pytest.fixture
def pgn_full() -> str:
    return PGN_FULL


@pytest.fixture
def pgn_no_eco() -> str:
    return PGN_NO_ECO


@pytest.fixture
def pgn_empty_eco() -> str:
    return PGN_EMPTY_ECO
