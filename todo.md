# Chess Coach — V1 TODO

Scope reminder: V1 builds a trustworthy data + engine-analysis foundation.
No motif detection, no explanations, no coaching engine, no dashboard.

## Phase 1 — Ingestion (§3)  — COMPLETE, verified against real games
- [x] `config.py` — YAML config, `config.local.yaml` override, no personal data in code
- [x] `chesscom.py` — PubAPI client, serial requests, User-Agent, retry/backoff
- [x] `db.py` — SQLite schema (`games`, `sync_state`, `schema_meta`)
- [x] `pgn_parser.py` — tolerant parsing, NULL vs `'Unknown'` for ECO/Opening
- [x] `ingest.py` — raw PGN storage, dedup by `external_game_id`, ETag sync
- [x] `status.py` — counters
- [x] `__main__.py` — `ingest` / `analyze` (stub) / `status`
- [x] Unit tests: parser, id handling, ingest idempotency/dedup/304/failures
- [x] Real `username` + contact set in `config.local.yaml` (git-ignored)
- [x] Smoke run: 5 games imported, 0 failed
- [x] Raw PGNs byte-identical to API payload (5/5 compared, 0 mismatches)
- [x] DB rows verified: 7/7 colors + results resolved, 7 ECO, 7 opening NULL
- [x] Full historical ingest: 7 games, 2 archives
- [x] Idempotency rerun: 0 imported, 304 on current month, past month skipped
- [x] Fixed ETag-on-incomplete-month data-loss bug (see lessons.md) + 3 tests
- [x] Ingestion evidence reported → **STOPPED, awaiting approval for Stockfish**

### Follow-ups carried into later phases
- [ ] Derive opening names from `ECOUrl` / ECO table — no `Opening` header exists
      in this account's PGNs (7/7 NULL)
- [ ] Dataset is only 7 games; benchmarking Stockfish throughput will need more

## Phase 2 — Stockfish analysis (§4-§9)  — COMPLETE, verified on 7 real games
- [x] Stockfish 18 installed via winget; path in `config.local.yaml`
- [x] `moves` table (schema v2) + state machine (`pending/running/completed/failed`)
- [x] Eval normalization from mover's perspective, unit-tested both colors
- [x] Config-driven thresholds, pure + deterministic + boundary-tested
- [x] Explicit mate handling (mapped cp + signed mate distance, both stored)
- [x] Deterministic phase detection, documented rules
- [x] Resumable backfill at depth 14; `--force --depth 18` re-analysis path tested
- [x] Benchmarked: 380 moves in 33.6 s = 4.8 s/game, 0.088 s/move
- [x] One-game validation (game 1, 93 plies, ends in mate) + independent re-check
- [x] Full backfill: 7 games, 380 moves, 0 failed
- [x] **STOPPED** — no advanced analytics

### Not done on purpose (§6, §11, §16)
- No motif detection, no explanations, no coaching, no weakness/priority scoring
- No parallelism or engine tuning: correctness first, per the brief

## Phase 3 — Data integrity + provenance  — COMPLETE, verified
Full analysis in `ARCHITECTURE.md` sections 1, 7 and 9.
- [x] **Analysis provenance** (schema v3): `analysis_runs` + `move_analysis.run_id`
      recording engine name/version, depth, multipv, threads, hash, eval cap,
      rules version, all thresholds, all phase params, timestamps, status
- [x] `UNIQUE(run_id, game_id, ply)` so runs coexist; re-analysis adds, never
      destroys. `games.analysis_run_id` + `current_move_analysis` view prevent
      double-counting
- [x] v2 -> v3 migration preserving all 380 historical rows, attributed to a run
      labelled RECONSTRUCTED (7 migration tests)
- [x] **Raw layer split**: `game_moves` (SAN/UCI/FEN/clocks) separate from
      engine observations - facts stored once, opinions attributed
- [x] **Per-move clocks** re-parsed from immutable PGNs: 380/380 coverage,
      clock_before / clock_after / time_spent, verified against source
- [x] **Principal variation** stored (pv, pv_san, pv_length); all verified legal
      and starting with best_move
- [x] **Opening families** derived from move sequence, ECO as cross-check only;
      prefix depth is a query parameter, nothing denormalized
