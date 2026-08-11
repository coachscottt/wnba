# Restoring from backup

What is backed up, and where:

| Data | Primary | Backup | Replaceable? |
|---|---|---|---|
| `data/wnba.db` | this machine (OneDrive) | committed to GitHub by every collection run | rebuildable, slowly |
| `data/raw/odds/*.json` | this machine (OneDrive) | committed to GitHub | **NO — irreplaceable.** The Odds API's historical coverage varies by plan/book/market; assume a missed snapshot is gone forever |
| `data/raw/stats/*.parquet` | this machine | none | yes — re-downloaded by `update` from the sportsdataverse releases |
| models, reports | this machine | none | yes — `train` / `evaluate` regenerate them |

OneDrive syncing the project folder provides a second copy of everything
independent of GitHub.

## Restore steps (tested 2026-08-11)

1. On any machine: install `uv` (astral.sh/uv) and Git.
2. `git clone https://github.com/<you>/wnba-props.git && cd wnba-props`
   — the clone already contains `data/wnba.db` and the full raw odds archive.
3. `uv sync` (recreates the exact locked environment).
4. Copy `.env.example` to `.env` and add the ODDS_API_KEY.
5. Verify: `uv run python run.py audit` — table counts, match rates, the
   weekly collection summary, and the staleness check should all print.
6. Resume collecting: `uv run python run.py update`.

Full rebuild from nothing (if the database itself were lost): steps 1–4,
then `uv run python run.py update` (stats backfill 2022→today) and
`uv run python run.py clean`. Odds history older than the raw archive in
`data/raw/odds/` CANNOT be rebuilt — re-parse what exists with
`uv run python run.py update --dry-run`.

## Failure notifications

GitHub emails the repository owner when a scheduled workflow run fails,
provided notification settings allow it: github.com → your avatar →
Settings → Notifications → "Actions" → check "Email" (and leave "Only
notify for failed workflows" on). The 36-hour staleness gate inside
`update` is what turns silent collection gaps into failed (red, emailed)
runs.
