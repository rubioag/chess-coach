# Chess Coach


A personal chess analysis pipeline. It syncs games from the Chess.com public API, stores
the raw PGN untouched, parses it into a queryable schema, analyses every move with
Stockfish, and aggregates the result into per-opening and per-phase statistics.

> **Status: personal project. Phases 1-3 complete and verified against real games.
> Phases 4 onward are designed but not implemented, on purpose.**

---

## Why this repository is worth thirty seconds

The design principle is that **raw data is never overwritten and derived data is never
confused with it**. Downloaded PGN files are written once and never modified; every
later layer — parsed moves, clock times, engine evaluations, opening classifications —
is derived and can be rebuilt from scratch without another network call.

That constraint is what makes the schema migrations safe, and it is why the project can
change how it evaluates a position without ever risking the games it is evaluating.

`ARCHITECTURE.md` is the design record. It states which parts are verified, which are
planned, and which constraints must survive future phases — including an explicit
instruction that nothing in the "Future" sections should be built before the open data
questions are answered. `lessons.md` records non-obvious facts discovered by running
against real data, including bugs found and how.

---

## Architecture

```
Chess.com PubAPI
      │
      ▼
RAW PGN FILES        data/games/YYYY/MM/{id}.pgn   — immutable, write-once
      │
      ▼
PGN PARSER           tolerant parsing; NULL vs 'Unknown' kept distinct
      │
      ├──► games          platform facts + PGN headers
      │
      ▼
RAW MOVE LAYER       SAN / UCI / FEN / clocks — engine-independent
      │
      ▼
ENGINE LAYER         Stockfish evaluations, principal variation
      │
      ▼
AGGREGATION          per-opening and per-phase statistics
```

| Module | Responsibility |
|---|---|
| `chesscom.py` | Public API client: serial requests, honest User-Agent, retry with backoff, ETag sync |
| `ingest.py` | Write-once raw PGN storage, deduplication by external game id |
| `pgn_parser.py` | Tolerant parsing; distinguishes a missing field from a literal `'Unknown'` |
| `moves.py` / `clocks.py` | Move and clock extraction, independent of any engine |
| `engine.py` / `evaluation.py` | Stockfish integration; evaluations normalised to the mover's perspective |
| `analysis.py` / `aggregate.py` | Per-move analysis and statistical aggregation |
| `openings.py` | Opening classification |
| `db.py` | SQLite schema with explicit versioning and migrations |
| `config.py` | YAML config, git-ignored local override, and profile layering; no personal data in code |

---

## What is verified, and what is not

**Verified**

- **Phase 1 — Ingestion.** Raw PGNs confirmed byte-identical to the API payload.
  Idempotent re-runs import nothing. A data-loss bug caused by trusting the ETag on an
  incomplete month was found by running against real data, fixed, and covered by three
  regression tests — the incident is written up in `lessons.md`.
- **Phase 2 — Stockfish.** Evaluation normalisation unit-tested from both colours.
  Analysis state machine (`pending / running / completed / failed`) with recovery.
- **Phase 3 — Data integrity and provenance.** Schema v3 migration with a dedicated
  migration test; clock, principal-variation and opening layers added without touching
  stored raw data.
- **Multi-profile isolation.** Each tracked account gets its own database and raw PGN
  tree, guarded three ways: auto-isolated default paths, path collisions refused at load
  time, and every database stamped with its owning username so it refuses to open for
  anyone else. 18 of the tests cover isolation specifically.
- **Ingestion at real volume.** A second profile ingested 53 archives and **6,330 games**
  with 0 failures in 87 seconds, then extracted **429,768 move rows** in 152 seconds with
  100% clock coverage. Storage at that volume: 96 MB of database and 15 MB of raw PGN.
  Two items failed and both were genuinely abandoned games containing no moves — correct
  isolation of a bad record, not a bug.
- **176 tests**, running in about six seconds with no network access and no Stockfish
  binary required. See the caveat below.

**Not verified, and not claimed**

- **Engine analysis has not been run at that volume.** Ingestion handled 6,330 games;
  Stockfish analysis has only been run over a much smaller set. Analysing the full
  archive would take roughly 6.5 hours single-threaded at depth 14, which `todo.md`
  records as deliberately deferred rather than done.
- **Three of the 176 tests only pass with local configuration.** They read the
  developer's real, git-ignored config and profile files, so on a clean checkout of this
  repository 173 pass and 3 fail. That is a defect in those tests, not in the code they
  cover, and it is why there is no CI badge here yet: a green badge should mean the suite
  runs anywhere.
- **No weakness or repertoire claims.** The default profile has 29 analysed games. The
  project's own notes set 30 as a threshold to *inspect*, explicitly not as evidence.
- Coaching quality. The advisory layer is designed, not built.
- Any statement about the strength or accuracy of the analysis itself.

---

## Stack

Python · `python-chess` · Stockfish (UCI) · SQLite with versioned migrations · PyYAML ·
`requests` · pytest

## Running it

```bash
pip install -r requirements.txt
cp config.yaml config.local.yaml    # set your username, contact and Stockfish path
python -m chess_coach ingest
python -m chess_coach analyze
python -m chess_coach status
```

```bash
pytest    # 176 tests; 3 require local configuration (see above)
```

`config.local.yaml` is git-ignored and holds the only personal data in the project. The
tracked `config.yaml` is a template with placeholders. Requests are serial with a delay
by default: parallel fetching triggers rate limiting, which is one of the findings in
`lessons.md`.

---

## Development method

This project was built with AI-assisted development. The author specified the
architecture and the constraints, directed the model, and reviewed, debugged and
verified the output. The bugs recorded in `lessons.md` were found by running the system
against real data and checking the result — that verification step is the part worth
looking at, and it is stated here rather than left to be discovered.

## Scope

Personal project, built to analyse the author's own games. Data comes from the Chess.com
public API under its own terms.
