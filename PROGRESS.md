# Progress

**Agent: read this first. Build the first phase not marked complete, then stop.**

Mark a phase complete only when its Definition of Done checks pass and the user
has seen the output.

| # | Phase | Spec | Status |
|---|---|---|---|
| 0 | Environment and scaffold | `build/00-setup.md` | ✅ complete 2026-08-06 |
| 1 | Stats ingestion | `build/01-stats-ingestion.md` | ✅ complete 2026-08-09 |
| 2 | Odds ingestion | `build/02-odds-ingestion.md` | ⬜ not started |
| 3 | Cleaning and joining | `build/03-cleaning-joining.md` | ⬜ not started |
| 4 | Features | `build/04-features.md` | ⬜ not started |
| 5 | Minutes model | `build/05-minutes-model.md` | ⬜ not started |
| 6 | Rate models | `build/06-rate-models.md` | ⬜ not started |
| 7 | Simulation | `build/07-simulation.md` | ⬜ not started |
| 8 | Pricing | `build/08-pricing.md` | ⬜ not started |
| 9 | Evaluation | `build/09-evaluation.md` | ⬜ not started |
| 10 | Automation | `build/10-automation.md` | ⬜ not started |

Optional, run before phase 3 if applicable:

| — | Spreadsheet log audit | `build/03b-spreadsheet-audit.md` | ⬜ only if the user has a hand-kept prop log |

Something broken? See `build/recovery.md`.

---

## Phase log

Append one entry per completed phase: what was built, what the DoD checks
returned, and anything left unresolved.

<!-- Agent: append below this line. Do not rewrite earlier entries. -->

### Phase 0 — Environment and scaffold (2026-08-06)

**Built:** `pyproject.toml` (Python ≥3.11, zero dependencies), `run.py` with six argparse subcommands that print not-implemented messages and exit 0, `config.yaml` with `season: 2026` only, `src/log.py` (single logging helper: plain console + timestamped `logs/wnba.log`). Confirmed the repo's `.gitignore` and `.env.example` are correct; copied `.env.example` → `.env` (key still blank). Installed `uv` 0.12.0 and Git 2.55 via winget; `git init` + initial commit made.

**DoD results:** all pass — `--help` lists all six subcommands (exit 0); `update` prints "not implemented — phase 1 builds this" (exit 0); bare `run.py` prints help, no traceback (exit 0); `uv sync` works from clean checkout (created `.venv`, CPython 3.12.1).

**Unresolved:** ODDS_API_KEY in `.env` is empty — needed by phase 2, not phase 1. DB Browser for SQLite not yet installed (user-facing tool, not needed until data exists). Repo was downloaded as a zip before git was installed, so history starts at this commit rather than upstream's.

### Phase 1 — Stats ingestion (2026-08-09)

**Built:** `src/config.py` (config.yaml loader), `src/db.py` (SQLite schema: games, player_games, availability, players, teams, meta), `src/ingest_stats.py` (fetch via sportsdataverse → raw parquet to `data/raw/stats/` → parse → upsert → 7 validation checks → summary), `run.py update` wired. Seasons 2022–2026 backfilled: 1,297 games, 29,421 player-games, 361 players. Availability breakdown: 24,693 played / 2,580 dnp_coach / 1,821 dnp_injury / 302 inactive / 25 dnp_rest.

**DoD results:** all pass — second `update` run reports "0 new games"; 7/7 validation checks pass (two required fixes recorded in DECISIONS.md: ±3 minutes rounding tolerance, Commissioner's Cup final excluded from the 44-game cap); summary prints seasons/games/player-games/date range/checks; availability has 4,856 rows with minutes_played = 0; Toronto Tempo and Portland Fire each ingested cleanly (350 player-games, 17 players, no imputation).

**Unresolved:**
- **Source feed lags ~1 week.** Latest completed game in the sportsdataverse release is 2026-08-01; 25 games played Aug 2–9 show as "Scheduled". Fine for model development; projections (phase 5+) will need the live ESPN endpoints (`espn_wnba_scoreboard` etc.) for freshness.
- `not_on_roster` availability status never emitted — box scores only list rostered players; needs roster/injury data in a later phase.
- Play-by-play not ingested (deferred, see DECISIONS.md) — the 0.44 possession coefficient stays an approximation until then.
- `plus_minus` arrives as a string and is null for 2 played rows ("plus_minus_missing" issue code); 28 played rows have null minutes (kept with issue code `minutes_null_for_played_row`).
