"""Build the Lavish slate board from reports/slate CSV + db context."""

import csv
import html
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
conn = sqlite3.connect(ROOT / "data" / "wnba.db")
conn.row_factory = sqlite3.Row

stamp = conn.execute("SELECT MAX(captured_at_utc) m FROM prop_lines").fetchone()["m"]
games = {r["event_id"]: f'{r["away_team_raw"]} @ {r["home_team_raw"]}'
         for r in conn.execute("SELECT * FROM odds_event_map")}
player_game = {}
for r in conn.execute(
        "SELECT DISTINCT pl.player_id, p.player_name, pl.event_id "
        "FROM prop_lines pl JOIN players p ON p.player_id = pl.player_id "
        "WHERE pl.captured_at_utc = ?", (stamp,)):
    player_game[r["player_name"]] = games.get(r["event_id"], "?")

rows = list(csv.DictReader(open(ROOT / "reports" / "slate_2026-08-11.csv",
                                encoding="utf-8")))
n_bets = sum(1 for r in rows if r["bet_flag"] == "1")
holds = [dict(r) for r in conn.execute(
    "SELECT book, COUNT(*) n FROM prop_lines WHERE captured_at_utc = ? "
    "AND over_price IS NOT NULL AND under_price IS NOT NULL GROUP BY book",
    (stamp,))]
age_h = (datetime.now(timezone.utc)
         - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds() / 3600

def badge(side):
    cls = "badge-info" if side == "over" else "badge-warning"
    return f'<span class="badge badge-sm {cls}">{side.upper()}</span>'

def edge_cell(e):
    e = float(e)
    color = ("text-error font-bold" if abs(e) >= 0.15
             else "text-warning font-semibold" if abs(e) >= 0.08
             else "text-base-content")
    note = " ⚠" if abs(e) >= 0.15 else ""
    return f'<span class="{color}">{e:+.1%}{note}</span>'

body_rows = []
for r in sorted(rows, key=lambda x: -abs(float(x["edge"]))):
    gm = player_game.get(r["player"], "")
    flags = []
    if r["is_whole_line"] == "1":
        flags.append('<span class="badge badge-ghost badge-xs">push poss.</span>')
    body_rows.append(f"""<tr class="hover">
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
    </tr>""")

hold_txt = " · ".join(f"{h['book']} ({h['n']})" for h in holds)

page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WNBA Props Slate — 2026-08-11</title>
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
    <h1 class="text-2xl font-bold">WNBA Player Points — Slate Board</h1>
    <p class="text-sm opacity-70">Snapshot {stamp} ({age_h:.1f}h old) · 6 games ·
    {len(rows)} priced sides · books quoting: {hold_txt}</p>
  </header>

  <div role="alert" class="alert alert-warning">
    <span><b>Diagnostic, not a betting card.</b> Phase 9: the market's centers are better
    calibrated than the model's (log loss 0.6942 vs 0.6979). Large "edges" below are the
    model's most likely <i>errors</i> — rows ⚠ above 15% are near-certainly model error
    (real edges in this market run 1–5%). No stakes until prospective CLV proves out.</span>
  </div>

  <div class="stats stats-vertical sm:stats-horizontal shadow bg-base-100 w-full">
    <div class="stat"><div class="stat-title">Priced sides</div>
      <div class="stat-value text-2xl">{len(rows)}</div></div>
    <div class="stat"><div class="stat-title">Above 3% floor + EV&gt;0</div>
      <div class="stat-value text-2xl">{n_bets}</div>
      <div class="stat-desc">flag only — see warning</div></div>
    <div class="stat"><div class="stat-title">Median hold</div>
      <div class="stat-value text-2xl">~6.8%</div>
      <div class="stat-desc">props run 6–12%</div></div>
    <div class="stat"><div class="stat-title">Closing-line archive</div>
      <div class="stat-value text-2xl">live</div>
      <div class="stat-desc">3 captures/day via Actions</div></div>
  </div>

  <div class="card bg-base-100 shadow">
    <div class="card-body p-4">
      <h2 class="card-title text-base">All sides, sorted by |model − market| disagreement</h2>
      <div class="overflow-x-auto">
        <table class="table table-sm table-zebra">
          <thead><tr>
            <th>Player / game</th><th class="text-right">Line</th><th>Side</th>
            <th class="text-right">Best price</th>
            <th class="text-right">Model P</th><th class="text-right">Fair P</th>
            <th class="text-right">Edge</th><th class="text-right">¼-Kelly</th><th></th>
          </tr></thead>
          <tbody>{''.join(body_rows)}</tbody>
        </table>
      </div>
      <p class="text-xs opacity-60">Model P = simulation win probability (push-adjusted).
      Fair P = de-vigged (power method) median across books at the same line.
      Edge = model − fair. ¼-Kelly shown for scale only.</p>
    </div>
  </div>

</div></body></html>"""

out = ROOT / ".lavish" / "wnba-slate-2026-08-11.html"
out.write_text(page, encoding="utf-8")
print(f"wrote {out} ({len(rows)} rows)")
