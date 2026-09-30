"""Descriptive aggregation over the canonical analysis run.

STRICTLY DESCRIPTIVE. This module counts and averages. It does not score, rank,
prioritize, diagnose or recommend. There is no weakness score, no priority
formula, no mastery model and no opening recommendation here, and none may be
added: those are interpretations, and per ARCHITECTURE.md section 2 they require
recurrence and a sample size this dataset does not have.

Everything reads the `current_move_analysis` view, so a game analysed under
several runs contributes exactly one set of rows - whichever run
`games.analysis_run_id` names as canonical.

All breakdowns are restricted to the user's OWN moves (`color = player_color`)
unless stated otherwise. Opponent moves are in the database and are useful
context, but they are not this player's decisions.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, field

from .openings import DEFAULT_KEY_PLIES, opening_families, opening_key_for_game

# Buckets of remaining clock, in seconds, for time-pressure breakdowns. These
# are reporting buckets only: no claim is made that any of them is a threshold
# at which play changes. That would be an interpretation.
CLOCK_BUCKETS = (
    ("under 10s", 0, 10_000),
    ("10-30s", 10_000, 30_000),
    ("30-60s", 30_000, 60_000),
    ("1-2min", 60_000, 120_000),
    ("over 2min", 120_000, None),
)

OWN_MOVES = """
    FROM current_move_analysis c
    JOIN games g ON g.id = c.game_id
   WHERE g.player_color IS NOT NULL AND c.color = g.player_color
