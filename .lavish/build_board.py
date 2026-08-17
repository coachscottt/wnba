"""Build the Lavish slate board from reports/slate_<date>.csv + db context.

Usage: python .lavish/build_board.py [YYYY-MM-DD]  (default: latest slate csv)
"""

import csv
import html
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
conn = sqlite3.connect(ROOT / "data" / "wnba.db")
conn.row_factory = sqlite3.Row

if len(sys.argv) > 1:
    date = sys.argv[1]
else:
    date = max(ROOT.glob("reports/slate_*.csv")).stem.removeprefix("slate_")

STAT = {"player_points": "PTS", "player_rebounds": "REB", "player_assists": "AST"}

stamp = conn.execute("SELECT MAX(captured_at_utc) m FROM prop_lines").fetchone()["m"]
games = {r["event_id"]: f'{r["away_team_raw"]} @ {r["home_team_raw"]}'
         for r in conn.execute("SELECT * FROM odds_event_map")}
player_game = {}
for r in conn.execute(
        "SELECT DISTINCT pl.player_id, p.player_name, pl.event_id "
        "FROM prop_lines pl JOIN players p ON p.player_id = pl.player_id "
        "WHERE pl.captured_at_utc = ?", (stamp,)):
    player_game[r["player_name"]] = games.get(r["event_id"], "?")

rows = list(csv.DictReader(open(ROOT / "reports" / f"slate_{date}.csv",
                                encoding="utf-8")))
n_bets = sum(1 for r in rows if r["bet_flag"] == "1")
# both sides of a market are priced (each has its own best price/EV), but a
# board row should show only the side the model actually leans toward —
# the mirror side's edge is just the negative twin
best_side = {}
for r in rows:
    key = (r["player"], r["market"], r["line"])
    if key not in best_side or float(r["edge"]) > float(best_side[key]["edge"]):
        best_side[key] = r
rows = list(best_side.values())
n_games = len({player_game.get(r["player"], "?") for r in rows})
by_stat = {}
for r in rows:
    by_stat.setdefault(STAT.get(r["market"], "?"), []).append(r)
