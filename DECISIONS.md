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
