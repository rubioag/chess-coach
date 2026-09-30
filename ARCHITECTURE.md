# Chess Coach — Architecture

Two documents sit beside this one: `todo.md` (what is done and what is next) and
`lessons.md` (non-obvious facts discovered while building). This file is the
durable design record: what exists, what is coming, and which constraints must
survive contact with the coaching layer.

Status: **Phases 1 (ingestion), 2 (Stockfish foundation) and 3 (data integrity
and provenance) are complete and verified.** Nothing in the "Future" sections
below is implemented, and none of it should be implemented before the data
questions in section 8 are answered.

---

## 1. Current architecture (verified)

```text
CHESS.COM PubAPI
      |
      v
RAW PGN FILES              data/games/YYYY/MM/{id}.pgn   -- immutable, write-once
      |
      v
PGN PARSER                 chess_coach/pgn_parser.py     -- tolerant, NULL vs 'Unknown'
      |
      +--> games           platform facts + PGN headers
      |
      v
RAW MOVE LAYER             chess_coach/moves.py
  game_moves               SAN / UCI / FEN / clocks. Engine-independent.
      |
      v
STOCKFISH 18               chess_coach/engine.py         -- depth 14, MultiPV 1, 1 thread
      |                                                     PV captured
      v
OBSERVATION LAYER          chess_coach/analysis.py
  analysis_runs            PROVENANCE: engine, version, depth, multipv, threads,
  move_analysis            hash, eval cap, rule version, thresholds, timestamps
      |                    every observation carries its run_id
      v
current_move_analysis      view: raw layer + the canonical run only
      |
      +--> chess_coach/openings.py    move-derived families (descriptive)
      |
      v
DESCRIPTIVE AGGREGATION    chess_coach/aggregate.py      -- counts, never scores
```

The two-layer split is the schema expression of the integrity ladder in
section 2: `game_moves` holds facts, `move_analysis` holds an engine's opinion
about those facts, and the opinion is never stored without saying who formed it
and under which rules.

| Module | Role |
| --- | --- |
| `config.py` | All tunables. No personal data in code; `config.local.yaml` is git-ignored. |
| `chesscom.py` | PubAPI client. Serial requests, ETag sync, `url` as stable id. |
| `ingest.py` | Raw PGN write-once, dedup by `external_game_id`, incremental sync. |
| `pgn_parser.py` | Header extraction. Missing header -> `NULL`; present-but-empty -> `'Unknown'`. |
| `db.py` | Schema v3 + migrations. `games`, `sync_state`, `game_moves`, `analysis_runs`, `move_analysis`, `schema_meta`, `current_move_analysis`. |
| `clocks.py` | **Pure.** TimeControl parsing, `[%clk]` readings, before/after/spent derivation. |
| `moves.py` | RAW layer. Immutable PGN -> `game_moves`. No engine, no network. |
| `engine.py` | Stockfish lifecycle, score normalization, terminal positions, mate mapping, PV. |
| `evaluation.py` | **Pure.** Perspective normalization, loss, classification, phase, `RULES_VERSION`. |
| `analysis.py` | Run lifecycle + resumable per-game state machine. Reads `game_moves`, writes attributed observations. |
| `openings.py` | Move-derived opening families. Descriptive grouping only. |
| `aggregate.py` | Descriptive counts over the canonical run. No scoring of any kind. |
| `update.py` | Thin orchestrator for the routine cycle. No logic of its own. |
| `status.py` | Counters. |

Profiles are pure configuration: `config.py` resolves them and `db.open_db()`
enforces ownership. No other module knows profiles exist.

Dataset as of 2026-09-03: 7 games, 380 raw move rows (100% clock coverage),
802 observation rows across 4 recorded analysis runs, 0 failed. The canonical
set is 380 rows at depth 14; 42 depth-18 rows from priority re-analysis are
retained alongside it. 191 of the 380 canonical moves are the user's own; the
rest are opponents'.

CLI: `update`, `ingest`, `extract-moves`, `analyze`, `report`, `status`.

