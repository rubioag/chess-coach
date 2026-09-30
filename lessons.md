# Lessons — Chess Coach

Running log of non-obvious facts discovered while building. Append, don't rewrite.

## chess.com PubAPI
- No `since` / date filter on `/pub/player/{u}/games/{yyyy}/{mm}`. Incremental
  sync must be driven by the archive list + `ETag` / `If-None-Match`, not a
  timestamp cursor.
- Parallel requests trigger `429` and abnormal-activity blocks. All requests are
  serial, with a configurable delay between them.
- chess.com asks for a `User-Agent` carrying tool name, username and contact.
  Without it, requests can be blocked as abnormal activity.
- The game `url` field is the only stable identifier. The internal id format has
  already changed once (integer → uuid), so the code never parses it — the URL
  is stored verbatim as `external_game_id`, and only the last path segment
  (sanitized) is used as a filename.
- Platform metadata that never appears in PGN headers (`time_class`, `rated`,
  `end_time`, opening url) comes from the JSON game object, not the PGN.

## PGN / data modelling
- chess.com PGNs frequently lack `ECO` / `Opening` / `Variation`. Missing
  headers must not fail the row. Two distinct states are preserved:
  `NULL` = header absent, `'Unknown'` = header present but unclassified.
  Collapsing them would hide how much opening data is actually missing.
- Ratings/dates/usernames use the same tolerant reader but collapse `'Unknown'`
  back to `NULL` — an unknown marker carries no analytical meaning there.

## Sync correctness
- ETag only saves bandwidth. Dedup by `external_game_id` runs unconditionally,
  because a `200` can replay games already imported.
- The current month is never marked `complete` — more games can still land in it.
- A month with any parse failure stays `pending`, so failures are retried rather
  than silently swallowed.
- Raw PGN files are write-once: an existing file is never rewritten, which keeps
  `data/games/` an immutable source of truth that later phases can reprocess
  without touching the API.

## Environment
- Stockfish is NOT on PATH on this machine (2026-09-03). Phase 2 needs the
  binary installed and its path added to config before analysis can run.
- Python 3.12.10, python-chess 1.11.2.

## Bugs found by running against real data (2026-09-03)
- **ETag cached on an incomplete month silently loses games.** The smoke run
  used `--max-games 5` on a 6-game month, then stored that month's ETag. The
  next full run got `304`, skipped parsing, and the 6th game was never imported
  even though the month was still `pending`. Fix: the ETag is persisted only
  when the month was *fully processed* (no `--max-games` truncation, zero parse
  failures); otherwise it is written as `NULL` to force a `200` next time.
  Regression tests: `test_truncated_month_does_not_cache_etag`,
  `test_failed_month_does_not_cache_etag`, `test_fully_processed_month_caches_etag`.
  Lesson: `status='pending'` alone is not enough — every skip mechanism has to
  agree with it.
- **`relative_to(PROJECT_ROOT)` crashes** when `raw_games_dir` is outside the
  repo (e.g. a pytest tmp_path). Stored path now falls back to absolute.

## Observed data shape for this account (2026-09-03)
- chess.com PGNs here carry `ECO` + `ECOUrl` but **no `Opening` header at all**
  — 7/7 games have `eco` set and `opening` NULL. So opening names must be
  derived from `ECOUrl` (or an ECO lookup table), not from the PGN. This is
  exactly the gap the NULL-vs-`'Unknown'` split was built to measure.
- PGN carries a `CurrentPosition` header. Our independently-computed
  `final_fen` matched it exactly on spot-check — free validation of the
  move-replay path.
- Move text contains `{[%clk ...]}` comments. python-chess ignores them for
  move replay; they remain available in the raw PGN for later time-usage work.
- The account is small: 2 archives, 7 games total. Stockfish benchmarking will
  need a synthetic/larger sample to produce a meaningful throughput number.

## Stockfish phase (2026-09-03)
- Installed via `winget install Stockfish.Stockfish` -> **Stockfish 18**, AVX2
  build. The winget "Links" shim (`.../WinGet/Links/stockfish.exe`) is an app
  execution alias; the real binary under `WinGet/Packages/...` is what goes in
  config, so nothing depends on shim behaviour with piped stdin.
- `engine.analyse(..., multipv=N)` returns a **list** of InfoDict, not a single
  dict, even when N == 1. Indexing it like a dict raises
  `TypeError: list indices must be integers`. Worse, an exception between
  `popen_uci` and `quit()` leaves an orphan engine process holding the pipe and
  the shell appears to hang. The wrapper is a context manager for this reason.
- python-chess mate scoring: `Mate(+k).score(mate_score=M)` = `M - k`,
  `Mate(-k)` = `-(M - k)`. Both `MateGiven` (+M) and `Mate(-0)` (-M) report
  `.mate() == 0`, so mate distance alone cannot tell "I mated" from "I am
  mated" - the sign of the cp value disambiguates. Both are stored.
