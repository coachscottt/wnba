# Decisions

Append-only. One entry per choice that could reasonably have gone another way.
Future-you and future-agent will not remember why, and the guide's defaults are
starting points, not conclusions.

Format:

```
## YYYY-MM-DD — <the choice>
**Decided:** what was chosen
**Alternatives:** what else was considered
**Why:** the reasoning
**Revisit if:** the condition that would change this
```

<!-- Agent: append new decisions below. Do not edit existing entries. -->

## 2026-08-06 — Zero dependencies in phase 0
**Decided:** `pyproject.toml` ships with an empty dependency list. `run.py` and the logging helper use only the standard library. `config.yaml` exists but nothing parses it yet.
**Alternatives:** Add PyYAML now so run.py loads config at startup.
**Why:** The standing rules require asking before adding any package, and phase 0 has no code that needs config values. PyYAML (and python-dotenv) will be proposed in phase 1, the first phase that reads config and secrets.
**Revisit if:** never — phase 1 supersedes this.

## 2026-08-06 — Phase numbers in not-implemented messages
**Decided:** update→1, clean→3, train→5, project→8, evaluate→9, audit→3.
**Alternatives:** update→2 (odds), train→6 (rates), project→7 (simulation).
**Why:** Each command points at the first phase that delivers a usable version of it: stats ingestion makes `update` real (odds extends it in 2), the minutes model is the first thing `train` fits (rates extend it in 6), and `project` needs pricing (8) before its output means anything.
**Revisit if:** a phase spec assigns a command differently when it arrives.

## 2026-08-06 — Python 3.12 interpreter
**Decided:** `uv sync` resolved to the machine's existing CPython 3.12.1; `requires-python = ">=3.11"` per the spec.
**Alternatives:** Pin 3.11 or have uv download 3.13.
**Why:** 3.12 satisfies the constraint, is already installed, and avoids an extra download. All planned libraries support it.
**Revisit if:** a dependency in a later phase lacks a 3.12 wheel.

## 2026-08-09 — Packages added: sportsdataverse, pyyaml
**Decided:** `uv add sportsdataverse pyyaml`. sportsdataverse is named by the phase 1 spec as the data source; PyYAML is required the moment code reads `config.yaml`.
**Alternatives:** Direct ESPN/stats.wnba.com calls with `requests` (no heavy package); JSON config instead of YAML.
**Why:** The spec mandates sportsdataverse and its cached loaders pull five seasons in seconds. It drags in large transitive deps (pandas 3.0, polars, pyarrow, scikit-learn, xgboost) — accepted since later phases need the scientific stack anyway.
**Revisit if:** the package breaks on a pandas/polars upgrade; fallback is direct ESPN calls, which its loaders wrap.

## 2026-08-09 — Raw saved as parquet, not JSON
**Decided:** Raw feeds go to `data/raw/stats/{feed}_{season}.parquet`, overwritten per fetch, written to disk before any parsing.
**Alternatives:** Convert to JSON per the AGENTS.md raw-JSON rule; keep dated copies per fetch.
**Why:** The upstream release files ARE parquet — saving them verbatim is the rawest form, and JSON conversion would inflate ~10× inside a OneDrive-synced folder. Overwriting is safe because the release files are season-cumulative (append-only); dated snapshots matter for odds, not stats. The rule's intent — reparse without refetch — is preserved.
**Revisit if:** odds ingestion (phase 2) — those responses are JSON and volatile, so the JSON + timestamped-filename rule applies there in full.

## 2026-08-09 — Seasons 2022–2026
**Decided:** Backfill five seasons, `first_season: 2022` in config.
**Alternatives:** 2002-onward (available), or 2026 only.
**Why:** Rate priors want several seasons; pre-2026 minutes priors are suspect anyway under the new CBA (guide §2), so deeper history adds little for a minutes model and slows every rebuild.
**Revisit if:** phase 6 shrinkage wants deeper rate history.