`update` is the routine cycle - `ingest -> extract-moves -> analyze` - and is
**plumbing, not a fourth layer**: it contains no analysis logic, calls the three
existing entry points in order, aborts rather than continuing past a broken
stage, and inherits every idempotency and resumability guarantee unchanged
because it reimplements none of them. A test asserts its source contains no
classification, threshold, pattern, weakness, priority or mastery logic.

---

## 1b. Multiple players

The system supports more than one player. Isolation is **physical, not
conditional**: each profile owns a separate SQLite database and a separate
immutable raw PGN directory. There is no `player_id` column and no query that
has to remember to filter by it, because there is nothing to filter - a
profile's database contains only that profile's games.

```text
config.yaml              shared: engine path, depth, thresholds, phase rules
     |
     +-- config.local.yaml ........ default profile identity  -> data/
     |
     +-- profiles/<name>.yaml ..... one more player           -> data/profiles/<name>/
```

```bash
python -m chess_coach profiles                      # who is configured
python -m chess_coach update                        # the default profile
python -m chess_coach --profile second-account update  # a different tracked account
```

**One engine, many players.** Nothing about the analysis is duplicated. The same
`ingest -> extract-moves -> analyze -> report` pipeline runs for every profile;
only the configuration differs. Engine settings, thresholds and phase rules are
shared deliberately, so two players' numbers stay comparable in future.

**Why not a `player_id` column.** A shared database would make correctness a
matter of every query remembering a `WHERE` clause, forever, including in
components that do not exist yet (Pattern Engine, User Model). One forgotten
filter would silently blend two people's evidence. Separate files make that
class of bug unreachable. The cost is that cross-player queries need two
connections - which nothing needs today, and which is the right trade while the
system's whole purpose is to model each person individually.

**Three guards against the one remaining failure mode**, a mis-configured path:

1. A profile that declares no `paths` gets `data/profiles/<name>/`
   automatically. It never inherits the base config's paths.
2. A profile whose declared paths collide with the default profile's is
   rejected at load time.
3. Every database records the username it belongs to in `schema_meta` and
   refuses to open for anyone else (`ProfileMismatch`). Usernames compare
   case-insensitively, as chess.com treats them. A database created before
   profiles existed is adopted on first open, not rejected.

**A game between two configured players is not contamination.** It legitimately
exists in both databases, because it is a fact about both. What never crosses
over is the perspective: each profile stores it from its own owner's side, so
the same game is a win in one database and a loss in the other. A test fixes
this semantics.

**No schema change was required** for any of this. `schema_meta` already
existed; the profile stamp reuses it.

---

## 2. The analytical integrity ladder

This is the most important rule in the project and it is permanent.

```text
RAW DATA            the PGN bytes chess.com served
     |
     v
ENGINE OBSERVATION  "evaluation dropped 717 cp at ply 26"
     |
     v
INTERPRETATION      "this move hung a piece to a back-rank motif"
     |
     v
COACHING DECISION   "train back-rank awareness before opening theory"
```

Each arrow is a separate, falsifiable step. The system currently implements the
first two and stops. Specifically:

* `evaluation_loss` is an **observation**.
* `classification = BLUNDER` is an observation passed through a documented
  threshold. It is still an observation, not a diagnosis.
* Neither one means "this player has a tactical weakness." A single move is
  never evidence of a persistent trait.

A future weakness claim must require **recurrence, sufficient sample size, and
context** (rating band, time control, time remaining, phase). Any component that
crosses from observation to interpretation must record *why* it crossed, so the
inference can be audited and reversed.

Corollary for schema design: never overwrite an observation with an
interpretation. Store both, in different columns or different tables.

---

## 3. Learning design constraints

These are **product and UX constraints**, derived from how this specific user
learns. They are not medical claims and they are not optional decoration — they
determine what the coaching layer is allowed to output.

**Action first.** The user makes a decision before receiving an explanation. The
explanation supports the action; it never replaces it. A component that emits a
lecture before a decision point is wrong regardless of how correct its chess is.

