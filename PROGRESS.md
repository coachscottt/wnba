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
| 4 | Features | `build/04-features.md` | ✅ complete 2026-08-10 |
| 5 | Minutes model | `build/05-minutes-model.md` | ✅ complete 2026-08-10 |
| 6 | Rate models | `build/06-rate-models.md` | ✅ complete 2026-08-10 (all 4 stats) |
| 7 | Simulation | `build/07-simulation.md` | ✅ complete 2026-08-10 |
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

### Phase 4 — Features (2026-08-10)

**Built:** `src/features.py` — 41 as-of features per played player-game across six families: form (trailing 3/5/10 + EWM + shrunk season per-40 for points/reb/ast, games_played, effective shrinkage weight), role (usage, start rate, minutes share, FT rate, 3PA rate, started-tonight), team (pace/ORtg/DRtg, as-of, shrunk k=8), opponent (pace/DRtg k=12, positional defense ratio k=15), situational (rest capped at 7 with >7 as its own `f_long_break` category for the FIBA break, b2b, home, timezones crossed, game number), teammate availability (vacated minutes overall + same-position via as-of trailing minutes of pregame-known absences, primary-ball-handler-out flag). `features` table (24,693 rows) rebuilt by `run.py clean`. `tests/test_leakage.py` runs standalone.

**DoD results:** all pass — leakage test: 50 sampled player-games recomputed from as-of snapshots (future deleted AND same-date stats nulled) across 47 cutoff dates, all 41 features identical; feature summary printed with coverage/mean/sd/min/max/nulls — no feature over 20% nulls (worst 4.2%) and none with zero variance; expansion teams: 583 TOR/POR rows, team features all non-null, game 1 correctly falls back to the shrunk league prior (pace 80.8, ortg 100.9).

**Unresolved:**
- k values (10/8/12/15) are initial guesses by design — tune on validation folds in phases 6/9 (DECISIONS.md carries the reminder).
- Features exist only for historical played rows. Projection-time feature building for tonight's games (roster from a `today_out` override or injury feed, per guide §5.5) is built when `project` arrives (phase 8).
- `f_usage_l5` has a fat tail (max 71 from 1-minute stints inside small windows) — consider minutes-weighting the window when the rate models care.
- `f_started` treated as pregame info; phase 9 should verify measured edge with and without it (capture-time reality check).

### Phase 5 — Minutes model (2026-08-10)

**Built:** Features extended to all 29,421 rostered player-games (backward as-of for non-played rows; `f_min_last`, `f_opp_ortg` added; leakage test still passes). `src/model_minutes.py` — two-stage: isotonic-calibrated HistGradientBoosting DNP classifier + share regressor + full Dirichlet over playing players per simulation, share×200 (+25 only on explicit OT). Concentration fit by matching interval coverage on a late-train calibration slice (c=192; the MLE approach landed c≈89 and failed coverage — see DECISIONS.md). `run.py train` evaluates on the holdout, refits on all data, saves `models/minutes.pkl` (gitignored). `tests/test_minutes_sum.py` standalone.

**DoD results, holdout n=3,304 (2026-06-01+, regulation, time-split):**

| | MAE (minutes) |
|---|---|
| model (sim median) | **4.19** |
| baseline: trailing-5 mean | 4.76 |
| baseline: last game | 5.11 |

Coverage 50% → 56.0%, 80% → 82.4%, 95% → 94.2% (all within tolerance; slightly wide at 50). DNP log loss 0.2099, calibration deciles track (plot at reports/minutes_dnp_calibration.png). Sum test: 100 team-games × 50 sims, exactly 200 in regulation, 225 only with explicit OT, no negatives. `grep 36` over src/ is clean. **Verdict: beats trailing-5 on MAE with calibrated coverage — proceed permitted.**

**Unresolved:**
- Backtest knows historical pregame availability (printed in every train run). The projection-time gap is real and unmeasured until phase 10's `today_out.csv` workflow.
- 50% coverage is 6pts wide — the Dirichlet's single global c can't sharpen established-starter minutes and bench volatility simultaneously; a role-dependent concentration is the natural phase 7 refinement if points tails misbehave.
- Blowout proxy is ratings-based; game spread/total markets (capturable via the odds API) would be strictly better once phase 8 pulls game lines.
- No game-level OT model yet — phase 7 decides whether props settle including OT and simulates accordingly.

### Phase 6 — Rate models (2026-08-10)

