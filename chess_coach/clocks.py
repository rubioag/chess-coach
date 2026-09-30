"""Clock extraction from raw PGN move comments.

chess.com writes a `[%clk H:MM:SS.f]` comment on every move. That reading is the
clock the mover had left *after* the move was made, with the increment already
credited. The number a coach actually cares about is the one *before* the move -
how much time the player had while deciding - so it is derived here:

    clock_before(ply) = clock_after(ply - 2)      same player's previous move
    clock_before(1)   = base time                 White's first move
    clock_before(2)   = base time                 Black's first move

    time_spent(ply)   = clock_before - clock_after + increment

The increment is added back because `clock_after` already includes it: a player
who takes 3 s with a 5 s increment ends up with 2 s MORE than they started, and
without the correction their time spent would come out negative.

No API call is involved. Everything here comes from the immutable PGN already on
disk, which is exactly why storing the raw file unmodified was worth its cost.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import chess.pgn

# TimeControl header forms seen in practice:
#   "300"      300 s, no increment
#   "300+5"    300 s plus 5 s per move
#   "1/86400"  correspondence, one move per 86400 s
#   "-"        untimed
TIME_CONTROL_RE = re.compile(r"^(\d+)(?:\+(\d+))?$")
CORRESPONDENCE_RE = re.compile(r"^1/(\d+)$")


@dataclass(frozen=True)
class TimeControl:
    base_ms: int | None
    increment_ms: int | None

    @property
    def known(self) -> bool:
        return self.base_ms is not None


def parse_time_control(value: str | None) -> TimeControl:
    """Parse a PGN TimeControl header into milliseconds.

    Unrecognized or absent values yield an unknown TimeControl rather than an
    error: a game with a time control we cannot parse must still be analysed,
    it simply contributes no time-pressure data.
    """
    if not value:
        return TimeControl(None, None)
    text = value.strip()

    match = TIME_CONTROL_RE.match(text)
    if match:
        base = int(match.group(1)) * 1000
        increment = int(match.group(2) or 0) * 1000
        return TimeControl(base, increment)

    match = CORRESPONDENCE_RE.match(text)
    if match:
        # Correspondence: the per-move allowance behaves as the starting budget.
        return TimeControl(int(match.group(1)) * 1000, 0)

    return TimeControl(None, None)


def clock_readings(game: chess.pgn.Game) -> list[int | None]:
    """Per-ply `[%clk]` readings in milliseconds, index 0 = ply 1.

    Returns None for a ply that carries no clock comment, so partial coverage is
    visible rather than silently filled in.
    """
    readings: list[int | None] = []
    node = game
    while node.variations:
        node = node.variations[0]
        seconds = node.clock()
        readings.append(None if seconds is None else int(round(seconds * 1000)))
    return readings


def derive_clock_columns(
    readings: list[int | None], time_control: TimeControl
) -> list[tuple[int | None, int | None, int | None]]:
    """Turn raw readings into (clock_before, clock_after, time_spent) per ply.

    Any component that cannot be derived honestly is left as None: an unparsed
    time control means no `clock_before` on the first two plies and therefore no
    `time_spent` for them either.
    """
    out: list[tuple[int | None, int | None, int | None]] = []
    for index, after in enumerate(readings):
        ply = index + 1
        if ply <= 2:
            before = time_control.base_ms
        else:
            before = readings[index - 2]

        spent: int | None = None
        if before is not None and after is not None and time_control.increment_ms is not None:
            spent = before - after + time_control.increment_ms
            # A negative value means the reading pair is inconsistent (clock
            # adjustment, disconnect, or a time control that is not what the
            # header claims). Record nothing rather than a nonsense number.
            if spent < 0:
                spent = None

        out.append((before, after, spent))
    return out
