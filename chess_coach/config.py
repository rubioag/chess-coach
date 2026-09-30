"""Configuration loading.

No personal data is hardcoded anywhere in the codebase. Everything that
identifies the user lives in config.yaml (or config.local.yaml, which
overrides it and is git-ignored).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .evaluation import PhaseRules, Thresholds

PLACEHOLDER = "[REPLACE_ME]"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# One YAML file per additional player. Personal data lives here, so the whole
# directory is git-ignored apart from the example.
PROFILES_DIR = PROJECT_ROOT / "profiles"

# The profile name used when --profile is not given. It keeps the original
# paths (data/games, data/chess_coach.db) so pre-existing data is untouched.
DEFAULT_PROFILE = "default"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpConfig:
    user_agent: str
    timeout_seconds: float = 30.0
    request_delay_seconds: float = 1.0
    max_retries: int = 5


@dataclass(frozen=True)
class StockfishConfig:
    path: str
    historical_depth: int = 14
    priority_depth: int = 18
    multipv: int = 1
    threads: int = 1
    hash_mb: int = 128


@dataclass(frozen=True)
class AnalysisConfig:
    """Everything that shapes a move metric lives here, never inline in code."""

    eval_cap_cp: int
    thresholds: Thresholds
    phase_rules: PhaseRules


@dataclass(frozen=True)
class Config:
    profile: str
    username: str
    platform: str
    http: HttpConfig
    raw_games_dir: Path
    database: Path
    stockfish: StockfishConfig
    analysis: AnalysisConfig

    @property
    def username_lower(self) -> str:
        return self.username.lower()


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def profile_path(profile: str) -> Path:
    return PROFILES_DIR / f"{profile}.yaml"


def available_profiles() -> list[str]:
    """Profile names on disk, plus the implicit default. Sorted, no duplicates."""
    names = {DEFAULT_PROFILE}
    if PROFILES_DIR.is_dir():
        names |= {
            p.stem for p in PROFILES_DIR.glob("*.yaml")
            if not p.name.endswith(".example.yaml")
        }
    return sorted(names)


def _default_profile_paths(profile: str) -> tuple[str, str]:
    """Storage for a named profile, isolated by construction.

    A profile that does not state its own paths gets its own subtree rather
    than inheriting the base config's. Inheriting would silently point two
    players at one database, which is the single worst failure this design has
    to prevent - so it is prevented by default, not by remembering to set it.
    """
    return (
        f"data/profiles/{profile}/games",
        f"data/profiles/{profile}/chess_coach.db",
    )


def load_config(
    path: str | os.PathLike[str] | None = None,
    profile: str | None = None,
) -> Config:
    """Load config.yaml, overlay config.local.yaml, then the profile file.

    Layering, lowest precedence first:

        config.yaml            shared settings: engine, thresholds, phase rules
        config.local.yaml      the default profile's identity (git-ignored)
        profiles/<name>.yaml   one additional player (git-ignored)

    Without `profile` the behaviour is exactly what it was before profiles
    existed, so pre-existing data and commands keep working untouched.
    """
    base_path = Path(path) if path else PROJECT_ROOT / "config.yaml"
    if not base_path.exists():
        raise ConfigError(f"config file not found: {base_path}")

    data = _read_yaml(base_path)
    local_path = base_path.with_name("config.local.yaml")
    if path is None and local_path.exists():
        data = _deep_merge(data, _read_yaml(local_path))

    # Paths declared by the base layers belong to the default profile only.
    base_paths = dict(data.get("paths") or {})

    profile_name = profile or DEFAULT_PROFILE
    profile_data: dict[str, Any] = {}
    if profile and profile != DEFAULT_PROFILE:
        p_path = profile_path(profile)
        if not p_path.exists():
            known = ", ".join(available_profiles())
            raise ConfigError(
                f"profile not found: {p_path}. Known profiles: {known}"
            )
        profile_data = _read_yaml(p_path)
        data = _deep_merge(data, profile_data)

    username = str(data.get("username", "")).strip()
    if not username or PLACEHOLDER in username:
        raise ConfigError(
            "username is not configured. Set 'username' in config.yaml "
            "(or config.local.yaml)."
        )

    http_raw = data.get("http") or {}
    user_agent = str(http_raw.get("user_agent", "")).strip()
    if not user_agent or PLACEHOLDER in user_agent:
        raise ConfigError(
            "http.user_agent is not configured. chess.com asks for a "
            "User-Agent carrying tool name, username and contact."
        )

    http = HttpConfig(
        user_agent=user_agent,
        timeout_seconds=float(http_raw.get("timeout_seconds", 30.0)),
        request_delay_seconds=float(http_raw.get("request_delay_seconds", 1.0)),
        max_retries=int(http_raw.get("max_retries", 5)),
    )

    if profile_name == DEFAULT_PROFILE:
        paths = base_paths
        default_raw, default_db = "data/games", "data/chess_coach.db"
    else:
        # Only paths declared by the profile file itself are honoured; the base
        # layers' paths belong to the default profile.
        paths = dict(profile_data.get("paths") or {})
        default_raw, default_db = _default_profile_paths(profile_name)

    raw_games_dir = PROJECT_ROOT / str(paths.get("raw_games_dir", default_raw))
    database = PROJECT_ROOT / str(paths.get("database", default_db))

    if profile_name != DEFAULT_PROFILE:
        base_db = PROJECT_ROOT / str(base_paths.get("database", "data/chess_coach.db"))
        base_raw = PROJECT_ROOT / str(base_paths.get("raw_games_dir", "data/games"))
        if database == base_db or raw_games_dir == base_raw:
            raise ConfigError(
                f"profile '{profile_name}' points at the default profile's "
                "storage. Two players must never share a database or a raw PGN "
                "directory; remove the 'paths' override from "
                f"{profile_path(profile_name)} to get isolated storage."
            )

    sf_raw = data.get("stockfish") or {}
    stockfish = StockfishConfig(
        path=str(sf_raw.get("path", "stockfish")),
        historical_depth=int(sf_raw.get("historical_depth", 14)),
        priority_depth=int(sf_raw.get("priority_depth", 18)),
        multipv=int(sf_raw.get("multipv", 1)),
        threads=int(sf_raw.get("threads", 1)),
        hash_mb=int(sf_raw.get("hash_mb", 128)),
    )
    if stockfish.multipv != 1:
        raise ConfigError(
            "V1 analyses with MultiPV 1 only; MultiPV 3 triples the cost for "
            "alternatives we do not yet know we need."
        )

    analysis_raw = data.get("analysis") or {}
    thresholds_raw = analysis_raw.get("thresholds") or {}
    phase_raw = analysis_raw.get("phase") or {}
    try:
        thresholds = Thresholds(
            inaccuracy_cp=int(thresholds_raw.get("inaccuracy_cp", 50)),
            mistake_cp=int(thresholds_raw.get("mistake_cp", 100)),
            blunder_cp=int(thresholds_raw.get("blunder_cp", 300)),
        )
    except ValueError as exc:
        raise ConfigError(f"invalid analysis.thresholds: {exc}") from exc

    analysis = AnalysisConfig(
        eval_cap_cp=int(analysis_raw.get("eval_cap_cp", 1000)),
        thresholds=thresholds,
        phase_rules=PhaseRules(
            opening_max_ply=int(phase_raw.get("opening_max_ply", 20)),
            opening_min_material=int(phase_raw.get("opening_min_material", 58)),
            endgame_max_material=int(phase_raw.get("endgame_max_material", 20)),
        ),
    )

    return Config(
        profile=profile_name,
        username=username,
        platform=str(data.get("platform", "chess.com")),
        http=http,
        raw_games_dir=raw_games_dir,
        database=database,
        stockfish=stockfish,
        analysis=analysis,
    )
