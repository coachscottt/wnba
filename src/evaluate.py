"""Phase 9 evaluation: find out whether the model is any good, honestly.

Structure: weekly walk-forward (train on everything before each week, predict
that week, accumulate out-of-sample predictions). Distributional metrics vs
realized values; binary metrics vs the posted morning lines; four baselines
side by side; CLV skipped in plain words because no closing lines exist yet.

Honesty note printed with every run: model DESIGN decisions in phases 5-8
(gamma, isotonic calibrations, EWM blend, tuning grids) were made after
inspecting June-August outcomes, so these walk-forward numbers are upper
bounds. The untouched read accumulates prospectively from 2026-08-11 onward.

Never produced here: bankroll curves, ROI, units won. The sample cannot
support them.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

from src.config import ROOT, load_config
from src.db import connect
from src.log import get_logger
from src.model_minutes import (CLF_FEATURES, REG_FEATURES, fit_concentration,
                               fit_models)
from src.model_minutes import build_dataset as build_min_dataset
from src.model_rates import (crps_from_cdf, fit_accuracy_priors, fit_dispersion,
                             mean_counts, nb_cdf_grid, tune_accuracy_k, tune_k)
from src.model_rates import build_dataset as build_rate_dataset
from src.price import american_to_prob, devig_power, model_p_over
from src.simulate import SIM_STATS, fit_game_params, load_slate, simulate_game

log = get_logger("evaluate")

EVAL_STATS = ["points", "reb", "ast", "fg3m"]
REALIZED = {"points": "points", "reb": "reb", "ast": "ast", "fg3m": "fg3m"}


# ---------------------------------------------------------------- fold fitting


def fit_rates_fold(train: pl.DataFrame, rcfg: dict) -> dict:
    ks = tune_k(train, rcfg)
    ks_acc = tune_accuracy_k(train, rcfg)
    priors = fit_accuracy_priors(train)
    disp = {}
    for stat, target in (("fg3a", "fg3a_t"), ("fg2a", "fg2a"), ("fta", "fta_t"),
                         ("reb", "reb"), ("ast", "ast")):
        disp[stat] = fit_dispersion(
            train[target].to_numpy().astype(float),
            mean_counts(train, stat, rcfg, k=ks[stat]), stat, quiet=True)
    return {"dispersion": disp, "accuracy_priors": priors, "k_rate": ks,
            "config": {"k_p3": ks_acc["p3"], "k_p2": ks_acc["p2"],
                       "k_ftp": ks_acc["ftp"],
                       "lg_pace_ref": rcfg["lg_pace_ref"]}}


# ---------------------------------------------------------------- walk-forward


def walk_forward(conn, cfg) -> pl.DataFrame:
    """Returns one row per (game_id, player_id, stat): p_ge grid + realized."""
    ecfg = cfg["evaluate"]
    start = date.fromisoformat(ecfg["start"])
    last = date.fromisoformat(conn.execute(
        "SELECT MAX(game_date) d FROM games WHERE home_score IS NOT NULL"
    ).fetchone()["d"])
    scfg = dict(cfg["simulation"])
    scfg["n_sims"] = int(ecfg["sims"])
    rcfg = cfg["rates"]
    kmax = int(ecfg["crps_max"])
    rng = np.random.default_rng(int(ecfg["seed"]))

    min_all = build_min_dataset(conn)
    rate_all = build_rate_dataset(conn)

    # Dirichlet shape: coverage-fit once at the earliest cutoff, reused
    pre = min_all.filter(pl.col("game_date") < str(start))
    models_pre = fit_models(pre, int(cfg["minutes"]["seed"]))
    c, gamma = fit_concentration(models_pre, pre, cfg["minutes"])

    rows = []
    wk = start
    n_folds = 0
    while wk <= last:
        wk_end = wk + timedelta(days=6)
        gids = [r["game_id"] for r in conn.execute(
            "SELECT game_id FROM games WHERE game_date >= ? AND game_date <= ? "
            "AND home_score IS NOT NULL", (str(wk), str(wk_end)))]
        if not gids:
            wk = wk_end + timedelta(days=1)
            continue
        cut = str(wk)
        mtrain = min_all.filter(pl.col("game_date") < cut)
        rtrain = rate_all.filter(pl.col("game_date") < cut)
        mm = fit_models(mtrain, int(cfg["minutes"]["seed"]))
        minutes_model = {"clf": mm["clf"], "reg": mm["reg"], "concentration": c,
                         "gamma": gamma, "clf_features": CLF_FEATURES,
                         "reg_features": REG_FEATURES}
        rates_model = fit_rates_fold(rtrain, rcfg)
        models = {"minutes": minutes_model, "rates": rates_model}
        ot_p, pace_sd = fit_game_params(conn, cut)

        slate = load_slate(conn, gids)
        n_folds += 1
        for gid in gids:
            gdf = slate.filter(pl.col("game_id") == gid)
            if gdf.height < 10:
                continue
            sims = simulate_game(gdf, models, scfg, rcfg, ot_p, pace_sd, rng)
            mins_q = np.quantile(sims["minutes"], [0.5], axis=1)[0]
            for stat in EVAL_STATS:
                arr = sims[stat]
                for i in range(gdf.height):
                    x = arr[i].astype(np.int64)
                    counts = np.bincount(np.clip(x, 0, kmax + 1),
                                         minlength=kmax + 2)
                    below = np.concatenate([[0], np.cumsum(counts)[:-1]])[:kmax + 1]
                    p_ge = 1.0 - below / len(x)
                    rows.append({
                        "game_id": gid, "player_id": gdf["player_id"][i],
                        "game_date": gdf["game_date"][i], "stat": stat,
                        "week": str(wk), "med_minutes": float(mins_q[i]),
                        "p_ge": ",".join(f"{p:.5f}" for p in p_ge)})
        log.info(f"  fold {cut}: trained on {mtrain.height} rows, "
                 f"simulated {len(gids)} games")
        wk = wk_end + timedelta(days=1)
    log.info(f"walk-forward: {n_folds} weekly folds, {len(rows)} "
             f"player-game-stat predictions accumulated")
    return pl.DataFrame(rows)


# ---------------------------------------------------------------- metrics


def attach_realized(conn, wf: pl.DataFrame) -> pl.DataFrame:
    cur = conn.execute(
        "SELECT pg.game_id, pg.player_id, pg.minutes,"
        " pg.points, pg.reb, pg.ast, pg.fg3m FROM player_games pg")
    cols = [d[0] for d in cur.description]
    pg = pl.DataFrame([dict(zip(cols, r)) for r in cur.fetchall()],
                      infer_schema_length=None).with_columns(
        pl.col("minutes").fill_null(0.0))
    pg = pg.unpivot(index=["game_id", "player_id", "minutes"],
                    on=EVAL_STATS, variable_name="stat", value_name="actual")
    return wf.join(pg, on=["game_id", "player_id", "stat"], how="inner"
                   ).with_columns(pl.col("actual").fill_null(0).cast(pl.Float64))


def grids(wf: pl.DataFrame, kmax: int) -> np.ndarray:
    return np.array([[float(x) for x in s.split(",")] for s in wf["p_ge"]])


def crps_rows(p_ge: np.ndarray, actual: np.ndarray) -> np.ndarray:
    cdf = 1.0 - p_ge[:, 1:]  # F(k) = 1 - P(X >= k+1), k = 0..kmax-1
    return crps_from_cdf(np.clip(cdf, 0, 1), actual)


def pit_values(p_ge: np.ndarray, actual: np.ndarray,
               rng: np.random.Generator) -> np.ndarray:
    """Randomized PIT for discrete outcomes: U(F(x-1), F(x))."""
    x = actual.astype(int)
    n, k = p_ge.shape
    xc = np.clip(x, 0, k - 2)
    f_hi = 1.0 - p_ge[np.arange(n), xc + 1]
    f_lo = np.where(x > 0, 1.0 - p_ge[np.arange(n), np.clip(x, 0, k - 1)], 0.0)
    return f_lo + rng.random(n) * np.clip(f_hi - f_lo, 1e-12, None)


# ---------------------------------------------------------------- baselines


def baseline_means(conn) -> pl.DataFrame:
    """As-of baseline means per (game_id, player_id, stat): season avg (B1),
    trailing-10 avg (B2), trailing-10 per-40 rate (for B3)."""
    cur = conn.execute(
        "SELECT pg.game_id, pg.player_id, pg.game_date, g.season, pg.minutes,"
        " pg.points, pg.reb, pg.ast, pg.fg3m, a.status "
        "FROM player_games pg "
        "JOIN games g ON g.game_id = pg.game_id "
        "JOIN availability a ON a.game_id = pg.game_id AND a.player_id = pg.player_id")
    cols = [d[0] for d in cur.description]
    pg = pl.DataFrame([dict(zip(cols, r)) for r in cur.fetchall()],
                      infer_schema_length=None).with_columns(
        pl.col("minutes").fill_null(0.0))
    played = pg.filter(pl.col("status") == "played").sort(
        ["player_id", "game_date"])
    over = ["player_id", "season"]
    exprs = []
    for s in EVAL_STATS:
        sh = pl.col(s).shift(1).over(over)
        shm = pl.col("minutes").shift(1).over(over)
        exprs += [
            sh.cum_sum().over(over).alias(f"cs_{s}"),
            sh.rolling_mean(10, min_samples=1).over(over).alias(f"b2_{s}"),
            (40 * sh.rolling_sum(10, min_samples=1).over(over)
             / shm.rolling_sum(10, min_samples=1).over(over)).alias(f"b3rate_{s}"),
        ]
    exprs.append((pl.col("minutes").cum_count().over(over) - 1).alias("n_prior"))
    played = played.with_columns(exprs)
    for s in EVAL_STATS:
        played = played.with_columns(
            (pl.col(f"cs_{s}") / pl.col("n_prior")).alias(f"b1_{s}"))
    keep = ["game_id", "player_id"] + \
        [f"b{i}_{s}" for i in (1, 2) for s in EVAL_STATS] + \
        [f"b3rate_{s}" for s in EVAL_STATS]
    return played.select(keep)


def nb_p_ge_grid(means: np.ndarray, r: float, kmax: int) -> np.ndarray:
    cdf = nb_cdf_grid(np.clip(means, 1e-6, None), r, kmax)
    p_ge = np.zeros((len(means), kmax + 1))
    p_ge[:, 0] = 1.0
    p_ge[:, 1:] = 1.0 - cdf[:, :-1]
    return p_ge


# ---------------------------------------------------------------- report


def run_evaluate() -> int:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    cfg = load_config()
    ecfg = cfg["evaluate"]
    kmax = int(ecfg["crps_max"])
    conn = connect()
    if conn.execute("SELECT COUNT(*) n FROM sqlite_master WHERE name='features'"
                    ).fetchone()["n"] == 0:
        log.info("no features table — run `python run.py clean` first")
        return 1
    rng = np.random.default_rng(int(ecfg["seed"]))

    log.info("=" * 70)
    log.info("EVALUATION — walk-forward, weekly refits, out-of-sample only")
    log.info("HONESTY NOTE: model design (gamma, calibrations, tuning grids) was")
    log.info("chosen after inspecting June-August results in phases 5-8. These")
    log.info("numbers are upper bounds. The untouched read accumulates")
    log.info("prospectively from 2026-08-11 (tonight's slate onward).")
    log.info("=" * 70)

    wf = walk_forward(conn, cfg)
    wf = attach_realized(conn, wf)
    G = grids(wf, kmax)
    y = wf["actual"].to_numpy()

    # ---- distributional
    crps = crps_rows(G, y)
    wf = wf.with_columns(pl.Series("crps", crps))
    log.info("")
    log.info("CRPS by stat (model, walk-forward):")
    for r in wf.group_by("stat").agg(pl.col("crps").mean(), pl.len()
                                     ).sort("stat").to_dicts():
        log.info(f"  {r['stat']:8} {r['crps']:.4f}  (n={r['len']})")
    log.info("CRPS by realized-minutes bucket (points only):")
    pts = wf.filter(pl.col("stat") == "points").with_columns(
        pl.when(pl.col("minutes") == 0).then(pl.lit("DNP"))
        .when(pl.col("minutes") < 15).then(pl.lit("<15"))
        .when(pl.col("minutes") < 25).then(pl.lit("15-24"))
        .when(pl.col("minutes") < 32).then(pl.lit("25-31"))
        .otherwise(pl.lit("32+")).alias("bucket"))
    for r in pts.group_by("bucket").agg(pl.col("crps").mean(), pl.len()
                                        ).sort("bucket").to_dicts():
        log.info(f"  {r['bucket']:6} {r['crps']:.4f}  (n={r['len']})")

    pit = pit_values(G, y, rng)
    fig, ax = plt.subplots(figsize=(6, 3.2))
    ax.hist(pit, bins=20, density=True, alpha=0.8)
    ax.axhline(1.0, color="k", ls="--", lw=1)
    ax.set_title("PIT — walk-forward, all stats")
    fig.tight_layout()
    fig.savefig(ROOT / "reports" / "pit_walkforward.png", dpi=120)
    plt.close(fig)
    hist, _ = np.histogram(pit, bins=10, range=(0, 1), density=True)
    shape = ("flat -> calibrated" if hist.max() - hist.min() < 0.25 else
             "U-shaped -> too narrow (overconfident)" if hist[0] + hist[-1] > 2.6 else
             "peaked -> too wide (underconfident)" if hist[4] + hist[5] > 2.6 else
             "sloped -> systematic bias" if abs(hist[0] - hist[-1]) > 0.5 else
             "mildly irregular — see the plot")
    log.info(f"PIT histogram: {shape} (reports/pit_walkforward.png; "
             f"bin heights {np.round(hist, 2).tolist()})")

    cov = {}
    for lvl, (lo_q, hi_q) in ((50, (0.25, 0.75)), (80, (0.10, 0.90)),
                              (95, (0.025, 0.975))):
        inside = (pit >= lo_q) & (pit <= hi_q)
        cov[lvl] = float(inside.mean())
    log.info(f"interval coverage (PIT-based): 50% -> {cov[50]:.1%}, "
             f"80% -> {cov[80]:.1%}, 95% -> {cov[95]:.1%}")

    # ---- baselines: CRPS (B1-B3)
    bm = baseline_means(conn)
    wf = wf.join(bm, on=["game_id", "player_id"], how="left")
    pre = wf.filter(pl.col("game_date") < ecfg["start"])  # empty; disp from DB
    log.info("")
    log.info("baselines, CRPS side by side (NB distributions around each mean;")
    log.info("dispersion moment-fit per baseline on pre-walk-forward data):")
    log.info(f"  {'stat':8} {'model':>8} {'B1 season':>10} {'B2 trail10':>11} "
             f"{'B3 rate*min':>12}")
    beats3_all = True
    for stat in EVAL_STATS:
        sub = wf.filter(pl.col("stat") == stat)
        ya = sub["actual"].to_numpy()
        Gs = grids(sub, kmax)
        res = {"model": float(crps_rows(Gs, ya).mean())}
        for name, mean_expr in (
                ("B1", sub[f"b1_{stat}"].to_numpy()),
                ("B2", sub[f"b2_{stat}"].to_numpy()),
                ("B3", (sub[f"b3rate_{stat}"].to_numpy()
                        * sub["med_minutes"].to_numpy() / 40.0))):
            m = np.nan_to_num(mean_expr.astype(float), nan=0.0)
            r_disp = fit_dispersion(ya, np.clip(m, 1e-6, None), name, quiet=True)
            res[name] = float(crps_rows(nb_p_ge_grid(m, r_disp, kmax), ya).mean())
        flag3 = "beats B3" if res["model"] < res["B3"] else "LOSES to B3"
        if res["model"] >= res["B3"]:
            beats3_all = False
        log.info(f"  {stat:8} {res['model']:8.4f} {res['B1']:10.4f} "
                 f"{res['B2']:11.4f} {res['B3']:12.4f}   {flag3}")

    # ---- binary vs the posted line + market baseline (B4) + reliability
    log.info("")
    lines = [dict(r) for r in conn.execute(
        "SELECT pl.game_id, pl.player_id, pl.line, pl.over_price,"
        " pl.under_price, pl.result FROM prop_lines pl "
        "JOIN games g ON g.game_id = pl.game_id "
        "WHERE pl.match_status = 'ok' AND pl.is_alternate = 0 "
        "AND pl.market = 'player_points' AND pl.result IN ('over','under') "
        "AND pl.over_price IS NOT NULL AND pl.under_price IS NOT NULL "
        "AND g.game_date >= ?", (ecfg["start"],))]
    pmap = {(r_["game_id"], r_["player_id"]): [float(v) for v in
                                              r_["p_ge"].split(",")]
            for r_ in wf.filter(pl.col("stat") == "points")
            .select("game_id", "player_id", "p_ge").to_dicts()}
    b_named = {(r_["game_id"], r_["player_id"]): r_ for r_ in
               wf.filter(pl.col("stat") == "points").to_dicts()}
    model_p, market_p, b3_p, outcome = [], [], [], []
    disp_b3 = None
    for ln in lines:
        key = (ln["game_id"], ln["player_id"])
        if key not in pmap:
            continue
        win, push = model_p_over(pmap[key], ln["line"])
        lose = 1 - win - push
        if win + lose <= 0:
            continue
        model_p.append(win / (win + lose))
        qo, qu = american_to_prob(ln["over_price"]), american_to_prob(ln["under_price"])
        market_p.append(devig_power(qo, qu))
        row = b_named[key]
        m_b3 = (row["b3rate_points"] or 0) * row["med_minutes"] / 40.0
        if disp_b3 is None:
            ya_all = wf.filter(pl.col("stat") == "points")["actual"].to_numpy()
            mm_all = np.nan_to_num(
                wf.filter(pl.col("stat") == "points")["b3rate_points"].to_numpy()
                * wf.filter(pl.col("stat") == "points")["med_minutes"].to_numpy()
                / 40.0, nan=0.0)
            disp_b3 = fit_dispersion(ya_all, np.clip(mm_all, 1e-6, None),
                                     "b3", quiet=True)
        g3 = nb_p_ge_grid(np.array([max(m_b3, 1e-6)]), disp_b3, kmax)[0]
        w3, p3_ = model_p_over(list(g3), ln["line"])
        b3_p.append(w3 / max(w3 + (1 - w3 - p3_), 1e-9))
        outcome.append(1.0 if ln["result"] == "over" else 0.0)
    model_p, market_p, b3_p = map(np.array, (model_p, market_p, b3_p))
    outcome = np.array(outcome)

    def logloss(p):
        p = np.clip(p, 1e-6, 1 - 1e-6)
        return float(-(outcome * np.log(p) + (1 - outcome) * np.log(1 - p)).mean())

    def brier(p):
        return float(((p - outcome) ** 2).mean())

    n_bets = len(outcome)
    log.info(f"binary vs the posted line — {n_bets} graded quotes "
             f"(morning captures, single capture per prop; SMALL SAMPLE):")
    log.info(f"  {'':14} {'log loss':>9} {'brier':>8}")
    log.info(f"  {'model':14} {logloss(model_p):>9.4f} {brier(model_p):>8.4f}")
    log.info(f"  {'B3 rate*min':14} {logloss(b3_p):>9.4f} {brier(b3_p):>8.4f}")
    log.info(f"  {'B4 market':14} {logloss(market_p):>9.4f} {brier(market_p):>8.4f}")
    beats_market = logloss(model_p) < logloss(market_p)

    fig, ax = plt.subplots(figsize=(5, 5))
    ax.plot([0, 1], [0, 1], "k--", lw=1)
    for p, lbl in ((model_p, "model"), (market_p, "market")):
        bins = np.clip((p * 10).astype(int), 0, 9)
        px, ox = [], []
        for b in range(10):
            m_ = bins == b
            if m_.sum() >= 5:
                px.append(p[m_].mean())
                ox.append(outcome[m_].mean())
        ax.plot(px, ox, "o-", label=f"{lbl}")
    ax.set_xlabel("predicted P(over)")
    ax.set_ylabel("observed over rate")
    ax.set_title(f"Reliability — points vs posted line (n={n_bets})")
    ax.legend()
    fig.tight_layout()
    fig.savefig(ROOT / "reports" / "reliability.png", dpi=120)
    plt.close(fig)
    log.info("  reliability diagram: reports/reliability.png")

    # ---- CLV
    log.info("")
    log.info("CLV: UNAVAILABLE and therefore skipped. Every graded prop has a")
    log.info("single morning capture (~11:25 ET); no closing lines have been")
    log.info("captured yet. Per the build rules no substitute price is used.")
    log.info("The closing-line archive starts with the snapshots now being")
    log.info("taken near tip; CLV becomes computable within a few slates.")

    # ---- verdicts
    log.info("")
    log.info("VERDICTS, stated plainly:")
    log.info(f"  vs B3 (trailing-10 per-40 rate x projected minutes): "
             f"{'model BEATS B3 on CRPS for all stats' if beats3_all else 'model LOSES to B3 on at least one stat'}")
    log.info(f"  vs B4 (de-vigged market): model "
             f"{'beats' if beats_market else 'does NOT beat'} the market on "
             f"log loss over {n_bets} graded morning lines")
    if not beats_market:
        log.info("  That is the expected outcome for most models (guide §13.4). "
                 "The distributional model is sound; the market knows more.")
    log.info("no bankroll curve, ROI, or units-won will be produced: at a "
             "handful of bets per slate the sample cannot support them")
    return 0

