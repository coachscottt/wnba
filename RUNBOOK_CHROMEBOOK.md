# WNBA props on the Chromebook — runbook

Same Linux terminal as the soccer setup. First-timer notes live in
soccer's RUNBOOK_CHROMEBOOK.md (paste = Ctrl+Shift+V, `ls` = letters L-S).

## How the pieces split (read this once)
- **GitHub Actions collector** (runs by itself, 3x daily: 12:05 PM, 6:20 PM,
  9:20 PM ET) fetches stats + odds snapshots and commits `data/wnba.db` to
  this repo. It **only collects — it does not run the model.** Its job is
  making sure no line movement is lost on days you skip.
- **You (Chromebook or PC)** run the model: settle yesterday, price today's
  slate. That's the daily block below. GitHub is the source of truth for the
  DB, so you never carry a database file — both machines just `git pull`.

## One-time setup (~5 min)
```
cd ~ && git clone https://github.com/coachscottt/wnba.git && cd wnba
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                    # installs from pyproject.toml, ~2-3 min
grep THE_ODDS_API_KEY ~/soccer/.env | sed 's/THE_ODDS_API_KEY/ODDS_API_KEY/' > .env
python run.py audit                 # smoke test: prints a data-quality report
```
(That `grep ... > .env` line copies the odds key from soccer and renames it
— WNBA calls it `ODDS_API_KEY`, no "THE_". Repo must be public to clone.)

## Daily — RUN THE MODEL (this is exactly what run_daily.bat does on the PC)
```
cd ~/wnba && source .venv/bin/activate
git checkout -- data/wnba.db && git pull      # take the collector's fresh data (discard local db)
python run.py update                          # top up stats + odds to right now
python run.py clean                           # features + SETTLE yesterday's paper bets
python run.py project                         # PRICE today's slate -> console + today_out.csv
git add data/wnba.db data/raw/odds && git commit -m "local collect + slate" && git push
```
`today_out.csv` is the deliverable — the day's priced lines. Then `claude`
in this folder for the board or any questions.

Run it once **before you leave home** as the real smoke test — same idea
as soccer's Korea scan proving the setup end to end.

## Sync rule
Same as rugby, opposite of soccer: **pull before, push after, on whichever
machine you use.** The `git checkout -- data/wnba.db` before pulling is
deliberate — it throws away local db changes because the cloud archive is
the one that can't be rebuilt; everything local is re-derivable.

## If something goes wrong
| You see | Do |
|---|---|
| clone asks for a password | repo is private → make public at github.com/coachscottt/wnba/settings (Danger Zone) |
| `KeyError: ODDS_API_KEY` | `.env` missing/misnamed key — it's `ODDS_API_KEY=` (no THE_) |
| `command not found: python` | `source .venv/bin/activate` |
| `git pull` says conflict on wnba.db | `git checkout -- data/wnba.db && git pull` (collector wins) |