- [x] **Descriptive aggregation** (`report`): classification, loss distribution,
      phase, color, time control, result, time remaining, opening family, per
      game. No scores, no recommendations - guarded by a test
- [x] Data integrity verified: raw PGNs byte-identical, 0 duplicates, 0 orphans,
      FK check clean, integrity_check ok
- [x] 143 tests passing (was 92)
- [x] `update` orchestrator: `ingest -> extract-moves -> analyze`, stage-failure
      propagation, clean no-op when nothing is new, final roll-up summary.
      Plumbing only - a test asserts it holds no analysis logic (+15 tests, 158)
- [x] **PHASE 3 CLOSED** - no Pattern Engine, no coaching layer

## Phase 4 — Accumulate data (NOT an engineering phase)
- [ ] After playing: `python -m chess_coach update`
- [ ] Decide whether to add lichess ingestion for volume (schema already ready:
      `games.platform` exists, `sync_state` keyed by archive URL)
- [ ] Periodically re-read `report`; watch the real distribution
- [ ] Do NOT pick a sample-size threshold in advance - derive it from the
      distribution once one exists

### Review checkpoints (agreed 2026-09-04)
- [ ] **~30 games** -> first exploratory audit. Look only: is any pattern
      emerging? Build nothing. "Not enough signal yet" is a valid outcome.
- [ ] **~50 games** -> consider designing the first Pattern Engine version.

These are triggers to INSPECT, not evidence thresholds. 30 games does not
license a weakness claim; 50 licenses designing a component whose own per-pattern
evidence rules still have to be met.

Progress (default profile): 29 / 30.

## Multi-player — DONE (2026-09-09)
- [x] Profiles as pure configuration; `--profile` on every command; `profiles`
      command lists them
- [x] Physical isolation: separate database + separate raw PGN dir per profile.
      NO schema change required
- [x] Three guards: auto-isolated default paths, collision refused at load time,
      database stamped with its owning username (`ProfileMismatch`)
- [x] Same single engine and pipeline for every profile; thresholds shared
- [x] 18 isolation tests (176 total)
- [x] Second profile ingested: 6330 games, 429,768 raw moves, 100% clock coverage

### Second profile — pending work (deliberately not done)
- [ ] 6325 games still `pending` analysis (~6.5 h at depth 14). Analyse in
      batches with `--limit`, or leave until there is a reason to need it
- [ ] 2 games have no moves at all (abandoned before move 1); they will be
      marked `failed` when analysis reaches them. Expected, not a bug

### Blocking reality (unchanged)
Volume is gated on games being played, not on engineering. The first real
`update` run already imported 2 new games (7 -> 9, 518 raw move rows), which is
exactly how this phase is meant to progress. Still far too little to support any
weakness or repertoire claim.

## Future — learning / coaching layer (DESIGNED, NOT BUILT)
See `ARCHITECTURE.md` sections 3-5 for the full constraints. Headlines:
- Action first: decision before explanation, always.
- ~10-15 min sessions, 5-10 exercises, max ONE new concept per cycle.
- Zero-friction start: one button, coach picks the diet. No study menu.
- Own-game positions preferred over generic ones; ratio tunable, never hard-coded.
- Repetition targets the PATTERN, never the identical puzzle.
- Wrong answer -> short feedback -> visual clue -> SECOND ATTEMPT -> explanation.
- Neutral "bug to patch" tone. No praise, guilt, or motivational filler.
- Progress = error frequency/recurrence/transfer, NOT hours studied or Elo.
- No simplistic mastery rule; high puzzle accuracy + low real-game transfer
  means NOT mastered.
- Openings: minimal repertoire, taught as middlegame plans, selected from data,
  never from win rate on tiny samples.
- The coach must eventually learn HOW the user learns, not just what they miss.

Components and where they will live: Pattern Engine, User Model, Priority
Engine, Training Generator, Opening/Repertoire Engine, Explanation layer -
see the table in `ARCHITECTURE.md` section 5. All of them READ from `games` /
`moves` / raw PGN and WRITE only to their own tables. Interpretation must never
be written back into the observation tables.

## Explicitly out of scope for V1 (§6, §16)
Motif detectors (forks/pins/skewers/back-rank/weak squares), LLM explanations,
lessons, training plans, feedback loops, advanced dashboard.
