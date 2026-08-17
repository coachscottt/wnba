# WNBA props on the Chromebook — runbook

Same Linux terminal as the soccer setup. First-timer notes live in
soccer's RUNBOOK_CHROMEBOOK.md (paste = Ctrl+Shift+V, `ls` = letters L-S).

## Why this one is easy
A GitHub Actions collector runs 3x daily on its own (12:05 PM, 6:20 PM,
9:20 PM ET) and commits `data/wnba.db` + odds snapshots to this repo.
**GitHub is the source of truth for the database** — you never carry a
DB file. Both machines just `git pull`.

## One-time setup
```
cd ~ && git clone https://github.com/coachscottt/wnba.git && cd wnba
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                    # installs from pyproject.toml, ~2-3 min
cp .env.example .env && nano .env   # paste ODDS_API_KEY=... (same key as soccer's THE_ODDS_API_KEY), Ctrl+O Enter Ctrl+X
python run.py audit                 # smoke test: prints a data-quality report
```

## Daily (this is exactly what run_daily.bat does on the PC)
```
cd ~/wnba && source .venv/bin/activate
git checkout -- data/wnba.db && git pull      # cloud collector is canonical: discard local db, pull latest
python run.py update                          # stats + fresh odds snapshot
python run.py clean                           # rebuild joins/features + SETTLE yesterday's paper bets
python run.py project                         # today's slate -> console + today_out.csv
git add data/wnba.db data/raw/odds && git commit -m "local collect + slate" && git push
```
Then `claude` in this folder for the board / any questions.

## Sync rule
Same as rugby, opposite of soccer: **pull before, push after, on whichever
machine you use.** The `git checkout -- data/wnba.db` line before pulling
is deliberate — it throws away local db changes because the cloud archive
is the one that can't be rebuilt. Everything local is re-derivable.