**Cognitive load.** Default training unit: ~10–15 minutes, 5–10 high-quality
exercises, **at most one genuinely new concept per cycle**. The pipeline is:

```text
ONE CONCEPT -> PRACTICE -> FEEDBACK -> REPETITION -> REAL GAME -> DID IT IMPROVE?
```

not "10 concepts, 50 exercises, 100 statistics." Optimize learning efficiency,
not content volume. Sessions need a natural stopping point; continuing past it
is allowed only after an explicit boundary.

**Zero-friction start.** The entry point is one action:

```text
TODAY'S TRAINING

[ START SESSION ]
```

Not a menu of Tactics / Openings / Endgames / Strategy / Puzzles. The coach
decides the training diet. Manual exploration may exist, but never as the
default screen.

**Own-data first.** Exercises should come from the user's own games whenever
sufficient relevant data exists. External positions are legitimate when own data
is insufficient, when a concept needs more examples, for transfer and
generalization, or to prevent overfitting to familiar positions. The ratio is a
**tunable, not a constant** — do not hard-code 80% or any other figure.

**Repetition targets patterns, not positions.** Never re-serve the identical
puzzle. Identify the underlying pattern and express it in different positions.
The system must distinguish exact-position memory, pattern recognition, and
transferable skill; only the last is the goal. Repetition frequency decreases as
the real-game error rate decreases.

**Feedback.** Immediate, direct, visual where useful, actionable, neutral,
concise. On error: short feedback -> visual clue -> **second attempt** ->
explanation. Never dump the answer on the first mistake.

Explanations are practical, not numeric:

> Your opponent's rook becomes active on the open file and your king has no safe square.

not

> Stockfish says +3.4.

Engine data *supports* the explanation; it is not the explanation.

**Tone.** Analytical and neutral — "bug to patch", never "you played terribly".
No paternalism, no motivational clichés, no guilt, no shame, no gamified praise.
Target register:

> This pattern appeared in 3 of your last 10 games. It is currently costing you
> more than your opening mistakes. Let's patch it.

**Progress measurement.** Hours studied and current Elo are *not* primary
metrics. Rating is noisy and emotionally loaded. Use instead: frequency of a
specific error, severity, recurrence, performance in relevant positions,
transfer to real games, retention over time, and improvement against the user's
own historical baseline. The target statement is:

> In your last 20 relevant positions you identified this pattern correctly 17
> times. Previously it was 9/20.

**Do not declare mastery early.** `85% = learned` is not a mastery model. A real
one should weigh minimum sample size, exercise performance, **real-game**
performance, recurrence, severity, recency, retention after a delay, and
transfer to unfamiliar positions. In particular:

```text
HIGH PUZZLE ACCURACY + LOW REAL-GAME TRANSFER = NOT MASTERED
```

The mastery model gets designed after real data exists, not before.

---

## 4. Opening philosophy

Opening training must not become a memorization tree.

```text
MINIMAL REPERTOIRE + DEEP UNDERSTANDING + REAL-GAME RELEVANCE
```

Initial default: one White repertoire, one defense to 1.e4, one defense to 1.d4.
Do not expand because a GM plays something, because it is fashionable, because
the user is bored, or because a video recommended it. Expansion requires
evidence of practical need. Equally, "exactly three openings forever" is **not**
a hard rule — it is a minimalism default the system may later justify changing.

**Openings are middlegame training.** Instead of "memorize moves 1–18", present
a position around move 6–10 and ask: *What is your main plan here? Which
opponent piece bothers you most? What pawn break are you looking for?* Test
plans, piece placement, pawn breaks, typical tactics, positional goals, and the
opponent's counterplay before demanding long variations. Ideas are primary;
concrete theory appears only where practically necessary. The ratio stays
configurable.

**Selection is data-driven, not personality-driven.** Not "you like tactics,
play the Sicilian." Instead:

