"""Cleaning and joining: odds_snapshots -> prop_lines, next to actual results.

Rebuilds odds_event_map and prop_lines wholesale from the DB and raw odds files
(idempotent). name_map is persistent — a raw name is resolved once, ever.
Nothing is dropped: every odds snapshot row lands in prop_lines with a
match_status, and reports/unmatched.md accounts for every loss.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.config import ROOT, load_config
from src.db import connect, get_meta
from src.log import get_logger

log = get_logger("clean")

RAW_ODDS = ROOT / "data" / "raw" / "odds"
REPORTS = ROOT / "reports"
APPROVALS = ROOT / "data" / "external" / "name_approvals.csv"
ET = ZoneInfo("America/New_York")

PUNCT = str.maketrans("", "", ".''`’‘-,")


def norm(s: str) -> str:
    """Light normalization: NFKD-strip accents, drop punctuation, collapse
    whitespace, lowercase. 'A’ja  Wilson' and 'Aja Wilson' both -> 'aja wilson'."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.translate(PUNCT).split()).lower()


def norm_flip(s: str) -> str:
    """'Wilson, A'ja' -> normalized 'aja wilson'."""
    if "," in s:
        last, _, first = s.partition(",")
        s = f"{first.strip()} {last.strip()}"
    return norm(s)


# ---------------------------------------------------------------- event map


def scan_raw_events() -> dict[str, dict]:
    """Every event id seen in raw odds files, with commence time and teams."""
    events = {}
    for f in sorted(RAW_ODDS.glob("*.json")):
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            log.info(f"  WARN unreadable raw file {f.name}: {e}")
            continue
        items = payload if isinstance(payload, list) else [payload]
        for ev in items:
            if isinstance(ev, dict) and "id" in ev and "commence_time" in ev:
                events[ev["id"]] = {
                    "commence_time": ev["commence_time"],
                    "home_team": ev.get("home_team"),
                    "away_team": ev.get("away_team"),
                }
    return events


def build_event_map(conn: sqlite3.Connection, ccfg: dict) -> None:
    events = scan_raw_events()
    teams = {
        norm(r["display_name"]): str(r["team_id"])
        for r in conn.execute("SELECT team_id, display_name FROM teams")
    }
    for alias, canonical in ccfg["team_aliases"].items():
        teams[norm(alias)] = teams.get(norm(canonical), canonical)
    last_stats = get_meta(conn, "last_ingested_game_date") or "0000-00-00"

    rows, unmatched = [], []
    for eid, ev in events.items():
        commence = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        date_et = str(commence.astimezone(ET).date())
        home_id = teams.get(norm(ev["home_team"] or ""))
        away_id = teams.get(norm(ev["away_team"] or ""))
        game = None
        if home_id and away_id:
            game = conn.execute(
                "SELECT game_id FROM games WHERE game_date = ? AND "
                "((home_team_id = ? AND away_team_id = ?) OR "
                " (home_team_id = ? AND away_team_id = ?))",
                (date_et, home_id, away_id, away_id, home_id)).fetchone()
        if game:
            status = "ok"
        elif date_et > last_stats:
            status = "stats_pending"  # game newer than ingested stats
        else:
            status = "game_unmatched"
            unmatched.append(f"{ev['away_team']} @ {ev['home_team']} {date_et}")
        rows.append((eid, game["game_id"] if game else None, date_et,
                     ev["commence_time"], ev["home_team"], ev["away_team"], status))

    with conn:
        conn.execute("DELETE FROM odds_event_map")
        conn.executemany(
            "INSERT INTO odds_event_map VALUES (?,?,?,?,?,?,?)", rows)
    n_ok = sum(1 for r in rows if r[6] == "ok")
    n_pend = sum(1 for r in rows if r[6] == "stats_pending")
    log.info(f"event map: {len(rows)} events -> {n_ok} matched, {n_pend} awaiting "
             f"stats, {len(unmatched)} unmatched")
    for u in unmatched[:10]:
        log.info(f"  UNMATCHED event: {u}")


# ---------------------------------------------------------------- name map