"""


@dataclass
class Section:
    title: str
    rows: list[tuple[str, str]] = field(default_factory=list)
    note: str | None = None

    def render(self) -> str:
        width = max((len(k) for k, _ in self.rows), default=0)
        body = "\n".join(f"  {k:<{width}} : {v}" for k, v in self.rows)
        out = f"{self.title}\n{body}" if body else f"{self.title}\n  (no data)"
        if self.note:
            out += f"\n  -- {self.note}"
        return out


def _fmt_pct(part: int, whole: int) -> str:
    return f"{part} ({100.0 * part / whole:.1f}%)" if whole else f"{part} (n/a)"


def _acpl(conn: sqlite3.Connection, where: str = "", params: tuple = ()) -> tuple[float, float, int]:
    rows = [
        r[0]
        for r in conn.execute(
            f"SELECT c.evaluation_loss {OWN_MOVES} {where}", params
        )
    ]
    if not rows:
        return (0.0, 0.0, 0)
    return (statistics.mean(rows), statistics.median(rows), len(rows))


def dataset_section(conn: sqlite3.Connection) -> Section:
    s = Section("DATASET")
    games = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    analyzed = conn.execute(
        "SELECT COUNT(*) FROM games WHERE analysis_status = 'completed'"
    ).fetchone()[0]
    raw_moves = conn.execute("SELECT COUNT(*) FROM game_moves").fetchone()[0]
    obs = conn.execute("SELECT COUNT(*) FROM move_analysis").fetchone()[0]
    canonical = conn.execute("SELECT COUNT(*) FROM current_move_analysis").fetchone()[0]
    own = conn.execute(f"SELECT COUNT(*) {OWN_MOVES}").fetchone()[0]
    runs = conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0]

    s.rows = [
        ("games imported", str(games)),
        ("games analyzed", str(analyzed)),
        ("raw move rows", str(raw_moves)),
        ("observation rows (all runs)", str(obs)),
        ("observation rows (canonical)", str(canonical)),
        ("the user's own moves", str(own)),
        ("analysis runs recorded", str(runs)),
    ]
    s.note = "counts only; nothing here is a judgement about the player"
    return s


def runs_section(conn: sqlite3.Connection) -> Section:
    s = Section("ANALYSIS RUNS")
    for r in conn.execute(
        """SELECT id, engine_name, engine_version, depth, multipv, status,
                  moves_analyzed, rules_version, eval_cap_cp
             FROM analysis_runs ORDER BY id"""
    ):
        used = conn.execute(
            "SELECT COUNT(*) FROM games WHERE analysis_run_id = ?", (r["id"],)
        ).fetchone()[0]
        s.rows.append((
            f"run {r['id']}",
            f"{r['engine_name']} {r['engine_version']} | depth {r['depth']} | "
            f"multipv {r['multipv']} | rules {r['rules_version']} | "
            f"cap {r['eval_cap_cp']} | {r['status']} | "
            f"{r['moves_analyzed']} moves | canonical for {used} game(s)",
        ))
    return s


def classification_section(conn: sqlite3.Connection) -> Section:
    s = Section("CLASSIFICATION (the user's own moves)")
    total = conn.execute(f"SELECT COUNT(*) {OWN_MOVES}").fetchone()[0]
    for label in ("BEST", "GOOD", "INACCURACY", "MISTAKE", "BLUNDER"):
        n = conn.execute(
            f"SELECT COUNT(*) {OWN_MOVES} AND c.classification = ?", (label,)
        ).fetchone()[0]
        s.rows.append((label, _fmt_pct(n, total)))

    mean, median, n = _acpl(conn)
    s.rows.append(("mean loss (cp)", f"{mean:.1f}"))
    s.rows.append(("median loss (cp)", f"{median:.1f}"))
    s.note = (
        "an evaluation-loss event is an observation, not a diagnosis; "
        "a BLUNDER here is not evidence of a tactical weakness"
    )
    return s


def loss_distribution_section(conn: sqlite3.Connection) -> Section:
    s = Section("EVALUATION LOSS DISTRIBUTION (own moves, centipawns)")
    buckets = [("0", 0, 1), ("1-24", 1, 25), ("25-49", 25, 50), ("50-99", 50, 100),
               ("100-199", 100, 200), ("200-299", 200, 300), ("300-599", 300, 600),
               ("600+", 600, None)]
    total = conn.execute(f"SELECT COUNT(*) {OWN_MOVES}").fetchone()[0]
    for label, low, high in buckets:
        if high is None:
            q = f"SELECT COUNT(*) {OWN_MOVES} AND c.evaluation_loss >= ?"
            n = conn.execute(q, (low,)).fetchone()[0]
        else:
            q = f"SELECT COUNT(*) {OWN_MOVES} AND c.evaluation_loss >= ? AND c.evaluation_loss < ?"
            n = conn.execute(q, (low, high)).fetchone()[0]
        s.rows.append((label, _fmt_pct(n, total)))
    return s


def _breakdown(
    conn: sqlite3.Connection, title: str, column: str, note: str | None = None
) -> Section:
    s = Section(title)
    for row in conn.execute(
        f"""SELECT {column} AS bucket, COUNT(*) n,
                   AVG(c.evaluation_loss) avg_loss,
                   SUM(CASE WHEN c.classification = 'BLUNDER' THEN 1 ELSE 0 END) blunders
            {OWN_MOVES}
            GROUP BY bucket ORDER BY n DESC"""
    ):
        s.rows.append((
            str(row["bucket"]),
            f"n={row['n']:<5} mean loss {row['avg_loss']:6.1f} cp   "
            f"blunders {row['blunders']}",
        ))
    s.note = note
    return s


def clock_section(conn: sqlite3.Connection) -> Section:
    s = Section("PERFORMANCE BY TIME REMAINING (own moves)")
    missing = conn.execute(
        f"SELECT COUNT(*) {OWN_MOVES} AND c.clock_before_ms IS NULL"
    ).fetchone()[0]
    for label, low, high in CLOCK_BUCKETS:
        if high is None:
            q = f"""SELECT COUNT(*), AVG(c.evaluation_loss),
                           SUM(CASE WHEN c.classification='BLUNDER' THEN 1 ELSE 0 END)
                    {OWN_MOVES} AND c.clock_before_ms >= ?"""
            n, avg, bl = conn.execute(q, (low,)).fetchone()
        else:
            q = f"""SELECT COUNT(*), AVG(c.evaluation_loss),
                           SUM(CASE WHEN c.classification='BLUNDER' THEN 1 ELSE 0 END)
                    {OWN_MOVES} AND c.clock_before_ms >= ? AND c.clock_before_ms < ?"""
            n, avg, bl = conn.execute(q, (low, high)).fetchone()
        s.rows.append((
            label,
            f"n={n:<5} mean loss {(avg or 0):6.1f} cp   blunders {bl or 0}",
        ))
    if missing:
        s.rows.append(("no clock data", f"n={missing}"))
    s.note = (
        "reporting buckets, not thresholds; small n here cannot support any "
        "claim about time pressure"
    )
    return s


def openings_section(conn: sqlite3.Connection, plies: int = DEFAULT_KEY_PLIES) -> Section:
    s = Section(f"OPENING FAMILIES (move-derived, first {plies} plies)")
    families = opening_families(conn, plies)
    for family in families:
        ids = [
            r[0] for r in conn.execute(
                "SELECT id FROM games ORDER BY id"
            )
        ]
        members = [
            gid for gid in ids
            if (k := opening_key_for_game(conn, gid, plies)) and k.key == family.key
        ]
        placeholders = ",".join("?" for _ in members)
        row = conn.execute(
            f"""SELECT COUNT(*) n, AVG(c.evaluation_loss) avg_loss
                {OWN_MOVES} AND c.game_id IN ({placeholders})""",
            tuple(members),
        ).fetchone() if members else None
        ecos = sorted({e for e in family.ecos if e}) or ["-"]
        s.rows.append((
            family.key_san,
            f"games={family.games}  own moves={row['n'] if row else 0}  "
            f"mean loss {(row['avg_loss'] or 0) if row else 0:.1f} cp  "
            f"ECO {'/'.join(ecos)}"
            + ("" if family.eco_agrees else "  [ECO disagrees within family]"),
        ))
    s.note = (
        "grouping only. No opening is recommended, ranked or judged: the "
        f"largest family here holds {max((f.games for f in families), default=0)} "
        "game(s)"
    )
    return s


def games_section(conn: sqlite3.Connection) -> Section:
    s = Section("BY GAME (own moves)")
    for row in conn.execute(
        """SELECT c.game_id, g.player_color, g.player_result, g.time_class,
                  COUNT(*) n, AVG(c.evaluation_loss) acpl,
                  SUM(CASE WHEN c.classification='BLUNDER' THEN 1 ELSE 0 END) bl,
                  SUM(CASE WHEN c.classification='MISTAKE' THEN 1 ELSE 0 END) mi,
                  SUM(CASE WHEN c.classification='INACCURACY' THEN 1 ELSE 0 END) ina,
                  g.end_time_utc
           FROM current_move_analysis c JOIN games g ON g.id = c.game_id
           WHERE g.player_color IS NOT NULL AND c.color = g.player_color
           GROUP BY c.game_id ORDER BY g.end_time_utc"""
    ):
        # Any of these can be NULL for a game whose headers were incomplete;
        # a missing label must not take the whole report down.
        date = (row["end_time_utc"] or "?")[:10]
        color = row["player_color"] or "?"
        result = row["player_result"] or "?"
        time_class = row["time_class"] or "?"
        s.rows.append((
            f"game {row['game_id']} ({date})",
            f"{color:<5} {result:<4} "
            f"{time_class:<6} moves={row['n']:<3} ACPL {row['acpl']:6.1f}  "
            f"blunder {row['bl']} mistake {row['mi']} inacc {row['ina']}",
        ))
    return s


def report(conn: sqlite3.Connection, plies: int = DEFAULT_KEY_PLIES) -> str:
    sections = [
        dataset_section(conn),
        runs_section(conn),
        classification_section(conn),
        loss_distribution_section(conn),
        _breakdown(conn, "PERFORMANCE BY PHASE (own moves)", "c.phase"),
        _breakdown(conn, "PERFORMANCE BY COLOR (own moves)", "c.color"),
        _breakdown(conn, "PERFORMANCE BY TIME CONTROL (own moves)", "g.time_control"),
        _breakdown(conn, "PERFORMANCE BY RESULT (own moves)", "g.player_result"),
        clock_section(conn),
        openings_section(conn, plies),
        games_section(conn),
    ]
    body = "\n\n".join(section.render() for section in sections)
    footer = (
        "\n\nNOTE: every figure above is a descriptive count over "
        f"{conn.execute(f'SELECT COUNT(*) {OWN_MOVES}').fetchone()[0]} of the "
        "user's own moves. No weakness, priority, mastery or opening "
        "recommendation is derived from them, and none should be: that requires "
        "recurrence and a sample size this dataset does not have."
    )
    return body + footer