- Each position is searched **once**. The search of the position after move i
  is reused, sign-flipped, as move i's `evaluation_after` and directly as move
  i+1's `evaluation_before`. Searching twice would double cost and, since a
  fixed-depth search is not perfectly stable, could return two different numbers
  for the same position.
- Two independent fixed-depth searches of adjacent positions disagree by a few
  centipawns (measured: 429 vs 422 re-running the same position with a cold
  hash). So `evaluation_loss` can come out slightly negative on a good move.
  It is floored at 0, and BEST is decided by move equality, never by loss == 0.
- `evaluation_loss` ranges over `[0, 2 * eval_cap_cp]`, not `[0, eval_cap_cp]`:
  a move can fall from +1000 (clamped) to -1000 (clamped). Observed real max:
  1414, from +4.14 straight into a mate-in-1.
- Engine refuses to search a position with no legal moves, so checkmate and
  stalemate are answered from the board. Games ending by resignation or timeout
  are NOT terminal - the position still has legal moves and is analysed
  normally, which is right: we grade the moves played, not why the clock stopped.

## Benchmark (measured, not estimated)
- Stockfish 18, depth 14, MultiPV 1, Threads 1, Hash 128 MB, 8-core machine.
- 7 games / 380 moves / 387 position searches in **33.6 s** => 4.8 s per game,
  ~0.088 s per move. Re-running one game with a warm hash took 2.8 s vs 3.7 s
  cold, so per-game timings vary ~30% run to run.
- At this rate a 1,000-game backfill at depth 14 is roughly 80 minutes
  single-threaded. Depth 18 was not benchmarked.

## Learning/coaching design context absorbed (2026-09-03)
Full design constraints live in `ARCHITECTURE.md`. Facts discovered while
reconciling them with the current build:

- **The account's entire history is 7 games** (2 monthly archives). Ingestion is
  complete and idempotent, so the data bottleneck is games *played*, not games
  *fetched*. More data means waiting, adding lichess, or another account. This
  reframes "get more data" from an engineering task into a scheduling one.
- **Per-move clocks are in 7/7 raw PGNs** (`[%clk ...]`, exposed by
  python-chess `node.clock()`) but were never stored in `moves`. Recoverable by
  re-parsing from disk - no API call, no engine run. Concrete proof that the
  immutable raw-PGN decision was worth its cost.
- **`moves` has `UNIQUE(game_id, ply)`**, so re-analysis replaces rows and no
  engine version, MultiPV, eval cap or threshold set is recorded anywhere. Seen
  live: the DB briefly held `{depth 14: 375 rows, depth 18: 5 rows}` with no
  marker distinguishing them. Provenance must be added while the table is still
  380 rows, not after it is thousands.
- **Depth 14 -> 18 re-analysis verified on real data**: no schema change, no row
  duplication, `games.analysis_depth` and `moves.analysis_depth` both updated.
  Restored to depth 14 afterwards to keep the dataset homogeneous.
- **Only `pv[0]` is stored.** The rest of the principal variation is discarded,
  and recovering it costs a full re-analysis pass. Worth capturing on the next
  full pass rather than in a third one.
- **Opening identity currently rests on a chess.com URL slug.** `opening` is
  NULL in 7/7 games; `eco` and `eco_url` are present in 7/7. Family grouping
  should come from the move sequence with ECO as cross-check, not from parsing a
  vendor-controlled string.
- **The user's rating moved 168 -> 472 within this dataset.** Games months apart
  are not samples of the same player; any longitudinal claim needs recency and
  rating-band weighting.
- **Coaching-relevant subset is smaller than it looks**: of 380 move rows only
  191 are the user's own, containing 8 BLUNDERs and 35 MISTAKEs, spread across
  six distinct openings whose largest bucket is 2 games. Not enough for any
  recurrence claim.

## Phase 3 - provenance, clocks, PV, openings (2026-09-03)

### Schema
- Splitting the old `moves` table into `game_moves` (raw facts) and
  `move_analysis` (engine observations attributed to a run) is the schema
  expression of the integrity ladder. It also stopped raw facts being duplicated
  once per analysis run: 4 runs would have meant 4 copies of every FEN.
- SQLite cannot alter a UNIQUE constraint, so `UNIQUE(game_id, ply)` ->
  `UNIQUE(run_id, game_id, ply)` required a table rebuild. Doing it at 380 rows
  cost minutes; at 100k rows it would have been a project.
- `executescript(SCHEMA)` fails on an older database if ANY statement in it
  references a column that `_ensure_columns` has not added yet. Both the
  `current_move_analysis` view AND `idx_games_run` referenced
  `games.analysis_run_id`, so both had to move into a POST_MIGRATION script that
  runs after the ALTER TABLEs. The index was the second, non-obvious offender.
- The migrated rows are attributed to a run whose `notes` begin "RECONSTRUCTED".
  Inferring the parameters is fine; presenting the inference as a recorded fact
  would not be.

