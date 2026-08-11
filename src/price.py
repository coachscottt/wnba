"""Pricing: turn a model probability and a market price into a decision.

Fair probability = de-vigged consensus (median across books quoting both
sides at the same line). Best price = the price actually available. These are
different numbers used for different things: edge is measured against fair,
EV and stakes are computed against best.

No bankroll graphs, no ROI, no profit projections — phase 9 has not
established that the model works.
"""

from __future__ import annotations

import csv
import sqlite3
from collections import defaultdict
from statistics import median

import numpy as np
from scipy.optimize import brentq

from src.config import ROOT, load_config
from src.db import connect
from src.log import get_logger

log = get_logger("price")

MARKET_STAT = {"player_points": "points"}


# ---------------------------------------------------------------- conversions


def american_to_prob(odds: int) -> float:
    return (-odds) / (-odds + 100) if odds < 0 else 100 / (odds + 100)


def american_to_decimal(odds: int) -> float:
    return 1 - 100 / odds if odds < 0 else 1 + odds / 100


def devig_multiplicative(q_over: float, q_under: float) -> float:
    return q_over / (q_over + q_under)


def devig_power(q_over: float, q_under: float) -> float:
    """Solve q_over^k + q_under^k = 1; handles lopsided markets better."""
    if abs(q_over + q_under - 1) < 1e-9:
        return q_over
    k = brentq(lambda x: q_over ** x + q_under ** x - 1, 0.1, 10.0)
    return q_over ** k


# ---------------------------------------------------------------- model side


def model_p_over(p_ge: list[float], line: float) -> tuple[float, float]:
    """(P(win over), P(push)) for a points line against the P(X>=k) grid."""
    if line != int(line):  # half line: over wins iff X >= ceil(line)
        k = int(np.ceil(line))
        return (p_ge[k] if k < len(p_ge) else 0.0), 0.0
    k = int(line)
    win = p_ge[k + 1] if k + 1 < len(p_ge) else 0.0
    push = (p_ge[k] if k < len(p_ge) else 0.0) - win
    return win, push


def kelly(p_win: float, p_lose: float, dec: float) -> float:
    """Kelly fraction conditional on no push; 0 when negative."""
    tot = p_win + p_lose
    if tot <= 0 or dec <= 1:
        return 0.0
    p = p_win / tot
    b = dec - 1
    return max(0.0, (p * b - (1 - p)) / b)


# ---------------------------------------------------------------- slate


def price_slate(conn: sqlite3.Connection, sim_p_ge: dict, stamp: str) -> list[dict]:
    """sim_p_ge: {(event_id, player_id, stat): p_ge list}. Prices every
    (player, market, line) in the snapshot `stamp` with both sides quoted."""
    pcfg = load_config()["pricing"]
    method = pcfg["devig_method"]
    devig = {"multiplicative": devig_multiplicative, "power": devig_power}
    rows = conn.execute(
        "SELECT pl.*, p.player_name FROM prop_lines pl "
        "LEFT JOIN players p ON p.player_id = pl.player_id "
        "WHERE pl.captured_at_utc = ? AND pl.is_alternate = 0 "
        "AND pl.market IN ({})".format(",".join("?" * len(MARKET_STAT))),
        (stamp, *MARKET_STAT)).fetchall()

    quotes = defaultdict(list)
    for r in rows:
        if r["player_id"] and r["over_price"] and r["under_price"]:
            quotes[(r["event_id"], r["player_id"], r["market"], r["line"])].append(r)

    slate, holds_by_book = [], defaultdict(list)
    diffs_mult_power = []
    for (event_id, pid, market, line), qs in quotes.items():
        stat = MARKET_STAT[market]
        p_ge = sim_p_ge.get((event_id, pid, stat))
        if p_ge is None:
            continue
        fair_mult, fair_pow = [], []
        for q in qs:
            qo, qu = american_to_prob(q["over_price"]), american_to_prob(q["under_price"])
            holds_by_book[q["book"]].append(qo + qu - 1)
            fair_mult.append(devig_multiplicative(qo, qu))
            fair_pow.append(devig_power(qo, qu))
        fair_by = {"multiplicative": median(fair_mult), "power": median(fair_pow)}
        fair = fair_by[method]
        diffs_mult_power.append(abs(fair_by["multiplicative"] - fair_by["power"]))

        p_win_o, p_push = model_p_over(p_ge, line)
        p_win_u = 1 - p_win_o - p_push
        best_over = max(qs, key=lambda q: q["over_price"])
        best_under = max(qs, key=lambda q: q["under_price"])
        for side, p_win, p_lose, fair_side, bq, price_col in (
                ("over", p_win_o, p_win_u, fair, best_over, "over_price"),
                ("under", p_win_u, p_win_o, 1 - fair, best_under, "under_price")):
            dec = american_to_decimal(bq[price_col])
            model_side = p_win / (p_win + p_lose)  # conditional on no push
            edge = model_side - fair_side
            ev = p_win * (dec - 1) - p_lose  # per unit staked; push returns stake
            f_full = kelly(p_win, p_lose, dec)
            slate.append({
                "player": bq["player_name"] or bq["player_name_raw"],
                "market": market, "line": line, "side": side,
                "best_price": bq[price_col], "book": bq["book"],
                "model_p": round(model_side, 4),
                "fair_p": round(fair_side, 4),
                "edge": round(edge, 4), "ev_per_unit": round(ev, 4),
                "p_push": round(p_push, 4), "is_whole_line": int(line == int(line)),
                "n_books": len(qs),
                "kelly_full": round(f_full, 4),
                "kelly_frac": round(f_full * float(pcfg["kelly_fraction"]), 4),
                "bet_flag": int(edge >= float(pcfg["min_edge"]) and ev > 0),
            })

    log.info("hold per two-way market, by book (median [min..max], n):")
    for book, hs in sorted(holds_by_book.items()):
        log.info(f"  {book:16} {median(hs):6.1%} "
                 f"[{min(hs):5.1%} .. {max(hs):5.1%}]  n={len(hs)}")
    if diffs_mult_power:
        log.info(f"devig method comparison (multiplicative vs power): "
                 f"median |diff| {median(diffs_mult_power):.4f}, "
                 f"max {max(diffs_mult_power):.4f} across "
                 f"{len(diffs_mult_power)} markets -> using {method}")
    return sorted(slate, key=lambda r: r["edge"], reverse=True)