## 2026-08-09 — Possession formula coefficient 0.44
**Decided:** `POSS = FGA − OREB + TOV + 0.44 × FTA` per team-game, using `total_turnovers` (falls back to `turnovers`). Game possessions = mean of the two teams; pace normalized per 40 minutes with OT adjustment.
**Alternatives:** Derive the FT coefficient from play-by-play possession endings.
**Why:** 0.44 is an NBA-derived approximation and is not exactly right for the WNBA — the spec says use it anyway and note it. Deriving it properly requires parsing PBP, deferred (below).
**Revisit if:** play-by-play is ingested; then count actual possession-ending FTs and put the derived coefficient in config.

## 2026-08-09 — Play-by-play deferred
**Decided:** Phase 1 ingests schedule + player box + team box only; no PBP.
**Alternatives:** The spec's source line mentions pulling PBP alongside box scores.
**Why:** No phase-1 table needs PBP; its only near-term consumer is the rigorous FT coefficient (explicitly deferrable per guide §5.3). Five seasons of PBP is hundreds of MB inside OneDrive sync for zero current use.
**Revisit if:** the derived FT coefficient or shot/lineup features are wanted — likely phase 4+.

## 2026-08-09 — Minutes-sum tolerance ±3
**Decided:** The 200 + 25/OT team-minutes check fails only beyond ±3 (config `minutes_sum_tolerance`).
**Alternatives:** Strict equality (fails 1,350 of 2,594 team-games).
**Why:** ESPN player minutes are integer-rounded; observed deviations are tightly bounded (−3…+3, mode 0) — rounding noise, not dropped players (a dropped rotation player shows −10 or worse).
**Revisit if:** a deviation beyond ±3 appears — that is a real parsing bug, not rounding. Known blind spot: a dropped 1–3-minute player is masked; the points/rebounds reconciliation checks partially cover this.

## 2026-08-09 — Commissioner's Cup final excluded from the 44-game cap
**Decided:** Games whose schedule note matches "Commissioner's Cup Championship" (config `cup_final_note`) are kept in all tables but excluded from the games-per-team ≤ 44 validation. Group-stage Cup games count normally.
**Alternatives:** Drop the Cup final entirely; store an event flag column.
**Why:** ESPN codes the Cup final as season_type 2, giving the two finalists 45 "regular-season" games (2025: Lynx/Fever). It is a real game with real minutes — valuable for modeling — it just isn't part of the 44-game schedule.
**Revisit if:** phase 4+ features want an explicit is_cup_final flag rather than a validation-side exclusion.

## 2026-08-09 — DNP reason → availability status mapping
**Decided:** `COACH'S DECISION` → dnp_coach; REST/LOAD MANAGEMENT → dnp_rest; body-part/injury/illness keywords → dnp_injury; everything else (NOT WITH TEAM, PERSONAL, no reason) → inactive. `not_on_roster` is never emitted in phase 1.
**Alternatives:** A lookup table of exact reason strings.
**Why:** ESPN reasons are free text ("RIGHT KNEE", "INJURY/ILLNESS"); keyword classes cover all 15 observed values. Box scores only list rostered players, so not_on_roster cannot be derived from this source.
**Revisit if:** phase 2/3 adds roster or injury-report data that can populate not_on_roster; unknown new reason strings default safely to inactive.

## 2026-08-09 — All-Star teams excluded by abbreviation
**Decided:** Schedule rows involving COOP/SPO (config `exclude_abbreviations`) are dropped with a reported count.
**Alternatives:** Filter on is_active flags (they are true for All-Star teams, so useless).
**Why:** The 2026 All-Star game (TEAM COOP vs TEAM SPOON) sits in the feed as season_type 2 and would pollute games/player_games with exhibition stats.
**Revisit if:** future seasons use different event-team abbreviations — add them to the config list.