```text
USER'S REAL GAMES -> OPENING FAMILIES -> RESULTING POSITION TYPES
   -> USER PERFORMANCE -> COMPETENCE / PREFERENCES -> CANDIDATES -> TESTING
```

**Do not overfit to win rate.** 4–1 does not mean "your best opening"; 2–4 does
not mean "abandon it." Separate opening quality, the player's understanding,
opponent strength, time control, game context, sample size, and resulting
middlegame performance. Today the largest move-derived opening family in the
whole dataset holds **1 game** — no opening claim of any kind is currently
supportable.

**"Do not change anything" is the default output, not a failure to decide.**
The repertoire engine must be able to conclude:

> Your repertoire is good enough. Do not study a new opening. Train something
> else this week.

and that should be its *normal* answer. An engine that always finds an opening
to change is not analysing, it is generating work. Recommending a change has to
clear a bar; recommending no change does not.

**A change is justified by fit, never by abstract quality.** The question is
never "is defence X objectively better than defence Y". It is: does the family
of positions this line produces suit how this player actually plays? The shape
of a justified change looks like:

```text
WHITE  1.e4 -> structures A/B/C -> good practical results
       -> few recurring errors                              => KEEP

BLACK vs 1.e4  current repertoire -> uncomfortable positions
       -> recurring middlegame problems
       -> variation X produces positions this player handles
          especially badly                                  => INVESTIGATE CHANGE
```

And when a change is made, what gets taught is the starting positions plus,
above all, **the plan after the opening** - not a memorized tree.

---

## 5. Future learning architecture

```text
                    +---------------------+
                    |   GAME DATABASE     |   games / moves        [EXISTS]
                    +----------+----------+
                               v
                    +---------------------+
                    |  ANALYSIS ENGINE    |   Stockfish + metrics  [EXISTS]
                    +----------+----------+
                               v
                    +---------------------+
                    |  PATTERN ENGINE     |   recurrence, context  [FUTURE]
                    +----------+----------+
                               v
                    +---------------------+
                    | USER MODEL          |   weaknesses, mastery,
                    |                     |   learning response    [FUTURE]
                    +----------+----------+
                               v
                    +---------------------+
                    | PRIORITY ENGINE     |   what matters NOW     [FUTURE]
                    +----------+----------+
                               v
                    +---------------------+
                    | TRAINING GENERATOR  |   10-15 min session    [FUTURE]
                    +----------+----------+
                               v
                    +---------------------+
                    | TRAINING RESULT     |                        [FUTURE]
                    +----------+----------+
                               v
                         NEW REAL GAMES  ->  back into USER MODEL
```

### Where each future component fits

| Component | Likely home | Reads | Writes | Depends on |
| --- | --- | --- | --- | --- |
| **Pattern Engine** | `chess_coach/patterns/` | `moves` (FEN + eval + PV), raw PGN | `patterns`, `pattern_instances` | position features, PV continuation |
| **User Model** | `chess_coach/user_model/` | `pattern_instances`, `training_results`, `games` | `user_pattern_state` (recurrence, severity, recency, mastery) | Pattern Engine + training history |
| **Priority Engine** | `chess_coach/priority/` | `user_pattern_state` | `training_priority` (ranked, with justification) | User Model |
| **Training Generator** | `chess_coach/training/` | `training_priority`, `pattern_instances`, external position bank | `training_sessions`, `exercises` | Priority Engine |
| **Opening/Repertoire Engine** | `chess_coach/openings/` | `games.eco/eco_url`, early-ply `moves`, per-family performance | `opening_families`, `repertoire_state` | opening family normalization |
| **Explanation layer** | `chess_coach/explain/` | position + PV + pattern label | ephemeral | Pattern Engine; likely an LLM call over position + engine PV, **not** hand-built detectors |

Two structural rules for all of them:

1. **They read; they never rewrite `games`, `moves`, or the raw PGN directory.**
   Every future component writes to its own tables. This keeps the observation
   layer reproducible and lets any interpretation be dropped and rebuilt.
