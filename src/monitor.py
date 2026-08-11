"""Monitoring: the checks that catch silent failures.

Automation without monitoring is worse than manual operation — it fails
quietly and the archive grows holes nobody notices for weeks.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from src.config import load_config
from src.log import get_logger

log = get_logger("monitor")


def check_staleness(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """FAIL LOUDLY (exit 1) if the newest odds snapshot is older than the
    configured limit. This is the check that catches silent failures —
    a run that quietly stopped snapshotting looks identical to a quiet day
    without it."""
    limit_h = float(load_config()["odds"].get("staleness_hours", 36))
    now = now or datetime.now(timezone.utc)
    row = conn.execute(
        "SELECT MAX(captured_at_utc) m FROM odds_snapshots").fetchone()
    if not row or not row["m"]:
        log.info("STALENESS FAIL: no odds snapshots exist at all")
        return 1
    newest = datetime.fromisoformat(row["m"].replace("Z", "+00:00"))
    age_h = (now - newest).total_seconds() / 3600
    if age_h > limit_h:
        log.info(f"STALENESS FAIL: newest odds snapshot is {age_h:.1f}h old "
                 f"(limit {limit_h:.0f}h, captured {row['m']}) — the collector "
                 "has been failing silently. Check the last runs.")
        return 1
    log.info(f"staleness ok: newest snapshot {age_h:.1f}h old "
             f"(limit {limit_h:.0f}h)")
    return 0


def weekly_summary(conn: sqlite3.Connection) -> None:
    """Days run vs days with games, snapshots taken, quota consumed — the
    heartbeat that shows whether collection is actually happening."""
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    runs = conn.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT substr(ts_utc, 1, 10)) days,"
        " SUM(lines_inserted) lines, SUM(events_failed) fails,"
        " MIN(quota_remaining) q_end, MAX(quota_remaining) q_start "
        "FROM run_log WHERE ts_utc >= ?", (week_ago,)).fetchone()
    game_days = conn.execute(
        "SELECT COUNT(DISTINCT game_date) n FROM games WHERE game_date >= ?",
        (week_ago,)).fetchone()["n"]
    snap_days = conn.execute(
        "SELECT COUNT(DISTINCT substr(captured_at_utc, 1, 10)) n "
        "FROM odds_snapshots WHERE captured_at_utc >= ?", (week_ago,)).fetchone()["n"]
    quota_used = (runs["q_start"] - runs["q_end"]) \
        if runs["q_start"] and runs["q_end"] else 0
    log.info("weekly collection summary (last 7 days):")
    log.info(f"  runs: {runs['n'] or 0} across {runs['days'] or 0} days; "
             f"snapshot days: {snap_days}; game days in data: {game_days}")
    log.info(f"  lines archived: {runs['lines'] or 0}; "
             f"event fetch failures: {runs['fails'] or 0}; "
             f"quota consumed: ~{quota_used}")
    missed = max(0, game_days - snap_days)
    if missed:
        log.info(f"  ⚠ {missed} game day(s) with no snapshot — archive holes")