def write_slate_csv(slate: list[dict], date_str: str) -> str:
    (ROOT / "reports").mkdir(exist_ok=True)
    path = ROOT / "reports" / f"slate_{date_str}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(slate[0].keys()))
        w.writeheader()
        w.writerows(slate)
    return str(path)


# ---------------------------------------------------------------- project


def run_project() -> int:
    from datetime import datetime, timezone

    import polars as pl

    from src.features import projection_features
    from src.simulate import (SIM_STATS, fit_game_params, load_models,
                              simulate_game)

    cfg = load_config()
    conn = connect()
    row = conn.execute(
        "SELECT MAX(captured_at_utc) m FROM prop_lines").fetchone()
    if not row or not row["m"]:
        log.info("no priced snapshots — run `python run.py update` then "
                 "`python run.py clean` first")
        return 1
    stamp = row["m"]
    age_h = (datetime.now(timezone.utc)
             - datetime.fromisoformat(stamp.replace("Z", "+00:00"))
             ).total_seconds() / 3600
    log.info(f"pricing snapshot {stamp} ({age_h:.1f}h old)"
             + (" — STALE, run `python run.py update` for tonight's lines"
                if age_h > 8 else ""))

    events = [dict(r) for r in conn.execute(
        "SELECT * FROM odds_event_map WHERE event_id IN "
        "(SELECT DISTINCT event_id FROM prop_lines WHERE captured_at_utc = ?) "
        "AND commence_time_utc >= ?", (stamp, stamp))]
    if not events:
        log.info("no upcoming games in this snapshot — nothing to project")
        return 0
    log.info(f"projecting {len(events)} upcoming games")

    try:
        models = load_models()
    except FileNotFoundError:
        log.info("no trained model found, run `python run.py train` first")
        return 1
    proj = projection_features(conn, events)
    if proj.is_empty():
        log.info("no projection features built — check team-name matching above")
        return 1
    names = {str(r["player_id"]): (r["player_name"], r["position"])
             for r in conn.execute("SELECT player_id, player_name, position FROM players")}
    proj = proj.with_columns(
        pl.col("player_id").replace_strict(
            {k: v[0] for k, v in names.items()}, default=None).alias("player_name"),
        pl.col("player_id").replace_strict(
            {k: v[1] for k, v in names.items()}, default=None).alias("position"),
        ((pl.col("f_team_ortg") - pl.col("f_team_drtg"))
         - (pl.col("f_opp_ortg") - pl.col("f_opp_drtg"))).alias("blowout_proxy"))

    scfg, rcfg = cfg["simulation"], cfg["rates"]
    ot_p, pace_sd = fit_game_params(conn, "9999-12-31")  # all completed games
    ot_p = float(scfg["ot_prob"] or ot_p)
    pace_sd = float(scfg["pace_sd"] or pace_sd)
    rng = np.random.default_rng(int(scfg["seed"]))
    kmax = int(scfg["p_ge_max"])

    sim_p_ge = {}
    for gid in proj["game_id"].unique().to_list():
        gdf = proj.filter(pl.col("game_id") == gid)
        sims = simulate_game(gdf, models, scfg, rcfg, ot_p, pace_sd, rng)
        event_id = gid.removeprefix("proj_")
        for stat in SIM_STATS:
            arr = sims[stat]
            for i, pid in enumerate(gdf["player_id"].to_list()):
                x = arr[i].astype(np.int64)
                counts = np.bincount(np.clip(x, 0, kmax + 1), minlength=kmax + 2)
                below = np.concatenate([[0], np.cumsum(counts)[:-1]])[:kmax + 1]
                sim_p_ge[(event_id, pid, stat)] = list(1.0 - below / len(x))

    slate = price_slate(conn, sim_p_ge, stamp)
    if not slate:
        log.info("no two-way quotes matched to simulations — nothing to price")
        return 0
    path = write_slate_csv(slate, stamp.split("T")[0])
    n_bets = sum(r["bet_flag"] for r in slate)
    pcfg = cfg["pricing"]
    log.info("")
    log.info(f"slate: {len(slate)} priced sides, {n_bets} at/above the "
             f"{float(pcfg['min_edge']):.0%} edge floor with positive EV")
    log.info(f"  {'player':22} {'line':>6} {'side':>5} {'best':>6} "
             f"{'book':12} {'model':>6} {'fair':>6} {'edge':>6} "
             f"{'kelly/4':>7}")
    for r in slate[:15]:
        log.info(f"  {r['player'][:22]:22} {r['line']:>6} {r['side']:>5} "
                 f"{r['best_price']:>+6} {r['book'][:12]:12} "
                 f"{r['model_p']:>6.1%} {r['fair_p']:>6.1%} "
                 f"{r['edge']:>+6.1%} {r['kelly_frac']:>7.2%}")
    log.info(f"full slate: {path}")
    log.info("reminder: no ROI/profit claims until phase 9 validates the model")
    return 0