## 2026-08-09 — Phase 2 packages: requests declared, python-dotenv skipped
**Decided:** `uv add requests` (it was already installed transitively via sportsdataverse; now it's an explicit, pinned dependency of our own code). `.env` is read by a ten-line parser in `src/config.py` instead of python-dotenv.
**Alternatives:** python-dotenv; relying on the transitive requests.
**Why:** Depending on a transitive package is fragile — an upstream change could silently remove it. python-dotenv adds a package for functionality that is ten lines of stdlib; the ask-before-adding rule biases against it.
**Revisit if:** .env needs quoting/multiline/interpolation semantics — then switch to python-dotenv rather than growing the hand parser.

## 2026-08-09 — Odds stored as American integers
**Decided:** `odds_format: american` in config; over/under prices stored as integers (e.g. -115).
**Alternatives:** Decimal floats.
**Why:** The guide requires picking one consistently in config. American integers round-trip exactly (no float representation noise) and match what US books display; de-vigging in phase 8 converts to implied probability regardless of format.
**Revisit if:** a non-US region's books are added and decimal comparison becomes the norm.

## 2026-08-09 — Snapshot identity and idempotent parsing
**Decided:** One UTC timestamp per run stamps every row and raw filename (`<stamp>_<event_id>.json`). `odds_snapshots` has a UNIQUE index on (captured_at_utc, game_id, book, market, player_name_raw, line, is_alternate) with INSERT OR IGNORE.
**Alternatives:** Per-request timestamps; an unconstrained append table.
**Why:** A run-level stamp groups a slate's captures into one logical snapshot (the fetch loop spans seconds). The natural-key index makes `--dry-run` re-parsing idempotent — re-reading a raw file can never duplicate rows — while still keeping every distinct alternate line.
**Revisit if:** intra-run price movement ever matters (it won't at 4 snapshots/day).

## 2026-08-09 — odds_snapshots.game_id is the provider's event id
**Decided:** Store The Odds API event id untouched; no join to ESPN game_ids yet.
**Alternatives:** Match on team names + date at ingest time.
**Why:** Name/id mapping is phase 3's job by design, and the raw JSON keeps home/away team names and commence_time for the join. Ingest stays dumb and lossless.
**Revisit if:** never — phase 3 builds the mapping into separate columns/tables.

## 2026-08-10 — ESPN dates are already Eastern; only odds need conversion
**Decided:** `games.game_date` is used directly as the Eastern game date. The odds side derives `game_date_et` from `commence_time` (UTC) via `zoneinfo America/New_York`.
**Alternatives:** Re-deriving ET dates on both sides.
**Why:** Verified in the raw schedule parquet: `game_date_time` is timezone-aware `America/New_York`, and `game_date` equals its ET calendar date even for Portland's 22:00 ET tips (which are already next-day in UTC — the exact trap §7.4 warns about).
**Revisit if:** the feed ever ships naive or UTC datetimes — the West-Coast date check in validation would start failing.

## 2026-08-10 — Historical odds backfill for join validation (~162 credits)
**Decided:** `run.py update --backfill START END` fetches historical near-tip snapshots (10x credit cost). Ran once for 2026-07-26..08-01: 808 lines, 15 events. Snapshot times 15:30Z + 23:00Z per day (config `backfill_times_utc`).
**Alternatives:** Wait weeks for the live archive to accumulate against completed stats.
**Why:** Phase 3's join cannot be trusted without real matched rows, and the stats feed lags a week — live snapshots alone would leave every row `stats_pending`. Two timestamps per day because the historical events list omits games that already tipped, so a single evening snapshot misses afternoon slates.
**Revisit if:** a deeper historical backfill is wanted for phase 9 evaluation — same command, longer range, budget accordingly (~30 credits/game-day).

## 2026-08-10 — Name cascade: exact and team+date auto-match; fuzzy only proposes
**Decided:** Exact normalized match (incl. "Last, First" flip) auto-maps at confidence 1.0; unique initial+surname match on the game's roster auto-maps at 0.9 (`team_date`); everything else generates printed proposals — rapidfuzz candidates, plus same-first-name roster members when fuzzy finds nothing (catches surname changes). Approvals live in `data/external/name_approvals.csv` (committed), applied as `user`/1.0, forever.
**Alternatives:** Auto-accepting high fuzzy scores.
**Why:** The spec forbids auto-accepted fuzzy matches. The roster/first-name fallback exists because of a real case: books list 'Megan Gustafson', ESPN lists 'Megan DiLeo' — no string similarity, same person (surname change), only roster context finds her.
**Revisit if:** two same-named players ever collide (the `ambiguous_exact` path already refuses to map them globally; they would need per-game mapping).

## 2026-08-10 — prop_lines semantics: voided, pushes, statuses
**Decided:** Every odds snapshot row lands in `prop_lines` (1:1, enforced by row-count print). `voided` = name and game matched but no clean played box-score row (DNP/scratched/absent): line kept, `actual`/`result` NULL — never an under. `result='push'` when actual equals a whole line; `is_whole_line` flagged for all integer lines. `stats_pending` = game newer than ingested stats; excluded from the match-rate denominator and resolves on the next `clean` after stats catch up.
**Alternatives:** Dropping unmatched rows; treating scratched players' lines as settled.
**Why:** Guide §7.5 — the scratched-star-becomes-an-under bug is catastrophic and silent. Push handling for evaluation (exclude vs three-way) is deliberately deferred to phase 9, where the metric choice makes it concrete; the data model already carries what it needs.
**Revisit if:** phase 9 — write the push policy down there.

## 2026-08-10 — rapidfuzz declared explicitly
**Decided:** `uv add rapidfuzz` (was already installed transitively).
**Alternatives:** difflib (stdlib).
**Why:** Our code imports it directly for fuzzy proposals; same transitive-fragility argument as requests. difflib's ratio quality on names is noticeably worse.
**Revisit if:** never, realistically.

## 2026-08-10 — Rate model structure (phase 6)
**Decided:** Empirical-Bayes means, explicit count families, no ML regressor: per-40 mean = feature-layer EB season rate (league→position→player) × opponent team-level allow-factor (as-of, shrunk k=15) × pace factor. Counts (3PA/2PA/FTA/REB/AST) are Negative Binomial with moment-fit dispersion; makes are Binomial with Beta-shrunk accuracy (k_p3=60, k_p2=80, k_ftp=40 pseudo-attempts); points strictly composed as 2×2PM + 3×3PM + FTM in simulation; combos never fitted. Opponent adjustments team-level only — no player-vs-team interactions (a player faces a given team ~4 times a season; there is no sample).
**Alternatives:** Poisson (observed variance/mean: reb 2.88, ast 2.41, fg3a 2.67, fg2a 3.56, fta 3.23 — all clearly overdispersed, Poisson would be too narrow and make low lines look like free overs); gradient boosting for rate means (kept for minutes where interactions dominate; rates favor the interpretable EB structure the guide specifies); full MCMC (start with EB per guide §10.3).
**Why:** Matches guide §10 exactly; every dispersion is justified by a printed variance/mean ratio.
**Revisit if:** phase 9 shows a specific stat's mean model lagging — that's where a regressor on top of EB earns its complexity.

## 2026-08-10 — Rate evaluation conditions on actual minutes
**Decided:** CRPS/PIT for rate models are computed given the minutes actually played; baselines (season-to-date raw rate, trailing-10 rate) get the same distribution family and dispersion, differing only in the mean rate.
**Alternatives:** Evaluating through the full minutes×rate pipeline (that is phase 7's composed evaluation).
**Why:** Isolates rate quality from minutes uncertainty — otherwise a good rate model hides behind minutes noise. Baselines sharing the family isolates exactly the thing being claimed: the mean structure (shrinkage + opponent + pace) adds information.
**Revisit if:** never — phase 7 evaluates the composition.

## 2026-08-10 — Phase 7: rate shrinkage re-tuned after the overlay check caught star bias
**Decided:** Rate-model means re-shrink from raw + as-of league prior with per-stat k tuned by CRPS on a late-train validation slice (chosen: fg3a k=1, fg2a k=1, fta k=3, reb k=3, ast k=2 — far below the feature layer's k_form=10), plus an optional EWM recent-form blend (grid-tuned, mostly small). The feature layer's k_form=10 is unchanged (the minutes model validated with it).
**Alternatives:** Keeping k=10 (the overlay check showed stars underprojected ~20% — A'ja Wilson sim 22.0 vs actual 26.8 — because a 31-per-40 scorer was still pulled 27% toward a ~15 league prior); Dirichlet-style MLE (same μ-error conflation as phase 5).
**Why:** The guide's overlay check is "the best debugging tool in the project" and it worked exactly as advertised: phase 6's all-player CRPS gate couldn't see a star-tier bias because stars are few — but stars are where props are priced. Tuning k per stat on validation (the tuning phases 4/6 deferred) halved the bias and also fixed the 3PA attempts submodel (now beats baselines: 0.7426 vs 0.7555).
**Revisit if:** phase 9 — see the star-tier residual below.

## 2026-08-10 — Simulation engine structure
**Decided:** Per game: shared pace draw (Normal, residual sd fit pre-holdout = 3.87) and shared OT draw (P=0.038 fit pre-holdout) for both teams; DNP coin flips; vectorized Dirichlet minutes shares (per-element gamma draws) × (200 + 25·OT); NB attempts scaled by that sim's minutes × pace; Binomial makes; points/PRA/PR/PA/RA derived per sim. Attempt redistribution when a teammate sits flows through the minutes reallocation (shares renormalize, attempts follow minutes) — an explicit usage-share layer would double-count it. Summaries stored (mean, sd, P(≥k) at 1-unit intervals in `sim_summary`), never raw draws. Props settle including OT, so OT sims are included.
**Alternatives:** Per-player independent sims (loses the zero-sum minutes and shared-pace correlations that combos need).
**Why:** Guide §11 structure exactly; correlations come free.
**Revisit if:** teammate-out repricing looks wrong in practice — then a usage model conditioned on the active lineup is the upgrade.

## 2026-08-10 — Shrinkage constants (phase 4 initial values)
**Decided:** `k_form: 10`, `k_team: 8`, `k_opp: 12`, `k_opp_pos: 15` in config.yaml, applied as (n·obs + k·prior)/(n+k).
**Alternatives:** Small k (chases hot streaks — guide explicitly warns against k=1); tuning now.
**Why:** These are deliberate starting points sized to a 44-game season: a player's season-to-date rate only outweighs the prior after ~10 games; opponent effects (15 teams, tiny samples) shrink hardest. Proper tuning needs validation folds, which exist from phase 6/9 — retune there. Current effective form weights: p10 0.17 / median 0.66 / p90 0.75.
**Revisit if:** phase 6/9 validation — this entry is the reminder.

## 2026-08-10 — Feature scope choices
**Decided:** (a) Form windows and season-to-date reset per season; teammate-availability trailing minutes and ball-handler ast/40 use career windows crossing seasons. (b) Shrinkage priors are as-of league-position averages with previous-season fallback and config defaults at the 2022 boundary. (c) `out_statuses` for vacated-minutes/ball-handler-out = dnp_injury, dnp_rest, inactive — **dnp_coach excluded** (a coach's decision is not pregame-knowable; counting it would leak). (d) `f_started` for game N is treated as pregame info (confirmed lineups are public before tip). (e) Shot mix = 3PA-rate + FT-rate only — rim/mid split needs play-by-play, which is deferred. (f) Features rebuild inside `run.py clean`; the leakage test is a standalone script (`python -m tests.test_leakage`), avoiding a pytest dependency.
**Alternatives:** Within-season vacated minutes (bad early-season estimates); including dnp_coach as an absence; pytest.
**Why:** Each follows the leakage rule or the ask-before-adding-packages rule; cross-season vacated minutes because "the star's usual minutes" is best estimated from last season in game 1.
**Revisit if:** projection-time reality differs — e.g. lineups not confirmed at capture time makes (d) optimistic; phase 9 should measure with and without `f_started`.

## 2026-08-10 — Features extended to all rostered players (phase 5 prerequisite)
**Decided:** The features table now carries every rostered player-game (29,421 rows) with a `status` column. Played rows use shifted windows; non-played rows get identical as-of values via a backward as-of join on the player's played history (their own game is never in that history, so "last played value" = everything strictly before tip). Added `f_min_last` (last game's minutes — also the phase 5 baseline) and `f_opp_ortg` (for the blowout proxy).
**Alternatives:** A separate reduced feature set for DNP rows.
**Why:** The DNP classifier's positive class is dnp_coach rows, which had no feature rows; and phase 8 projection needs features for full rosters anyway. The leakage test now covers non-played rows too and still passes.
**Revisit if:** never — this is the architecture phase 8 builds on.

## 2026-08-10 — Minutes model: Dirichlet shares with coverage-fit concentration
**Decided:** Two-stage as specced: HistGradientBoosting DNP classifier with isotonic recalibration on a time-ordered late-train slice, HistGradientBoosting regressor for expected minutes SHARE, full Dirichlet over the players who play in each simulation (not the normalize-independent-draws fallback), share × 200 (+25 only on explicit OT). Concentration c chosen by matching 50/80/95 interval coverage on the calibration slice — **not** by Dirichlet MLE.
**Alternatives:** MLE for c (landed c≈89 → intervals far too wide, 50% interval covering 62-66%); fitting c on recent-era subsets (2025+: no change; 2026-only: worse — season-start rotations are the noisiest).
**Why:** The likelihood fit conflates the regressor's μ-error with true rotation dispersion, systematically underestimating c. Coverage is the quantity that must be calibrated (props live in the tails), so fit it directly on data the holdout never sees. Result: c=192, holdout coverage 56/82/94 vs nominal 50/80/95.
**Revisit if:** phase 7 simulation shows tail problems on points (minutes tails propagate); or a game-total/spread market feature replaces the ratings-based blowout proxy.

## 2026-08-10 — Minutes evaluation choices
**Decided:** Holdout = 2026-06-01 onward (time-based, ~3.3k player-games), regulation games only; evaluation population = pregame-available players (played + dnp_coach); baseline comparison on MAE with model interval coverage reported alongside (point-estimate baselines have no intervals); backtest explicitly labeled as knowing historical pregame availability.
**Alternatives:** Including OT games (actual minutes incompatible with the 200-minute frame — OT simulation is phase 7's job); random splits (never).
**Why:** Each mirrors the spec; the availability caveat is the guide's "real result vs fantasy" distinction, printed in every train run.
**Revisit if:** phase 7 adds the game-level OT event model.

## 2026-08-10 — Packages: scikit-learn, scipy, numpy, matplotlib declared
**Decided:** `uv add scikit-learn scipy numpy matplotlib` — all were already installed transitively; our code now imports them directly.
**Alternatives:** xgboost (also present transitively) — HistGradientBoosting is sklearn-native, handles NaN features without imputation (which sidesteps a whole class of imputer-leakage), and is fast enough.
**Why:** Same explicit-dependency rule as requests/rapidfuzz. matplotlib is sanctioned by the repo's own `.gitignore` (`reports/*.png`) and the guide's layout (calibration plots in reports/).
**Revisit if:** never, realistically.