def build_name_map(conn: sqlite3.Connection, ccfg: dict) -> list[dict]:
    """Resolve new raw names via exact -> team+date -> user approvals.
    Returns fuzzy proposals for names still unresolved (never auto-applied)."""
    raw_names = [r["player_name_raw"] for r in conn.execute(
        "SELECT DISTINCT player_name_raw FROM odds_snapshots")]
    known = {r["raw_name"] for r in conn.execute(
        "SELECT raw_name FROM name_map WHERE source = 'the-odds-api'")}
    new = [n for n in raw_names if n not in known]
    log.info(f"name map: {len(raw_names)} distinct raw names, "
             f"{len(known)} already mapped, {len(new)} new")

    players = conn.execute("SELECT player_id, player_name FROM players").fetchall()
    by_norm: dict[str, list] = {}
    for p in players:
        by_norm.setdefault(norm(p["player_name"]), []).append(str(p["player_id"]))

    inserts, unresolved = [], []
    for raw in new:
        cands = by_norm.get(norm(raw)) or by_norm.get(norm_flip(raw)) or []
        if len(set(cands)) == 1:
            inserts.append((raw, "the-odds-api", cands[0], 1.0, "exact"))
        elif len(set(cands)) > 1:
            unresolved.append((raw, "ambiguous_exact"))
        else:
            unresolved.append((raw, "no_exact"))

    # team+date: a raw name inside an event is unambiguous if exactly one
    # rostered player that night shares the (initial +) last name
    still = []
    for raw, why in unresolved:
        cand_ids = set()
        conflict = False
        toks = norm(raw).split()
        if not toks:
            still.append((raw, why))
            continue
        last_tok, first_tok = toks[-1], toks[0]
        games = conn.execute(
            "SELECT DISTINCT m.game_id FROM odds_snapshots s "
            "JOIN odds_event_map m ON m.event_id = s.game_id "
            "WHERE s.player_name_raw = ? AND m.game_id IS NOT NULL", (raw,))
        for g in games:
            roster = conn.execute(
                "SELECT DISTINCT a.player_id, p.player_name FROM availability a "
                "JOIN players p ON p.player_id = a.player_id WHERE a.game_id = ?",
                (g["game_id"],)).fetchall()
            hits = {
                str(r["player_id"]) for r in roster
                if norm(r["player_name"]).split()[-1] == last_tok
                and norm(r["player_name"])[0] == first_tok[0]
            }
            if len(hits) == 1:
                cand_ids |= hits
            elif len(hits) > 1:
                conflict = True
        if len(cand_ids) == 1 and not conflict:
            inserts.append((raw, "the-odds-api", cand_ids.pop(), 0.9, "team_date"))
        else:
            still.append((raw, why))

    # user approvals from data/external/name_approvals.csv (raw_name,player_id)
    if APPROVALS.exists():
        with open(APPROVALS, encoding="utf-8", newline="") as f:
            approved = {r["raw_name"]: r["player_id"] for r in csv.DictReader(f)}
        applied = 0
        for raw, pid in approved.items():
            conn.execute(
                "INSERT OR REPLACE INTO name_map VALUES (?,?,?,?,?)",
                (raw, "the-odds-api", pid, 1.0, "user"))
            applied += 1
        still = [(raw, why) for raw, why in still if raw not in approved]
        if applied:
            log.info(f"  applied {applied} user approvals from {APPROVALS.name}")

    with conn:
        conn.executemany("INSERT OR IGNORE INTO name_map VALUES (?,?,?,?,?)", inserts)
    by_method = {}
    for i in inserts:
        by_method[i[4]] = by_method.get(i[4], 0) + 1
    log.info(f"  new mappings: {by_method or 0}; unresolved: {len(still)}")

    # fuzzy PROPOSALS only — printed for approval, never applied
    proposals = []
    if still:
        from rapidfuzz import process, fuzz

        names = {p["player_name"]: str(p["player_id"]) for p in players}
        for raw, why in still:
            top = process.extract(
                raw, list(names), scorer=fuzz.WRatio,
                score_cutoff=ccfg["fuzzy_cutoff"], limit=3)
            cands = [(t[0], names[t[0]], round(t[1], 1)) for t in top]
            if not cands:
                # last resort: same first name on the roster of a game where
                # this raw name was offered (catches surname changes)
                first = norm(raw).split()[0] if norm(raw) else ""
                roster_hits = conn.execute(
                    "SELECT DISTINCT p.player_id, p.player_name "
                    "FROM odds_snapshots s "
                    "JOIN odds_event_map m ON m.event_id = s.game_id "
                    "JOIN availability a ON a.game_id = m.game_id "
                    "JOIN players p ON p.player_id = a.player_id "
                    "WHERE s.player_name_raw = ? AND m.game_id IS NOT NULL",
                    (raw,)).fetchall()
                cands = [
                    (r["player_name"], str(r["player_id"]), "roster/first-name")
                    for r in roster_hits
                    if norm(r["player_name"]).split()[0] == first
                ]
            proposals.append({"raw": raw, "why": why, "candidates": cands})
        log.info("  fuzzy proposals (approve by adding lines to "
                 "data/external/name_approvals.csv as raw_name,player_id):")
        for p in proposals:
            log.info(f"    {p['raw']!r} ({p['why']}) -> " + (
                "; ".join(f"{c[0]} [{c[1]}] {c[2]}" for c in p["candidates"])
                or "no candidates"))
    return proposals