### Clocks
- chess.com `[%clk]` is the clock AFTER the move, increment already credited.
  So `time_spent = clock_before - clock_after + increment`; without adding the
  increment back, every move on a 300+5 game reads as negative time spent.
- `clock_before(ply) = clock_after(ply - 2)` (same player's previous move), with
  the base time for plies 1 and 2. Verified across all 380 moves: 0 violations.
- Real numbers from game 1: 2.3 s for 1.e4, 8.6 s for ply 6, longest think 77.4 s,
  lowest clock reached 5.5 s.

### Engine / PV
- `board.variation_san(pv)` renders a PV in SAN from the current position;
  storing both the UCI list and the SAN string costs little and saves every
  future consumer from re-deriving it.
- PV verification is cheap and worth doing: replay each stored PV from
  `fen_before` and assert every move is legal and that `pv[0] == best_move`.
  802 rows checked, 0 failures.

### Benchmark
- Depth 18 costs roughly **19x** depth 14 on the same game: game 3 took 2.8 s at
  depth 14 and 52.5 s at depth 18 (37 moves). Any future "priority games at
  depth 18" policy must budget for that, not assume it is a small increment.
- Full depth-14 pass over 7 games / 380 moves: 25.1 s, 3.6 s per game.

### Tooling gotcha
- The Bash tool in this environment mangles heredocs: `\` collapses to `\`, and
  some payloads abort with "unexpected EOF looking for matching quote". Two real
  bugs came from this (a broken f-string in `__main__.py`, a failed hash script).
  Write non-trivial file content with the Write tool, not `cat <<EOF`.
- SQL does not concatenate adjacent string literals the way Python does. A long
  note split across source lines inside a triple-quoted SQL string is a syntax
  error; bind it as a parameter instead.

## `update` orchestrator (2026-09-04)
- Stage failures and item failures are different and must be handled
  differently. A stage failure (network down, engine dead) aborts the pipeline:
  analysing against a half-finished ingest silently produces a partial picture.
  An item failure (one malformed PGN) is already isolated by `ingest`,
  `extract_moves` and `analyze` on purpose, and aborting on those would destroy
  the resumability those stages were built for. Both are surfaced; neither is
  swallowed.
- A past month that ingested cleanly is marked `complete` and never refetched,
  so new games only ever appear in the CURRENT month's archive. An integration
  test that adds games to a past month tests a scenario that cannot happen -
  the first version of the `update` tests did exactly that and failed correctly.
- `AUTOINCREMENT` never reuses ids, so deleting the temporary
  `Stockfish 19.1-dev` schema-probe run left a permanent gap at
  `analysis_runs.id = 5`. That is a feature, not corruption: a deleted run's id
  can never be silently reused by a later one.
- Verified on real data: after the first `update` (2 new games, 138 moves), two
  further cycles left games/raw/observations/runs/canonical counts byte-for-byte
  identical, with 0 duplicates and a clean foreign-key check.

## Multi-player profiles (2026-09-09)
- The minimal correct solution needed **no schema change**. Physical separation
  (one database and one raw PGN directory per profile) makes mixing two players
  unreachable, whereas a shared `player_id` column would make correctness depend
  on every future query - including the Pattern Engine's - remembering a WHERE
  clause. Choosing files over a column traded a cross-player JOIN nobody needs
  for a class of bug that can now never happen.
- A profile must NOT inherit `paths` from the base config layers. `config.yaml`
  sets `data/games` and `data/chess_coach.db`; a naive deep-merge would silently
  point every new player at the default player's database. Only paths declared
  by the profile file itself are honoured, and colliding with the default
  profile's storage is refused at load time.
- The database stamps its owning username into `schema_meta` on first open and
  refuses to open for anyone else. A pre-existing unstamped database is adopted,
  not rejected, so data created before profiles existed keeps working.
- **A shared game is not a leak.** The two tracked profiles played 3 games
  against each other on 2026-09-08; those games exist in both databases, with
  opposite `player_color` and opposite `player_result`. That is correct: the
  game is a fact about both people, and only the perspective is per profile.
  This looked alarming at first glance and is worth remembering.
- SQLite byte-comparison is a bad isolation assertion: merely OPENING a database
  rewrites its header. Compare bytes before re-reading, or compare logical
  content instead.

## Second profile: first ingest (2026-09-09)
- 53 archives, **6330 games**, 0 failed, 87 s. Raw extraction: 429,768 moves in
  152 s with 100% clock coverage, 2 item failures - both genuine abandoned games
  containing no moves at all ("won - game abandoned"). Correct isolation of a
  bad item, not a bug.
- Storage cost at this volume: 96 MB database, 15 MB of raw PGNs.
- Analysing all 6330 at depth 14 would take roughly **6.5 hours** single-
  threaded at the measured 3.7 s/game. That is the first time throughput has
  mattered; it was never an issue at 29 games.
