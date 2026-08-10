# Progress

**Agent: read this first. Build the first phase not marked complete, then stop.**

Mark a phase complete only when its Definition of Done checks pass and the user
has seen the output.

| # | Phase | Spec | Status |
|---|---|---|---|
| 0 | Environment and scaffold | `build/00-setup.md` | ✅ complete 2026-08-06 |
| 1 | Stats ingestion | `build/01-stats-ingestion.md` | ✅ complete 2026-08-09 |
| 2 | Odds ingestion | `build/02-odds-ingestion.md` | ✅ complete 2026-08-09 |
| 3 | Cleaning and joining | `build/03-cleaning-joining.md` | ✅ complete 2026-08-10 |
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

### Phase 2 — Odds ingestion (2026-08-09)

**Built:** `src/ingest_odds.py` (events → per-event event-odds calls → raw JSON to `data/raw/odds/<stamp>_<event_id>.json` before parsing → append-only `odds_snapshots` inserts), odds section in `config.yaml` (single market `player_points`, us region, american format, 30h horizon), `.env` loader in `src/config.py`, `odds_snapshots` table in `src/db.py`, `run.py update --dry-run` flag. `requests` declared explicitly.

**DoD results:** all pass — live snapshot 2026-08-09T16:04:24Z landed: 238 player_points lines, 4/4 games (Aces@Liberty, Mercury@Mystics, Wings@Lynx, Valkyries@Sparks), 5 books (betrivers 79, betonlineag 44, draftkings 40, williamhill_us 39, fanduel 36); `--dry-run` re-parsed all 4 raw files offline, 0 duplicate inserts; quota printed (4 credits/run, 4,754,475 remaining, projected 480/month at 4 snapshots/day); simulated dead network logs a clean error (API key scrubbed to ***) and exits 1 with no traceback.

**Unresolved:**
- **Capture cadence is manual until phase 10.** The 4-snapshot cadence (open / midday / T-60 / close) exists only as projection math; nothing schedules runs. The closing-line capture — the one that matters most — happens only if `update` is run near tip. Owner note: games tip ~19:00–22:00 ET.
- The API key on this account has a very large quota (4.75M credits remaining) — it appears to be a shared/existing account also used by other projects; WNBA usage (~480/month) is negligible against it.
- Only `player_points` is captured. Adding `player_rebounds`/`player_assists` is a one-line config change but deliberately deferred until the phase 3 join works.
- Odds event ids are not yet mapped to ESPN game_ids (phase 3, via team names + commence_time kept in raw JSON).

### Phase 3 — Cleaning and joining (2026-08-10)

**Built:** `src/clean.py` — `odds_event_map` (event→game via team display names + ET date), persistent `name_map` (exact → team+date → user approvals; fuzzy proposes only), `prop_lines` (every snapshot row 1:1 with match_status, is_whole_line / is_alternate / is_voided flags, actual + over/under/push results), `reports/unmatched.md` every run. `run.py clean` and `run.py audit` wired. Historical backfill added to `update` (`--backfill START END`); ran for Jul 26–Aug 1 (808 lines, 15 events, ~162 credits).

**DoD results:** all pass — unmatched.md written and readable (overall + by book/month/team rates, top-20 names, fuzzy proposals, loss accounting); before/after counts printed at each step; 25 voided rows (scratched/DNP) carry lines but NULL results and are excluded from outcome data; 1,046 odds rows → 1,046 prop_lines rows (nothing dropped). Current state: 779 ok (437 over / 342 under), 25 voided, 238 stats_pending (Aug 2+ games awaiting the lagging stats feed), 4 name_unmatched. **Match rate 99.5%.**

**Unresolved:**
- **Owner approval pending:** books' 'Megan Gustafson' vs ESPN's 'Megan DiLeo' [3934218] — same person per roster context (surname change). Approve by adding `Megan Gustafson,3934218` to `data/external/name_approvals.csv`, then re-run `clean`. Never auto-accepted per spec.
- 238 stats_pending rows resolve automatically: run `update` once the stats feed catches up (lags ~1 week), then `clean`.
- `is_whole_line` and `is_alternate` are all 0 so far — every captured line has been a half-point main line; the flags are live and will populate when alternates are added to config.
- Push-handling policy for evaluation deliberately deferred to phase 9 (see DECISIONS.md).