2. **Every interpretation row records its provenance** — which analysis run,
   which rule version, which thresholds produced it.

### Learn how the user learns

The coach must not assume the profile in section 3 is permanently correct. It
should eventually gather evidence on which exercise formats produce retention,
which feedback style produces successful second attempts, the optimal exercise
count, concept decay speed, whether own-game positions actually outperform
generic ones, which session lengths precede better play, and whether tactical
and positional training transfer differently.

```text
USER MODEL + CHESS MODEL = PERSONALIZED TRAINING MODEL
```

This implies the training tables must record the *format* and *conditions* of
every exercise, not only whether it was answered correctly. A schema that stores
only right/wrong cannot ever answer these questions.

### What a session looks like when the chain works

The whole pipeline exists to produce one screen:

```text
TODAY'S SESSION
8 positions - 12 minutes
Goal: spot the opponent's threats

[ START ]
```

One objective, stated up front, chosen by the coach. No category menu. The
objective is the visible end of the chain: Pattern Engine found recurrence,
User Model judged it unresolved, Priority Engine ranked it first, Coaching
Engine turned it into today's twelve minutes.

### The questions the intelligence layer exists to answer

This list is the acceptance criteria for building it at all. Until there is
enough data to answer these honestly, there is nothing to build:

* Which errors actually repeat?
* Which of them matter?
* In what situations do they appear?
* Are they tactical, strategic, time-driven, or opening-driven?
* Which patterns are disappearing?
* Which persist?
* What should be trained this week?
* Which opening or repertoire suits this player?
* **When should training on something stop?**
* When should something new be introduced?
* Did the training produce a real improvement in actual games?

The second-to-last one is the most neglected and the most important: a coach
that can only add topics, never retire them, will bury the user. Stopping
criteria are part of the mastery model, not an afterthought to it.

---

## 6. What is already safe and flexible

Verified, not assumed:

* **Raw PGN is an immutable, complete source of truth.** Write-once,
  byte-identical to the API payload, one file per `external_game_id`. Anything
  we failed to extract is recoverable by re-parsing from disk with no API call
  and no engine run. This is the single most valuable property the project has —
  it was proven by finding that per-move clocks are present in 7/7 PGNs and
  still recoverable despite never having been stored.
* **Full FENs per move.** `fen_before` and `fen_after` are complete FENs, so
  every position-derived feature the Pattern Engine will want — material,
  structures, checks, captures, king safety, piece activity, phase — is
  recomputable without re-running Stockfish. Phase labels can be recomputed
  under new rules at any time.
* **Depth upgrade needs no schema change.** Verified live: game 5 was
  re-analyzed at depth 18, the DB briefly held `{depth 14: 375 rows, depth 18: 5
  rows}` with no duplication, then was restored to 14. `--force` and
  `--depth` already work.
* **The evaluation layer is pure.** Thresholds, the eval cap, and phase rules
  live in config and are applied by side-effect-free functions. They can be
  changed and unit-tested without touching the engine or the database.
* **Stable game identity.** `external_game_id` is the chess.com `url` verbatim,
  which survived their integer -> uuid id change.
* **Multi-platform is structurally ready.** `games.platform` exists and
  `sync_state` is keyed by archive URL, so a lichess ingester is a new client
  module against the same schema, not a migration.
* **Resumable, honest analysis state.** `pending / running / completed / failed`
  with retry; failures are never silently completed.
* **Provenance is structural, not a convention.** `move_analysis` cannot hold a
  row without a `run_id`, and `analysis_runs` cannot hold a row without every
  parameter that shapes a measurement. Verified: a `Stockfish 19.1-dev` run at
  depth 20 inserts with no DDL change.
* **Raw and observation layers cannot drift.** `analyze_raw_moves` replays each
  board from the stored `fen_before` and refuses to proceed if the replay
  disagrees, so `game_moves` and `move_analysis` are always the same game.
* **143 passing tests**, including both spec sign-normalization cases, every
  classification boundary, mate handling, phase rules, resume and failure paths,
  clock derivation, opening grouping, aggregation honesty, and the v2 -> v3
  migration.

