"""CLI entry point.

Every command accepts `--profile NAME` to act on one player. Without it the
default profile is used, which is exactly the behaviour that existed before
profiles were introduced.

    python -m chess_coach profiles        list configured players
    python -m chess_coach ingest          sync games from the platform
    python -m chess_coach extract-moves   raw PGN -> game_moves (incl. clocks)
    python -m chess_coach analyze         Stockfish pass under a recorded run
    python -m chess_coach report          descriptive aggregation
    python -m chess_coach status          counters
"""

from __future__ import annotations

import argparse
import sys
import time

from .analysis import analyze
from .config import ConfigError, available_profiles, load_config
from .db import open_db
from .ingest import ingest
from .moves import extract_moves
from .status import status_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chess_coach")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument(
        "--profile", default=None,
        help="player profile to act on (default: the 'default' profile). "
             "Each profile has its own database and its own raw PGN directory.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ingest_cmd = sub.add_parser("ingest", help="sync games from the platform")
    ingest_cmd.add_argument(
        "--limit-archives", type=int, default=None,
        help="only process the N most recent monthly archives",
    )
    ingest_cmd.add_argument(
        "--max-games", type=int, default=None,
        help="stop after importing N new games (sampling / smoke tests)",
    )

    extract_cmd = sub.add_parser(
        "extract-moves",
        help="rebuild game_moves from the raw PGNs on disk (no API calls)",
    )
    extract_cmd.add_argument(
        "--force", action="store_true", help="re-extract games that already have moves",
    )
    extract_cmd.add_argument("--game-id", type=int, default=None, help="one game by DB id")

    analyze_cmd = sub.add_parser("analyze", help="run Stockfish analysis over imported games")
    analyze_cmd.add_argument(
        "--depth", type=int, default=None,
        help="search depth (default: stockfish.historical_depth from config)",
    )
    analyze_cmd.add_argument("--limit", type=int, default=None, help="analyze at most N games")
    analyze_cmd.add_argument("--game-id", type=int, default=None, help="analyze one game by DB id")
    analyze_cmd.add_argument(
        "--retry-failed", action="store_true", help="also re-attempt games marked failed",
    )
    analyze_cmd.add_argument(
        "--force", action="store_true", help="re-analyze games already completed",
    )
    analyze_cmd.add_argument(
        "--notes", default=None, help="free-text note stored on the analysis run",
    )

    update_cmd = sub.add_parser(
        "update",
        help="routine cycle: ingest -> extract-moves -> analyze (thin orchestrator)",
    )
    update_cmd.add_argument(
        "--depth", type=int, default=None, help="analysis depth (default from config)"
    )
    update_cmd.add_argument("--limit-archives", type=int, default=None)
    update_cmd.add_argument("--max-games", type=int, default=None)
    update_cmd.add_argument(
        "--retry-failed", action="store_true", help="also re-attempt games marked failed",
    )
    update_cmd.add_argument(
        "--notes", default=None, help="free-text note stored on the analysis run",
    )

    report_cmd = sub.add_parser("report", help="descriptive aggregation (no scoring)")
    report_cmd.add_argument(
        "--opening-plies", type=int, default=8,
        help="prefix length used to group opening families (default 8)",
    )

    sub.add_parser("status", help="show ingestion / analysis counters")
    sub.add_parser("profiles", help="list configured player profiles")
    return parser


def list_profiles(config_path: str | None) -> int:
    """Show every configured player and where their data lives."""
    for name in available_profiles():
        try:
            cfg = load_config(config_path, profile=name)
        except ConfigError as exc:
            print(f"  {name:<12} : NOT USABLE - {exc}")
            continue
        exists = "present" if cfg.database.exists() else "not created yet"
        print(f"  {name:<12} : {cfg.username} | {cfg.database} ({exists})")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "profiles":
        return list_profiles(args.config)

    try:
        config = load_config(args.config, profile=args.profile)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    print(f"profile: {config.profile} ({config.username}) | db: {config.database}")

    if args.command == "ingest":
        started = time.monotonic()
        report = ingest(
            config,
            limit_archives=args.limit_archives,
            max_games=args.max_games,
        )
        print(report.summary())
        print(f"elapsed: {time.monotonic() - started:.1f}s")
        if report.errors:
            print(f"\nfailures ({len(report.errors)}):")
            for line in report.errors[:20]:
                print(f"  {line}")
        return 0

    if args.command == "extract-moves":
        started = time.monotonic()
        report = extract_moves(config, force=args.force, game_id=args.game_id)
        print(report.summary())
        print(f"elapsed: {time.monotonic() - started:.1f}s")
        if report.errors:
            print(f"\nfailures ({len(report.errors)}):")
            for line in report.errors[:20]:
                print(f"  {line}")
        return 1 if report.games_failed else 0

    if args.command == "analyze":
        report = analyze(
            config,
            depth=args.depth,
            limit=args.limit,
            game_id=args.game_id,
            retry_failed=args.retry_failed,
            force=args.force,
            notes=args.notes,
        )
        print(report.summary())
        if report.errors:
            print(f"\nfailures ({len(report.errors)}):")
            for line in report.errors[:20]:
                print(f"  {line}")
        return 1 if report.games_failed else 0

    if args.command == "update":
        from .update import update

        report = update(
            config,
            depth=args.depth,
            limit_archives=args.limit_archives,
            max_games=args.max_games,
            retry_failed=args.retry_failed,
            notes=args.notes,
        )
        print()
        print(report.summary())
        if report.errors:
            print(f"\nitem failures ({len(report.errors)}):")
            for line in report.errors[:20]:
                print(f"  {line}")
        if report.aborted:
            return 2
        return 1 if report.total_failures else 0

    if args.command == "report":
        from .aggregate import report as build_report

        conn = open_db(config)
        try:
            print(build_report(conn, plies=args.opening_plies))
        finally:
            conn.close()
        return 0

    if args.command == "status":
        print(status_report(config))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