# ---------------------------------------------------------------- prop_lines


def build_prop_lines(conn: sqlite3.Connection, ccfg: dict) -> dict:
    n_snap = conn.execute("SELECT COUNT(*) n FROM odds_snapshots").fetchone()["n"]
    emap = {r["event_id"]: r for r in conn.execute("SELECT * FROM odds_event_map")}
    nmap = {r["raw_name"]: str(r["player_id"]) for r in conn.execute(
        "SELECT raw_name, player_id FROM name_map WHERE source = 'the-odds-api'")}
    market_stats = ccfg["market_stats"]

    rows, counts = [], {}
    for s in conn.execute("SELECT rowid AS snapshot_id, * FROM odds_snapshots"):
        ev = emap.get(s["game_id"])  # odds_snapshots.game_id is the event id
        base_market = s["market"].removesuffix("_alternate")
        stat_col = market_stats.get(base_market)
        line = s["line"]
        whole = int(line is not None and float(line).is_integer())
        pid = nmap.get(s["player_name_raw"])

        game_id = ev["game_id"] if ev else None
        date_et = ev["game_date_et"] if ev else None
        actual = result = None
        voided = 0

        if stat_col is None:
            status = "market_unknown"
        elif ev is None or ev["match_status"] == "game_unmatched":
            status = "game_unmatched"
        elif ev["match_status"] == "stats_pending":
            status = "stats_pending"
        elif pid is None:
            status = "name_unmatched"
        else:
            pg = conn.execute(
                f"SELECT minutes, dnp_reason, {stat_col} AS actual "
                "FROM player_games WHERE game_id = ? AND player_id = ?",
                (game_id, pid)).fetchone()
            if pg and pg["dnp_reason"] is None and pg["actual"] is not None:
                status = "ok"
                actual = float(pg["actual"])
                if line is not None:
                    result = ("over" if actual > line
                              else "under" if actual < line else "push")
            else:
                # scratched / DNP / not in the box score: voided, NOT an under
                status = "voided"
                voided = 1

        counts[status] = counts.get(status, 0) + 1
        rows.append((s["snapshot_id"], s["captured_at_utc"], s["game_id"], game_id,
                     date_et, s["book"], s["market"], s["player_name_raw"], pid,
                     line, s["over_price"], s["under_price"], s["is_alternate"],
                     whole, voided, actual, result, status))

    with conn:
        conn.execute("DELETE FROM prop_lines")
        conn.executemany(
            "INSERT INTO prop_lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows)
    log.info(f"prop_lines: {n_snap} odds snapshots in -> {len(rows)} rows out "
             f"(1:1, nothing dropped); status breakdown: {counts}")
    return counts


# ---------------------------------------------------------------- report


def write_unmatched_report(conn: sqlite3.Connection, proposals: list[dict]) -> None:
    REPORTS.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    q = conn.execute

    def rate(where: str = "1=1") -> str:
        r = q(f"SELECT SUM(match_status IN ('ok','voided')) m, COUNT(*) n "
              f"FROM prop_lines WHERE match_status NOT IN "
              f"('stats_pending','game_unmatched') AND {where}").fetchone()
        return f"{100 * (r['m'] or 0) / r['n']:.1f}% ({r['m']}/{r['n']})" if r["n"] else "n/a"

    lines = [f"# Unmatched report", f"", f"Generated {now} by `run.py clean`.", ""]
    lines += ["## Overview", ""]
    for r in q("SELECT match_status, COUNT(*) n FROM prop_lines "
               "GROUP BY match_status ORDER BY n DESC"):
        lines.append(f"- `{r['match_status']}`: {r['n']}")
    lines += ["",
              f"**Match rate** (odds rows whose game has stats): {rate()}",
              "`stats_pending` rows (games newer than ingested stats) are excluded "
              "from the rate — they resolve when stats catch up.", ""]

    lines += ["## Match rate by book", ""]
    for r in q("SELECT DISTINCT book FROM prop_lines ORDER BY book"):
        where = "book = '{}'".format(r["book"])
        lines.append(f"- {r['book']}: {rate(where)}")
    lines += ["", "## Match rate by month (capture time)", ""]
    for r in q("SELECT DISTINCT substr(captured_at_utc,1,7) m FROM prop_lines ORDER BY m"):
        where = "substr(captured_at_utc,1,7) = '{}'".format(r["m"])
        lines.append(f"- {r['m']}: {rate(where)}")
    lines += ["", "## Match rate by team (either side of the mapped game)", ""]
    for r in q("SELECT t.display_name d, t.team_id tid FROM teams t "
               "WHERE t.team_id IN (SELECT home_team_id FROM games WHERE season = "
               "(SELECT MAX(season) FROM games)) ORDER BY d"):
        w = (f"game_id IN (SELECT game_id FROM games WHERE "
             f"home_team_id = '{r['tid']}' OR away_team_id = '{r['tid']}')")
        lines.append(f"- {r['d']}: {rate(w)}")

    lines += ["", "## Top 20 unmatched names by frequency", ""]
    top = q("SELECT player_name_raw, COUNT(*) n FROM prop_lines "
            "WHERE match_status = 'name_unmatched' GROUP BY 1 "
            "ORDER BY n DESC LIMIT 20").fetchall()
    lines += [f"- {r['player_name_raw']!r}: {r['n']} rows" for r in top] or ["(none)"]

    lines += ["", "## Fuzzy proposals — approve via `data/external/name_approvals.csv`", ""]
    if proposals:
        lines.append("| raw name | reason | candidates (name [player_id] score) |")
        lines.append("|---|---|---|")
        for p in proposals:
            cand = "; ".join(f"{c[0]} [{c[1]}] {c[2]}" for c in p["candidates"]) or "none"
            lines.append(f"| `{p['raw']}` | {p['why']} | {cand} |")
    else:
        lines.append("(none — every raw name resolved)")

    lines += ["", "## Unmatched odds rows (game has stats, name did not match)", ""]
    rows = q("SELECT player_name_raw, game_date_et, book, COUNT(*) n FROM prop_lines "
             "WHERE match_status = 'name_unmatched' GROUP BY 1,2,3 ORDER BY 2,1").fetchall()
    lines += [f"- {r['player_name_raw']!r} on {r['game_date_et']} ({r['book']}): "
              f"{r['n']} lines" for r in rows] or ["(none)"]

    lines += ["", "## Player-games with no odds row (expected — not everyone gets props)", ""]
    r = q("SELECT COUNT(*) n FROM player_games pg WHERE dnp_reason IS NULL AND "
          "NOT EXISTS (SELECT 1 FROM prop_lines pl WHERE pl.game_id = pg.game_id "
          "AND pl.player_id = pg.player_id)").fetchone()
    r2 = q("SELECT COUNT(DISTINCT pg.game_id) n FROM player_games pg "
           "JOIN prop_lines pl ON pl.game_id = pg.game_id").fetchone()
    lines += [f"- {r['n']} played player-games have no prop line "
              f"(odds coverage spans {r2['n']} of the ingested games; "
              "most of history predates odds collection — expected).", ""]

    (REPORTS / "unmatched.md").write_text("\n".join(lines), encoding="utf-8")
    log.info(f"wrote reports/unmatched.md — read it weekly; overall match rate {rate()}")


# ---------------------------------------------------------------- entry points


def run_clean() -> int:
    cfg = load_config()
    ccfg = cfg["clean"]
    conn = connect()
    build_event_map(conn, ccfg)
    proposals = build_name_map(conn, ccfg)
    build_prop_lines(conn, ccfg)
    write_unmatched_report(conn, proposals)
    return 0


def run_audit() -> int:
    """Data quality report: stats validations + join health."""
    from src.ingest_stats import print_summary, run_validation

    cfg = load_config()
    conn = connect()
    passed, total = run_validation(conn, cfg)
    print_summary(conn, passed, total)
    n = conn.execute("SELECT COUNT(*) n FROM prop_lines").fetchone()["n"]
    if n:
        by = {r["match_status"]: r["n"] for r in conn.execute(
            "SELECT match_status, COUNT(*) n FROM prop_lines GROUP BY 1")}
        log.info(f"prop_lines: {n} rows, status {by} — details in reports/unmatched.md")
    else:
        log.info("prop_lines empty — run `python run.py clean`")
    return 0 if passed == total else 1
