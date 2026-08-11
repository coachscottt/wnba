"""Paper bet log: every bet the model's policy would make, at risk/win $100.

Data-only tracking — no real stakes. Sizing convention: favorites risk
|price| to win $100; underdogs risk $100 to win the price. The log is the
CLV instrument: each bet's capture price gets compared to the last pre-tip
capture of the same line once closing snapshots exist.

Never reported here: ROI curves or units-won charts (guide §13.6) — W/L and
CLV are printed plainly with the sample size, nothing more.
"""

from __future__ import annotations

import sqlite3

import polars as pl  # noqa: F401 (parity with sibling modules)

from src.db import connect
from src.log import get_logger

log = get_logger("paper")

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_bets (
    logged_at   TEXT NOT NULL,           -- snapshot stamp the price came from
    game_date_et TEXT,
    event_id    TEXT NOT NULL,
    player_id   TEXT NOT NULL,
    player_name TEXT,
    market      TEXT NOT NULL,
    line        REAL NOT NULL,
    side        TEXT NOT NULL,
    price       INTEGER NOT NULL,        -- American, at log time
    book        TEXT NOT NULL,
    edge        REAL, model_p REAL, fair_p REAL,
    risk        REAL NOT NULL,           -- $ risked (risk/win 100 convention)
    to_win      REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','won','lost','push','void')),
    settled     REAL,                    -- +to_win / -risk / 0
    closing_price INTEGER,               -- same line+book, last pre-tip capture
    clv_cents   INTEGER,                 -- price - closing (positive = beat close)
    PRIMARY KEY (event_id, player_id, market, line, side)
);
"""


def _stake(price: int) -> tuple[float, float]:
    if price < 0:
        return float(-price), 100.0
    return 100.0, float(price)


def log_slate(conn: sqlite3.Connection, slate: list[dict], stamp: str) -> int:
    """Insert the policy's bets (bet_flag rows). First log wins — re-running
    project the same evening never overwrites the original paper price."""
    conn.executescript(SCHEMA)
    ev_of = {}
    for r in conn.execute(
            "SELECT DISTINCT event_id, player_id, game_date_et FROM prop_lines "
            "WHERE captured_at_utc = ?", (stamp,)):
        ev_of[str(r["player_id"])] = (r["event_id"], r["game_date_et"])
    pid_of = {r["player_name"]: str(r["player_id"]) for r in conn.execute(
        "SELECT player_id, player_name FROM players")}
    before = conn.execute("SELECT COUNT(*) n FROM paper_bets").fetchone()["n"]
    with conn:
        for r in slate:
            if not r.get("bet_flag"):
                continue
            pid = pid_of.get(r["player"])
            if not pid or pid not in ev_of:
                continue
            event_id, gdate = ev_of[pid]
            risk, to_win = _stake(int(r["best_price"]))
            conn.execute(
                "INSERT OR IGNORE INTO paper_bets (logged_at, game_date_et,"
                " event_id, player_id, player_name, market, line, side, price,"
                " book, edge, model_p, fair_p, risk, to_win) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (stamp, gdate, event_id, pid, r["player"], r["market"],
                 float(r["line"]), r["side"], int(r["best_price"]), r["book"],
                 float(r["edge"]), float(r["model_p"]), float(r["fair_p"]),
                 risk, to_win))
    added = conn.execute("SELECT COUNT(*) n FROM paper_bets").fetchone()["n"] - before
    log.info(f"paper log: {added} new bets recorded at risk/win $100 "
             f"(re-runs never overwrite an existing paper price)")
    return added


def settle(conn: sqlite3.Connection) -> None:
    """Grade pending paper bets from prop_lines results; fill CLV where a
    later pre-tip capture of the same line+book exists."""
    conn.executescript(SCHEMA)
    n_settled = 0
    for b in conn.execute("SELECT rowid, * FROM paper_bets WHERE status = 'pending'"):
        res = conn.execute(
            "SELECT result, match_status FROM prop_lines WHERE event_id = ? "
            "AND player_id = ? AND market = ? AND line = ? "
            "AND match_status IN ('ok', 'voided') LIMIT 1",
            (b["event_id"], b["player_id"], b["market"], b["line"])).fetchone()
        if not res:
            continue
        if res["match_status"] == "voided":
            status, amount = "void", 0.0
        elif res["result"] == "push":
            status, amount = "push", 0.0
        elif res["result"] == b["side"]:
            status, amount = "won", b["to_win"]
        elif res["result"] in ("over", "under"):
            status, amount = "lost", -b["risk"]
        else:
            continue
        close = conn.execute(
            "SELECT over_price, under_price FROM prop_lines WHERE event_id = ? "
            "AND player_id = ? AND market = ? AND line = ? AND book = ? "
            "ORDER BY captured_at_utc DESC LIMIT 1",
            (b["event_id"], b["player_id"], b["market"], b["line"], b["book"])
        ).fetchone()
        c_price = None
        if close:
            c_price = close["over_price"] if b["side"] == "over" else close["under_price"]
        with conn:
            conn.execute(
                "UPDATE paper_bets SET status = ?, settled = ?, closing_price = ?,"
                " clv_cents = ? WHERE rowid = ?",
                (status, amount, c_price,
                 (b["price"] - c_price) if c_price is not None else None,
                 b["rowid"]))
        n_settled += 1
    if n_settled:
        log.info(f"paper log: settled {n_settled} bets")
    summary(conn)


def summary(conn: sqlite3.Connection) -> None:
    s = conn.execute(
        "SELECT COUNT(*) n, SUM(status = 'pending') pend,"
        " SUM(status = 'won') w, SUM(status = 'lost') l,"
        " SUM(status IN ('push', 'void')) pv, SUM(settled) net,"
        " SUM(risk) FILTER (WHERE status IN ('won','lost')) risked "
        "FROM paper_bets").fetchone()
    if not s["n"]:
        return
    clv = conn.execute(
        "SELECT COUNT(*) n, AVG(clv_cents) avg_c,"
        " SUM(clv_cents > 0) pos FROM paper_bets "
        "WHERE clv_cents IS NOT NULL AND logged_at != ("
        " SELECT MAX(captured_at_utc) FROM prop_lines WHERE event_id = paper_bets.event_id)"
    ).fetchone()
    log.info(f"paper book (risk/win $100, data only): {s['n']} bets — "
             f"{s['pend'] or 0} pending, {s['w'] or 0}W-{s['l'] or 0}L"
             f"-{s['pv'] or 0}P/V, net ${s['net'] or 0:+,.0f} on "
             f"${s['risked'] or 0:,.0f} risked")
    if clv["n"]:
        log.info(f"  CLV ({clv['n']} bets with a distinct later capture): "
                 f"avg {clv['avg_c']:+.0f} cents, {100 * clv['pos'] / clv['n']:.0f}% positive")
    else:
        log.info("  CLV: no bets have a distinct later pre-tip capture yet — "
                 "accumulating as the collector's near-tip snapshots stack up")
    log.info("  (sample far too small for any ROI claim — CLV is the signal)")
