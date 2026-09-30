"""Opening family identity, derived from the moves actually played.

DESCRIPTIVE ONLY. This module groups games; it does not recommend, rank or
judge openings, and it must not grow a repertoire engine. See ARCHITECTURE.md
section 4 for why repertoire decisions need far more data than exists today.

WHY NOT THE PLATFORM'S LABEL
----------------------------
chess.com supplies `ECO` and an `ECOUrl` whose slug reads like an opening name
(`Ruy-Lopez-Opening-Morphy-Defense-Exchange-Lutikov-Variation`). That slug is a
vendor-controlled string: it can change without notice, it does not exist on
other platforms, and in this dataset the actual `Opening` header is absent in
every single game. Grouping on it would make our analysis hostage to a third
party's naming.

Instead a family key is built from the move sequence itself, which is a fact we
own. ECO is kept alongside as an independent cross-check: if two games share a
move-prefix key but disagree on ECO, that disagreement is visible rather than
hidden.

DEPTH IS A QUERY PARAMETER, NOT A STORED CHOICE
-----------------------------------------------
Nothing is denormalized onto `games`. `game_moves` holds every ply, so a key at
any prefix length is computable at any time. A future component that wants
4-ply families or 16-ply families does not need a migration - it passes a
different `plies` argument.

KNOWN LIMITATION
----------------
A prefix key does not merge transpositions: 1.d4 Nf6 2.c4 e6 and 1.c4 e6 2.d4
Nf6 reach the same position under different keys. Handling that needs a
position-based key (or a real ECO book) and is deliberately left for later -
recording the limitation is more honest than pretending a prefix is a taxonomy.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

DEFAULT_KEY_PLIES = 8


@dataclass(frozen=True)
class OpeningKey:
    """A move-derived family identity for one game."""

    game_id: int
    key: str            # space-separated UCI prefix; the grouping identity
    key_san: str        # same prefix in SAN, for reading
    root: str           # first two plies in SAN, e.g. "e4 e5" - coarse bucket
    plies_used: int     # may be < requested for very short games
    eco: str | None     # platform's opinion, cross-check only
    eco_url: str | None


def opening_key_for_game(
    conn: sqlite3.Connection, game_id: int, plies: int = DEFAULT_KEY_PLIES
) -> OpeningKey | None:
    """Build the move-derived key for one game, or None if it has no raw moves."""
    rows = conn.execute(
        "SELECT ply, uci, san FROM game_moves WHERE game_id = ? AND ply <= ? "
        "ORDER BY ply",
        (game_id, plies),
    ).fetchall()
    if not rows:
        return None

    meta = conn.execute(
        "SELECT eco, eco_url FROM games WHERE id = ?", (game_id,)
    ).fetchone()

    return OpeningKey(
        game_id=game_id,
        key=" ".join(r["uci"] for r in rows),
        key_san=" ".join(r["san"] for r in rows),
        root=" ".join(r["san"] for r in rows[:2]),
        plies_used=len(rows),
        eco=meta["eco"] if meta else None,
        eco_url=meta["eco_url"] if meta else None,
    )


def opening_keys(
    conn: sqlite3.Connection, plies: int = DEFAULT_KEY_PLIES
) -> list[OpeningKey]:
    ids = [r[0] for r in conn.execute("SELECT id FROM games ORDER BY id")]
    keys = [opening_key_for_game(conn, gid, plies) for gid in ids]
    return [k for k in keys if k is not None]


@dataclass
class OpeningFamily:
    """Games sharing a move-prefix key, with the ECO cross-check result."""

    key: str
    key_san: str
    root: str
    games: int
    ecos: list[str]

    @property
    def eco_agrees(self) -> bool:
        """True when every game in the family carries the same ECO code.

        A False here is information, not a bug: it means the platform's
        classification and the actual move prefix disagree about where the
        boundary of this opening lies.
        """
        known = {e for e in self.ecos if e}
        return len(known) <= 1


def opening_families(
    conn: sqlite3.Connection, plies: int = DEFAULT_KEY_PLIES
) -> list[OpeningFamily]:
    """Group games by move-derived key. Descriptive; no ranking of any kind."""
    grouped: dict[str, OpeningFamily] = {}
    for item in opening_keys(conn, plies):
        family = grouped.get(item.key)
        if family is None:
            family = OpeningFamily(
                key=item.key, key_san=item.key_san, root=item.root, games=0, ecos=[]
            )
            grouped[item.key] = family
        family.games += 1
        family.ecos.append(item.eco or "")
    return sorted(grouped.values(), key=lambda f: (-f.games, f.key_san))


def coverage(conn: sqlite3.Connection, plies: int = DEFAULT_KEY_PLIES) -> dict[str, int]:
    """How much of the dataset can be grouped at all."""
    total = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    keyed = len(opening_keys(conn, plies))
    families = opening_families(conn, plies)
    return {
        "games": total,
        "games_with_key": keyed,
        "families": len(families),
        "largest_family": max((f.games for f in families), default=0),
        "families_where_eco_disagrees": sum(1 for f in families if not f.eco_agrees),
    }