---

## 7. Architectural risks

Phase 3 closed the four highest-cost items. What follows records both the
resolutions and what remains, because a risk register that only lists open items
loses the reason a design looks the way it does.

### RESOLVED in Phase 3

**R1 — No analysis provenance. (was HIGH)** `moves` has been split into
`game_moves` (raw facts) and `move_analysis` (engine observations), the latter
keyed `UNIQUE(run_id, game_id, ply)` and referencing `analysis_runs`. Each run
records engine name and version, depth, MultiPV, threads, hash, evaluation cap,
`RULES_VERSION`, all three classification thresholds, all three phase-rule
parameters, start/end timestamps and status. Re-analysis inserts a new run and
leaves earlier ones intact. `games.analysis_run_id` names the canonical run and
the `current_move_analysis` view exposes only that one, so aggregation cannot
double-count. The 380 pre-existing rows were migrated, not discarded, and are
attributed to a run whose `notes` say plainly that its parameters were
reconstructed rather than recorded.

**R2 — Per-move time not stored. (was HIGH)** `game_moves` now carries
`clock_after_ms` (the verbatim `[%clk]` reading), `clock_before_ms` (the clock
the mover actually had while deciding) and `time_spent_ms`. Coverage is 380/380
and every reading was verified against the PGN. No API call was involved.

**R3 — Only the top move stored. (was MEDIUM)** `move_analysis` stores `pv`,
`pv_san` and `pv_length` next to `best_move`. Every stored PV was verified legal
from its `fen_before` and verified to begin with `best_move`.

**R4 — Opening identity depended on a chess.com string. (was MEDIUM)**
`openings.py` derives families from the move sequence in `game_moves`, with ECO
retained purely as a cross-check that reports disagreement instead of hiding it.
Nothing is denormalized onto `games`, so any prefix length is a query parameter
rather than a migration.

### Still open

**R5 — The user's rating moved 168 -> 472 inside this dataset. (MEDIUM)**
Unchanged and unfixable by schema work. Games months apart are not samples of
the same player. Any recurrence or "did it improve?" computation must weight by
recency and rating band, or it will attribute skill growth to training that did
not cause it.

**R6 — Interpretation could leak into the observation tables. (MEDIUM,
preventive)** The schema now makes the boundary obvious, but nothing enforces
it. The first time a "weakness" or "motif" column is added to `game_moves` or
`move_analysis`, the ladder collapses. Future components get their own tables.

**R7 — `analysis_status` is per game, not per analysis type. (LOW)** Still fine.
A future non-Stockfish pass (pattern extraction) needs its own status column or
table rather than overloading this one.

### New, introduced by Phase 3

**R8 — Prefix keys do not merge transpositions. (LOW-MEDIUM)** `1.d4 Nf6 2.c4
e6` and `1.c4 e6 2.d4 Nf6` reach the same position under different opening keys.
Merging them needs a position-based key or a real ECO book. Recorded in
`openings.py` rather than papered over; harmless while families hold one game
each, and it must be fixed before repertoire reasoning.

**R9 — The canonical run is a single pointer per game. (LOW)** Nothing prevents
`games.analysis_run_id` from pointing at runs of different depths across
different games, which would make an aggregate silently mix depth 14 and depth
18. This was observed deliberately during verification (canonical was briefly
`{14: 338, 18: 42}`) and then made homogeneous. `report` prints the depth of
every run and which games it is canonical for, so the condition is visible - but
it is reported, not prevented.

Not a risk, explicitly: phase labels, which remain recomputable from the stored
FENs under any future rule set.

## 8. What data we still need

**The binding constraint is game volume, and it cannot be fixed by better
ingestion.** The chess.com account exposes exactly 2 monthly archives totalling
7 games — that is the account's entire history. Ingestion is already complete and
idempotent. More data means games played over time, another platform, or another
account.

Current evidence base for coaching:

