"""`update`: one command for the routine cycle.

    ingest  ->  extract-moves  ->  analyze

This is PLUMBING, not a fourth layer. It contains no analysis logic, no
heuristics and no intelligence of any kind: it calls the three existing entry
points in order, propagates their failures, and adds up their reports. Every
guarantee the individual stages provide - idempotency, resumability, and never
destroying data - is inherited unchanged, because nothing here reimplements
them.

STAGE FAILURES vs ITEM FAILURES
-------------------------------
Two different things can go wrong and they are treated differently on purpose:

* A **stage failure** is an exception that stops a stage doing its job at all -
  the network is down, the archive list is unreachable, the database is locked.
  The pipeline aborts immediately and the later stages do not run. Analysing
  against a half-finished ingest would silently produce a partial picture.

* An **item failure** is one bad game inside a stage that otherwise worked. Both
  `ingest` and `extract_moves` already isolate these deliberately, so that a
  single malformed PGN cannot block every other game, and `analyze` marks such a
  game `failed` for later retry. Aborting the pipeline on those would change
  existing behaviour and destroy resumability, so they are counted, surfaced in
  the summary, and reflected in the exit code - but they do not stop the run.

Neither kind is ever swallowed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from .analysis import analyze
from .config import Config
from .db import open_db
from .ingest import ingest
from .moves import extract_moves

STAGES = ("ingest", "extract-moves", "analyze")


@dataclass
class UpdateReport:
    """Roll-up of one cycle. Every number comes from a stage's own report."""

    # Stage that aborted the pipeline, if any.
    failed_stage: str | None = None
    error: str | None = None
    stages_run: list[str] = field(default_factory=list)

    games_new: int = 0
    games_pending_analysis: int = 0
    games_analyzed: int = 0
    moves_extracted: int = 0
    moves_analyzed: int = 0

    ingest_failures: int = 0
    extract_failures: int = 0
    analysis_failures: int = 0

    run_id: int | None = None
    nothing_to_do: bool = False
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def total_failures(self) -> int:
        return self.ingest_failures + self.extract_failures + self.analysis_failures

    @property
    def aborted(self) -> bool:
        return self.failed_stage is not None

    def summary(self) -> str:
        lines = ["stages run     : " + (", ".join(self.stages_run) or "none")]
        if self.aborted:
            lines.append(f"ABORTED at     : {self.failed_stage}")
            lines.append(f"  reason       : {self.error}")
            lines.append("  later stages did not run")
        elif self.nothing_to_do:
            lines.append("nothing to do  : no new games, nothing pending analysis")

        lines += [
            f"new games      : {self.games_new}",
            f"pending before : {self.games_pending_analysis}",
            f"games analyzed : {self.games_analyzed}",
            f"moves extracted: {self.moves_extracted}",
            f"moves analyzed : {self.moves_analyzed}",
            f"failures       : {self.total_failures}"
            + (
                f" (ingest {self.ingest_failures}, extract {self.extract_failures}, "
                f"analysis {self.analysis_failures})"
                if self.total_failures
                else ""
            ),
        ]
        if self.run_id is not None:
            lines.append(f"analysis run   : {self.run_id}")
        lines.append(f"total time     : {self.seconds:.1f}s")
        return "\n".join(lines)


def _pending_analysis(config: Config) -> int:
    conn = open_db(config)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM games WHERE analysis_status IN ('pending', 'running')"
        ).fetchone()[0]
    finally:
        conn.close()


def update(
    config: Config,
    depth: int | None = None,
    limit_archives: int | None = None,
    max_games: int | None = None,
    retry_failed: bool = False,
    notes: str | None = None,
    log: Callable[[str], None] = print,
    client=None,
    engine_factory=None,
) -> UpdateReport:
    """Run the routine cycle. Returns a roll-up; never raises for stage errors.

    `client` and `engine_factory` are injection points for tests only; they are
    passed straight through to the stages that already accept them.
    """
    report = UpdateReport()
    started = time.monotonic()

    try:
        # -- 1. ingest ----------------------------------------------------
        log("[1/3] ingest")
        try:
            ingest_report = ingest(
                config,
                client=client,
                limit_archives=limit_archives,
                max_games=max_games,
                log=lambda line: log(f"      {line}"),
            )
        except Exception as exc:  # noqa: BLE001 - a dead stage must stop the run
            report.failed_stage = "ingest"
            report.error = f"{type(exc).__name__}: {exc}"
            return report
        report.stages_run.append("ingest")
        report.games_new = ingest_report.games_imported
        report.ingest_failures = ingest_report.games_failed
        report.errors += ingest_report.errors
        log(f"      {ingest_report.games_imported} new game(s), "
            f"{ingest_report.games_failed} failed")

        # -- 2. extract-moves ---------------------------------------------
        log("[2/3] extract-moves")
        try:
            extract_report = extract_moves(
                config, log=lambda line: log(f"      {line}")
            )
        except Exception as exc:  # noqa: BLE001 - do not analyze on a broken raw layer
            report.failed_stage = "extract-moves"
            report.error = f"{type(exc).__name__}: {exc}"
            return report
        report.stages_run.append("extract-moves")
        report.moves_extracted = extract_report.moves_written
        report.extract_failures = extract_report.games_failed
        report.errors += extract_report.errors
        log(f"      {extract_report.moves_written} move(s) from "
            f"{extract_report.games_processed} game(s), "
            f"{extract_report.games_failed} failed")

        # -- 3. analyze ----------------------------------------------------
        report.games_pending_analysis = _pending_analysis(config)
        if report.games_pending_analysis == 0 and not retry_failed:
            # Nothing new and nothing left over: stop cleanly rather than open
            # an empty analysis run.
            report.nothing_to_do = report.games_new == 0
            log("[3/3] analyze: nothing pending")
            return report

        log("[3/3] analyze")
        try:
            analysis_report = analyze(
                config,
                depth=depth,
                retry_failed=retry_failed,
                notes=notes,
                log=lambda line: log(f"      {line}"),
                engine_factory=engine_factory,
            )
        except Exception as exc:  # noqa: BLE001 - engine death is a stage failure
            report.failed_stage = "analyze"
            report.error = f"{type(exc).__name__}: {exc}"
            return report
        report.stages_run.append("analyze")
        report.run_id = analysis_report.run_id
        report.games_analyzed = analysis_report.games_analyzed
        report.moves_analyzed = analysis_report.moves_analyzed
        report.analysis_failures = analysis_report.games_failed
        report.errors += analysis_report.errors
        return report
    finally:
        report.seconds = time.monotonic() - started