**Built:** Features extended (form stats for fg3a/fg2a/fta, as-of make/attempt counters for Beta accuracy shrinkage, team-level `f_opp_allow_*` factors, raw-season baselines; 75 features total, leakage test passes). `src/model_rates.py` — EB means (szn-shrunk × opp-allow × pace), NB counts with moment-fit dispersion, Beta-shrunk accuracies, points strictly composed from 2PM/3PM/FTM in simulation. Stats built and evaluated sequentially per spec. `run.py train` now trains minutes then rates; `models/rates.pkl` saved.

**DoD results, holdout n=3,049 played rows (2026-06-01+, actual-minutes-conditioned):**

| stat | var/mean (NB justification) | CRPS model | szn-raw | trail-10 | PIT |
|---|---|---|---|---|---|
| 3PM | 2.67 (attempts) | **0.4359** | 0.4432 | 0.4446 | flat |
| REB | 2.88 | **1.0062** | 1.0174 | 1.0284 | flat |
| AST | 2.41 | **0.7652** | 0.7682 | 0.7808 | flat |
| PTS (composed) | 6.40 (direct, baselines only) | **2.3907** | 2.4277 | 2.4508 | flat |

All overdispersion ratios ≫ 1 — Poisson would have been wrong everywhere, as the guide warned. p3 shrinkage weight at holdout: p10 0.03 / median 0.34 / p90 0.61 (the Beta prior does most of the work for low-volume shooters). **Verdict: all four stats beat both baselines — proceed permitted.**

**Unresolved:**
- The raw 3PA *attempts* model slightly loses to the season-raw baseline on CRPS (0.7854 vs 0.7559) even though 3PM and points — the priced quantities — win. Likely the opp/pace adjustments add noise on attempts; worth an ablation in phase 9.
- FTA dispersion is extreme (r=2.9) — free-throw attempts are the least predictable component; drives the points tails, watch it in phase 7 calibration.
- Accuracy counters (p3/p2/ftp) are season-scoped; career-scoped counters would stabilize early-season estimates further.

### Phase 7 — Simulation (2026-08-10)

**Built:** `src/simulate.py` — full joint engine (shared pace + OT draws per game, vectorized Dirichlet minutes, NB attempts × Binomial makes, points + PRA/PR/PA/RA derived per sim), 10k sims/game seeded, summaries (mean/sd/P≥k per unit) stored in `sim_summary`, raw draws discarded. Run with `python -m src.simulate` (155 holdout games).

**The overlay check earned its keep.** First run showed stars underprojected ~20% (A'ja Wilson sim 22.0 vs actual 26.8) — phase 4's k=10 shrinkage was crushing high-rate players, invisible to phase 6's all-player CRPS gate. Fixed by per-stat k tuning on the validation slice (k now 1–3; see DECISIONS.md); the fix also made the 3PA attempts submodel beat its baselines (closing that phase 6 unresolved). All phase 6 CRPS margins improved after re-tune (points 2.3384 vs 2.4277/2.4508).

**DoD results:** minutes sum asserted in every sim of every game (200 exact, +25 only on simulated OT); team points sim-vs-actual mean error −2.0 (MAE 10.3, n=310 team-games — no game-total lines captured, compared to realized scores); team rebounds 33.6/team (realistic); P(points≥k) grids printed around 8 posted lines; overlay plots for the 5 most-priced players saved to reports/overlay_*.png.

**Unresolved — the top item for phase 9:**
- **Star-tier residual: top scorers still sim ~8–10% under their holdout-period scoring** (Wilson 24.8 vs 27.6, Stewart 19.0 vs 22.5, Ionescu 13.3 vs 17.0 conditional-on-playing; role players are calibrated). Decomposition: Wilson's minutes are right → pure rate lag; Stewart/Ionescu are ~2 minutes under AND rate-lagged (season-cumulative rates lag mid-season ramps; Ionescu is an injury-return ramp). Candidate fixes for phase 9 adjudication: star-tier minutes-share bias in the HGB regressor, Beta accuracy pseudo-counts (k_p2=80) possibly over-shrinking high-volume shooters, league-wide −2.3% scoring drift (as-of estimates lag a rising scoring environment). **Pricing consequence if unfixed: the model will systematically fade star overs — phase 9's CLV-vs-market comparison is the right instrument to size this.**
- Usage-share redistribution beyond minutes-flow is deferred (DECISIONS.md).
- Overlay comparison is holdout-sims vs season log by construction; holdout-period actuals are printed alongside for honesty.
