"""Odds ingestion: The Odds API event-odds -> data/raw/odds/ -> odds_snapshots.

Every run stamps one UTC capture time, writes every API response verbatim to
data/raw/odds/<stamp>_*.json BEFORE parsing, then appends rows to
odds_snapshots. Snapshots are never updated in place — the table is a time
series and its value is in the movement. --dry-run re-parses the latest saved
raw files with no network access.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from src.config import ROOT, load_config, load_env
from src.db import connect
from src.log import get_logger

log = get_logger("odds")

RAW_DIR = ROOT / "data" / "raw" / "odds"
API = "https://api.the-odds-api.com/v4"
TIMEOUT = 30


def _stamp(dt: datetime) -> str:
    """ISO8601 UTC, colons swapped for hyphens so it works as a Windows filename."""
    return dt.strftime("%Y-%m-%dT%H-%M-%SZ")


def _stamp_to_iso(stamp: str) -> str:
    d, t = stamp.rstrip("Z").split("T")
    return f"{d}T{t.replace('-', ':')}Z"


# ---------------------------------------------------------------- parse


def parse_event_odds(payload: dict, captured_at_utc: str) -> list[tuple]:
    """Pair Over/Under outcomes per (book, market, player, line). Keeps every
    alternate line; a missing side stays null rather than dropping the row."""
    rows = {}
    event_id = payload.get("id", "unknown")
    for bm in payload.get("bookmakers", []):
        book = bm.get("key", "unknown")
        for mkt in bm.get("markets", []):
            mkey = mkt.get("key", "unknown")
            is_alt = int(mkey.endswith("_alternate"))
            for out in mkt.get("outcomes", []):
                player = out.get("description")
                if player is None:
                    continue  # not a player-prop outcome shape
                key = (book, mkey, player, out.get("point"))
                row = rows.setdefault(
                    key,
                    {"over": None, "under": None, "alt": is_alt},
                )
                side = (out.get("name") or "").lower()
                if side == "over":
                    row["over"] = out.get("price")
                elif side == "under":
                    row["under"] = out.get("price")
    return [
        (captured_at_utc, event_id, book, mkey, player, point,
         r["over"], r["under"], r["alt"])
        for (book, mkey, player, point), r in rows.items()
    ]


def insert_rows(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """Append-only insert; the natural-key UNIQUE index makes re-parsing the
    same raw file idempotent."""
    before = conn.execute("SELECT COUNT(*) n FROM odds_snapshots").fetchone()["n"]
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO odds_snapshots "
            "(captured_at_utc, game_id, book, market, player_name_raw, line, "
            " over_price, under_price, is_alternate) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
    after = conn.execute("SELECT COUNT(*) n FROM odds_snapshots").fetchone()["n"]
    return after - before


# ---------------------------------------------------------------- live fetch


def run_odds(dry_run: bool = False) -> int:
    cfg = load_config()["odds"]
    conn = connect()
    if dry_run:
        return _dry_run(conn)

    api_key = load_env().get("ODDS_API_KEY", "")
    if not api_key:
        log.info("no ODDS_API_KEY in .env — copy .env.example to .env and add "
                 "your the-odds-api.com key")
        return 1

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    stamp = _stamp(now)
    captured_at = _stamp_to_iso(stamp)

    # events list (costs 0 credits)
    try:
        resp = requests.get(
            f"{API}/sports/{cfg['sport_key']}/events",
            params={"apiKey": api_key}, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.info(f"network error fetching events — no snapshot taken: "
                 f"{type(e).__name__}: {_scrub(str(e), api_key)}")
        return 1
    used_before = int(resp.headers.get("x-requests-used", 0))
    events = resp.json()
    (RAW_DIR / f"{stamp}_events.json").write_text(
        json.dumps(events, indent=1), encoding="utf-8")

    horizon = now + timedelta(hours=float(cfg["horizon_hours"]))
    upcoming = [
        e for e in events
        if datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) <= horizon
    ]
    log.info(f"events: {len(events)} listed, {len(upcoming)} within "
             f"{cfg['horizon_hours']}h horizon")
    if not upcoming:
        log.info("no games in window — nothing to snapshot")
        _print_quota(resp.headers, 0, cfg)
        return 0

    markets = ",".join(cfg["markets"])
    all_rows, headers, failures = [], resp.headers, 0
    for ev in upcoming:
        label = f"{ev.get('away_team')} @ {ev.get('home_team')} ({ev['id']})"
        try:
            r = requests.get(
                f"{API}/sports/{cfg['sport_key']}/events/{ev['id']}/odds",
                params={"apiKey": api_key, "regions": cfg["regions"],
                        "markets": markets, "oddsFormat": cfg["odds_format"]},
                timeout=TIMEOUT)
            r.raise_for_status()
        except requests.RequestException as e:
            failures += 1
            log.info(f"  ERROR {label}: {type(e).__name__}: "
                     f"{_scrub(str(e), api_key)} — continuing")
            continue
        headers = r.headers
        payload = r.json()
        (RAW_DIR / f"{stamp}_{ev['id']}.json").write_text(
            json.dumps(payload, indent=1), encoding="utf-8")
        try:
            rows = parse_event_odds(payload, captured_at)
        except (KeyError, TypeError, ValueError) as e:
            failures += 1
            log.info(f"  ERROR parsing {label}: {type(e).__name__}: {e} — "
                     f"raw saved, continuing")
            continue
        if rows:
            books = len({row[2] for row in rows})
            log.info(f"  {label}: {len(rows)} lines from {books} books")
        else:
            log.info(f"  {label}: no props posted yet")
        all_rows += rows

    inserted = insert_rows(conn, all_rows)
    log.info(f"snapshot {captured_at}: {len(all_rows)} lines parsed, "
             f"{inserted} inserted ({len(upcoming) - failures}/{len(upcoming)} "
             f"events ok)")
    used_after = int(headers.get("x-requests-used", used_before))
    _print_quota(headers, used_after - used_before, cfg)
    return 0


def _scrub(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def _print_quota(headers, run_cost: int, cfg: dict) -> None:
    remaining = headers.get("x-requests-remaining", "?")
    monthly = run_cost * int(cfg["snapshots_per_day"]) * 30
    log.info(f"quota: {run_cost} credits this run, {remaining} remaining; "
             f"projected {monthly}/month at {cfg['snapshots_per_day']} snapshots/day")


# ---------------------------------------------------------------- dry run


def _dry_run(conn: sqlite3.Connection) -> int:
    files = sorted(RAW_DIR.glob("*.json")) if RAW_DIR.exists() else []
    event_files = [f for f in files if not f.stem.endswith("_events")]
    if not event_files:
        log.info("dry-run: no raw odds files in data/raw/odds/ — run "
                 "`python run.py update` live once first")
        return 1
    latest_stamp = max(f.stem.split("_")[0] for f in event_files)
    batch = [f for f in event_files if f.stem.startswith(latest_stamp)]
    captured_at = _stamp_to_iso(latest_stamp)
    log.info(f"dry-run: re-parsing {len(batch)} raw files from snapshot "
             f"{captured_at} (no network)")
    all_rows = []
    for f in batch:
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
            all_rows += parse_event_odds(payload, captured_at)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            log.info(f"  ERROR parsing {f.name}: {type(e).__name__}: {e} — continuing")
    inserted = insert_rows(conn, all_rows)
    log.info(f"dry-run: {len(all_rows)} lines parsed, {inserted} inserted "
             f"(0 expected if the live run already stored this snapshot)")
    return 0