| Quantity | Today |
| --- | --- |
| Games | 7 |
| User's own moves | 191 |
| User BLUNDERs | 8 |
| User MISTAKEs | 35 |
| Largest (ECO, color) bucket | 2 games |
| Time controls | blitz only (300+5 x6, 180+2 x1) |
| User rating span | 168 -> 472 |

Eight blunder events across six distinct openings cannot establish that any
pattern *recurs*, which is the minimum bar section 2 sets for a weakness claim.

Phase 3 removed the *measurement* obstacles on this list: time-per-move is now
captured (380/380), and analysis provenance is recorded. What remains cannot be
solved by code.

What is needed, in order:

1. **Volume.** Enough games that a candidate pattern can appear repeatedly in
   comparable contexts. The exact threshold is a **hypothesis to be measured
   against the real distribution**, not a constant to pick now — the whole point
   of gathering data first is to learn what sample size the data actually
   demands.
2. ~~Time-per-move~~ **done in Phase 3**: 380/380 moves carry
   `clock_before_ms`, verified against the PGN source.
3. **Diversity, or an explicit decision not to have it.** All-blitz data
   supports only all-blitz conclusions. Repeated exposure to the same opening
   families is required before any repertoire reasoning.
4. **Post-training games.** "Did the error actually decrease?" needs games played
   *after* an intervention. No amount of historical data substitutes for this,
   and the clock on it only starts once training exists.
5. ~~Analysis provenance~~ **done in Phase 3**: every observation is attributed
   to a run recording engine, version, depth and every threshold in force.

---

## 9. Recommended next phase

**Phase 3 is complete.** The measurement layer is now provenance-safe: every
observation says which engine, depth and rule set produced it, raw facts are
stored once and separately, clocks and PVs are captured, and opening families
come from moves rather than a vendor string.

**Phase 4 is not an engineering phase. It is waiting for data.**

The chess.com account's entire history is 7 games across 2 archives; ingestion
is complete and idempotent, so volume is gated on games being played, not on
code. The recommended next steps, in order:

1. **Keep ingesting on a cadence.** After playing, run one command:

   ```
   python -m chess_coach update
   ```

   It chains `ingest -> extract-moves -> analyze`, is safe to run repeatedly,
   resumes an interrupted cycle, and destroys nothing. Verified on real data:
   two further cycles left every row count byte-for-byte identical.
2. **Decide whether to add lichess ingestion** to accelerate volume. The schema
   is ready: `games.platform` exists and `sync_state` is keyed by archive URL, so
   this is a new client module, not a migration.
3. **Re-read `report` periodically** and watch the real distribution: how loss
   is distributed, whether any move-derived opening family accumulates enough
   games to mean anything, whether the time-remaining buckets separate.
4. **Only then design the Pattern Engine**, against the distribution that
   actually exists rather than an assumed one.

### Review checkpoints (agreed 2026-09-04)

Two numbers are now agreed, and what they are must not be misread:

| Games | What happens |
| --- | --- |
| **~30** | First **exploratory audit**. Look at the data and ask whether any pattern appears to be emerging at all. Build nothing. |
| **~50** | Consider designing the **first version of the Pattern Engine**. |

These are **triggers to look**, not evidence thresholds. Reaching 30 games does
not license a weakness claim, and reaching 50 does not license one either - it
licenses *designing a component* whose own evidence rules still have to be met
per pattern. The audit at 30 can perfectly well conclude "there is not enough
signal here yet", and that is a valid, expected outcome.

The sample-size threshold for an actual weakness claim remains deliberately
unwritten. Choosing that number now would repeat the `85% = mastered` mistake
the design constraints explicitly forbid; it must be derived from the observed
distribution once there is one. A checkpoint says *when to inspect*; it does not
say *what the data will support*.

**Explicitly not now:** coaching engine, automatic weakness claims, priority
scoring, opening recommendation, adaptive repertoire, mastery model, spaced
repetition, LLM explanations, motif detectors, dashboard, gamification, training
feedback loops.
