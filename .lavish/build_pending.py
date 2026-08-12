"""Build the Lavish pending-bets board from paper_bets + latest captures."""

import html
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
conn = sqlite3.connect(ROOT / "data" / "wnba.db")
conn.row_factory = sqlite3.Row

stamp = conn.execute("SELECT MAX(captured_at_utc) m FROM prop_lines").fetchone()["m"]
games = {r["event_id"]: (f'{r["away_team_raw"]} @ {r["home_team_raw"]}',
                         r["commence_time_utc"])
         for r in conn.execute("SELECT * FROM odds_event_map")}

bets = [dict(r) for r in conn.execute(
    "SELECT * FROM paper_bets WHERE status = 'pending' ORDER BY event_id, edge DESC")]

# current quote for the same (event, player, line, book) from the latest stamp
def current(b):
    r = conn.execute(
        "SELECT over_price, under_price FROM prop_lines WHERE captured_at_utc = ?"
        " AND event_id = ? AND player_id = ? AND market = ? AND line = ?"
        " AND book = ?",
        (stamp, b["event_id"], b["player_id"], b["market"], b["line"],
         b["book"])).fetchone()
    if not r:
        return None
    return r["over_price"] if b["side"] == "over" else r["under_price"]

total_risk = sum(b["risk"] for b in bets)
total_win = sum(b["to_win"] for b in bets)
age_h = (datetime.now(timezone.utc)
         - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds() / 3600

moved_for = moved_against = 0
sections = []
for ev in dict.fromkeys(b["event_id"] for b in bets):
    gm, tip = games.get(ev, ("?", ""))
    rows = []
    for b in [x for x in bets if x["event_id"] == ev]:
        cur = current(b)
        if cur is None:
            move = '<span class="opacity-50">line gone</span>'
        else:
            d = cur - b["price"]  # more positive American price = better for us
            if d > 0:
                moved_for += 1
                move = f'<span class="text-success font-semibold">{b["price"]:+d} → {cur:+d} (better)</span>'
            elif d < 0:
                moved_against += 1
                move = f'<span class="text-error">{b["price"]:+d} → {cur:+d} (worse)</span>'
            else:
                move = f'<span class="opacity-60">{cur:+d} (unmoved)</span>'
        side_cls = "badge-info" if b["side"] == "over" else "badge-warning"
        stat = {"player_points": "Points", "player_rebounds": "Rebounds",
                "player_assists": "Assists"}.get(b["market"], b["market"])
        rows.append(f"""<tr class="hover">
          <td class="font-medium whitespace-nowrap">{html.escape(b['player_name'])}</td>
          <td><span class="badge badge-ghost badge-sm">{stat}</span></td>
          <td class="text-right font-mono">{b['line']}</td>
          <td><span class="badge badge-sm {side_cls}">{b['side'].upper()}</span></td>
          <td class="whitespace-nowrap">{move}
            <div class="text-xs opacity-60">{html.escape(b['book'])}</div></td>
          <td class="text-right font-mono">{b['edge']:+.1%}</td>
          <td class="text-right font-mono">${b['risk']:.0f} / ${b['to_win']:.0f}</td>
        </tr>""")
    sections.append(f"""<div class="card bg-base-100 shadow">
      <div class="card-body p-4">
        <h2 class="card-title text-base">{html.escape(gm)}
          <span class="text-xs font-normal opacity-60">tip {tip}</span></h2>
        <div class="overflow-x-auto"><table class="table table-sm table-zebra">
          <thead><tr><th>Player</th><th>Stat</th><th class="text-right">Line</th><th>Side</th>
          <th>Price: logged → now</th><th class="text-right">Edge @ log</th>
          <th class="text-right">Risk / Win</th></tr></thead>
          <tbody>{''.join(rows)}</tbody></table></div>
      </div></div>""")

page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WNBA Paper Book — Pending</title>
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
<div class="max-w-5xl mx-auto p-6 space-y-5">
  <header class="space-y-1">
    <h1 class="text-2xl font-bold">Pending Paper Bets — tonight's 3 games</h1>
    <p class="text-sm opacity-70">Logged 2026-08-11T23:20Z at risk/win $100 ·
    prices compared against snapshot {stamp} ({age_h:.1f}h old) · data-only tracking</p>
  </header>

  <div class="stats stats-vertical sm:stats-horizontal shadow bg-base-100 w-full">
    <div class="stat"><div class="stat-title">Pending</div>
      <div class="stat-value text-2xl">{len(bets)}</div>
      <div class="stat-desc">book so far: 17W–17L, −$253</div></div>
    <div class="stat"><div class="stat-title">At risk / to win</div>
      <div class="stat-value text-2xl">${total_risk:,.0f} / ${total_win:,.0f}</div></div>
    <div class="stat"><div class="stat-title">Line movement since log</div>
      <div class="stat-value text-2xl">{moved_for}↗ {moved_against}↘</div>
      <div class="stat-desc">better/worse than our price — the early CLV read</div></div>
  </div>

  {''.join(sections)}

  <p class="text-xs opacity-60">These settle automatically after tonight's box scores
  land (tomorrow's update/clean). CLV per bet computes against the last pre-tip
  capture. One night proves nothing either way — the book exists to accumulate CLV.</p>
</div></body></html>"""

out = ROOT / ".lavish" / "wnba-pending-2026-08-12.html"
out.write_text(page, encoding="utf-8")
print(f"wrote {out} ({len(bets)} pending, {moved_for} better / {moved_against} worse)")
