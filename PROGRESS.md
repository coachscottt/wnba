# Progress

**Agent: read this first. Build the first phase not marked complete, then stop.**

Mark a phase complete only when its Definition of Done checks pass and the user
has seen the output.

| # | Phase | Spec | Status |
|---|---|---|---|
| 0 | Environment and scaffold | `build/00-setup.md` | ✅ complete 2026-08-06 |
| 1 | Stats ingestion | `build/01-stats-ingestion.md` | ⬜ not started |
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