age_h = (datetime.now(timezone.utc)
         - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds() / 3600

def badge(side):
    cls = "badge-info" if side == "over" else "badge-warning"
    return f'<span class="badge badge-sm {cls}">{side.upper()}</span>'

def stat_badge(market):
    return f'<span class="badge badge-sm badge-neutral">{STAT.get(market, "?")}</span>'

def edge_cell(e):
    e = float(e)
    color = ("text-error font-bold" if abs(e) >= 0.15
             else "text-warning font-semibold" if abs(e) >= 0.08
             else "text-base-content")
    note = " ⚠" if abs(e) >= 0.15 else ""
    return f'<span class="{color}">{e:+.1%}{note}</span>'

# one table per stat category, each sorted by |edge|
STAT_ORDER = ["PTS", "REB", "AST"]
STAT_TITLE = {"PTS": "Points", "REB": "Rebounds", "AST": "Assists"}

def row_html(r):
    gm = player_game.get(r["player"], "")
    flags = []
    if r["is_whole_line"] == "1":
        flags.append('<span class="badge badge-ghost badge-xs">push poss.</span>')
    return f"""<tr class="hover">
      <td class="font-medium whitespace-nowrap">{html.escape(r['player'])}
        <div class="text-xs opacity-60">{html.escape(gm)}</div></td>
      <td class="text-right font-mono">{r['line']}</td>
      <td>{badge(r['side'])}</td>
      <td class="text-right font-mono">{int(r['best_price']):+d}
        <div class="text-xs opacity-60">{html.escape(r['book'])} ({r['n_books']} bks)</div></td>
      <td class="text-right font-mono">{float(r['model_p']):.1%}</td>
      <td class="text-right font-mono">{float(r['fair_p']):.1%}</td>
      <td class="text-right font-mono">{edge_cell(r['edge'])}</td>
      <td class="text-right font-mono">{float(r['kelly_frac']):.1%}</td>
      <td>{' '.join(flags)}</td>
    </tr>"""

sections = []
for st in STAT_ORDER:
    srows = sorted(by_stat.get(st, []), key=lambda x: -abs(float(x["edge"])))
    if not srows:
        continue
    n_flag = sum(1 for r in srows if r["bet_flag"] == "1")
    sections.append(f"""<div class="card bg-base-100 shadow" id="sec-{st}">
      <div class="card-body p-4">
        <h2 class="card-title text-base">{STAT_TITLE[st]}
          <span class="badge badge-neutral badge-sm">{len(srows)} markets</span>
          <span class="badge badge-ghost badge-sm">{n_flag} logged</span></h2>
        <div class="overflow-x-auto">
          <table class="table table-sm table-zebra">
            <thead><tr>
              <th>Player / game</th><th class="text-right">Line</th><th>Side</th>
              <th class="text-right">Best price</th>
              <th class="text-right">Model P</th><th class="text-right">Fair P</th>
              <th class="text-right">Edge</th><th class="text-right">¼-Kelly</th><th></th>
            </tr></thead>
            <tbody>{''.join(row_html(r) for r in srows)}</tbody>
          </table>
        </div>
      </div></div>""")
nav = " · ".join(f'<a class="link" href="#sec-{st}">{STAT_TITLE[st]} ({len(by_stat.get(st, []))})</a>'
                 for st in STAT_ORDER if by_stat.get(st))

stat_summary = " · ".join(f"{k}: {len(v)}" for k, v in sorted(by_stat.items()))

page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WNBA Props Slate — {date}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/daisyui@5.5.19/daisyui.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/daisyui@5.5.19/themes.css">
<script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4.2.4/dist/index.global.js"></script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  :where(.grid, .flex) > * {{ min-width: 0; }}
  :where(td, th, .badge) {{ overflow-wrap: anywhere; }}
</style>
</head>
<body class="bg-base-200 min-h-screen">
<div class="max-w-6xl mx-auto p-6 space-y-5">

  <header class="space-y-1">
    <h1 class="text-2xl font-bold">WNBA Props Slate — {date}</h1>
    <p class="text-sm opacity-70">Snapshot {stamp} ({age_h:.1f}h old) ·
    {len(rows)} markets (model's side shown) across {stat_summary}</p>
  </header>

  <div role="alert" class="alert alert-warning">
    <span><b>Diagnostic, not a betting card.</b> Rows ⚠ above 15% edge are
    near-certainly model error. Extra caution on <b>REB</b>: the one stat where the
    model lost to its baseline in phase 9 — tonight's top edges cluster there, and
    the paper book is measuring exactly that. No stakes until CLV proves out.</span>
  </div>

  <div class="stats stats-vertical sm:stats-horizontal shadow bg-base-100 w-full">
    <div class="stat"><div class="stat-title">Markets priced</div>
      <div class="stat-value text-2xl">{len(rows)}</div>
      <div class="stat-desc">{n_games} games, 3 markets</div></div>
    <div class="stat"><div class="stat-title">Paper-logged tonight</div>
      <div class="stat-value text-2xl">{n_bets}</div>
      <div class="stat-desc">risk/win $100, data only</div></div>
    <div class="stat"><div class="stat-title">Book to date</div>
      <div class="stat-value text-2xl">164W–129L</div>
      <div class="stat-desc">net +$2,429 · CLV +9¢ (n=244)</div></div>
  </div>

  <p class="text-sm">Jump to: {nav}</p>

  {''.join(sections)}

  <p class="text-xs opacity-60">Model P = simulation win probability (push-adjusted).
  Fair P = de-vigged (power) median across books at the same line. Edge = model − fair.
  Each table sorted by |edge| within its category.</p>

</div></body></html>"""

out = ROOT / ".lavish" / f"wnba-slate-{date}.html"
out.write_text(page, encoding="utf-8")
print(f"wrote {out} ({len(rows)} rows, {n_bets} logged)")




