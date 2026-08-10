"""Minutes model: the most important model in the project.

Two stages over pregame-available players (played or dnp_coach — known
absences are handled upstream by availability):

  1. P(minutes = 0)      — gradient-boosted classifier
  2. minutes | plays     — expected SHARE of team minutes via gradient-boosted
                           regressor, turned into a distribution by a Dirichlet
                           over the players who play in each simulation

Simulated team minutes sum to exactly 200 by construction (share x 200);
+25 per period only after an explicit simulated overtime event.

Backtest caveat, stated plainly: evaluation conditions on the historical
pregame availability snapshot — the backtest KNOWS who was ruled out, which a
9 AM projection would not. Results are optimistic by that gap.
"""

from __future__ import annotations

import pickle
import sqlite3

import numpy as np
import polars as pl
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.metrics import log_loss

from src.config import ROOT, load_config
from src.db import connect
from src.log import get_logger

log = get_logger("minutes")

MODEL_PATH = ROOT / "models" / "minutes.pkl"

CLF_FEATURES = [
    "f_min_l3", "f_min_l5", "f_min_l10", "f_min_last", "f_min_share_l5",
    "f_start_rate_l10", "f_started", "f_games_played", "f_min_vacated",
    "f_min_vacated_samepos", "f_bh_out", "f_days_rest", "f_b2b", "f_long_break",
    "f_game_number", "f_home",
]
REG_FEATURES = CLF_FEATURES + ["f_usage_l5", "f_tz_crossed", "blowout_proxy"]


def build_dataset(conn: sqlite3.Connection) -> pl.DataFrame:
    cur = conn.execute(
        "SELECT f.*, pg.minutes, g.overtime_periods AS ot,"
        " (200 + 25 * g.overtime_periods) AS team_min "
        "FROM features f "
        "JOIN player_games pg ON pg.game_id = f.game_id AND pg.player_id = f.player_id "
        "JOIN games g ON g.game_id = f.game_id "
        "WHERE f.status IN ('played', 'dnp_coach')")
    cols = [d[0] for d in cur.description]
    df = pl.DataFrame([dict(zip(cols, r)) for r in cur.fetchall()],
                      infer_schema_length=None)
    return df.with_columns(
        pl.col("minutes").fill_null(0.0),
        ((pl.col("f_team_ortg") - pl.col("f_team_drtg"))
         - (pl.col("f_opp_ortg") - pl.col("f_opp_drtg"))).alias("blowout_proxy"),
    ).with_columns(
        (pl.col("minutes") == 0).cast(pl.Int32).alias("y_dnp"),
        (pl.col("minutes") / pl.col("team_min")).alias("y_share"),
    )


def to_numpy(df: pl.DataFrame, cols: list[str]) -> np.ndarray:
    return df.select([pl.col(c).cast(pl.Float64) for c in cols]).to_numpy()


# ---------------------------------------------------------------- fitting


class CalibratedDnp:
    """HGB classifier + isotonic recalibration fit on a later, disjoint,
    time-ordered slice of training data (never the holdout)."""

    def __init__(self, clf, iso):
        self.clf, self.iso = clf, iso

    def predict_proba(self, X):
        p = self.iso.predict(self.clf.predict_proba(X)[:, 1])
        return np.column_stack([1 - p, p])


class CalibratedShare:
    """HGB share regressor + isotonic correction fit on the calibration slice.
    Tree regressors compress the extremes — high-minute players' shares are
    systematically under-predicted, which starves stars of simulated minutes."""

    def __init__(self, reg, iso):
        self.reg, self.iso = reg, iso

    def predict(self, X):
        return np.clip(self.iso.predict(self.reg.predict(X)), 1e-4, None)


def fit_models(train: pl.DataFrame, seed: int) -> dict:
    from sklearn.isotonic import IsotonicRegression

    # time-ordered 80/20 split within train: fit early, calibrate late
    dates = train["game_date"].sort()
    cal_cut = dates[int(0.8 * len(dates))]
    early = train.filter(pl.col("game_date") < cal_cut)
    late = train.filter(pl.col("game_date") >= cal_cut)

    clf = HistGradientBoostingClassifier(
        max_iter=300, learning_rate=0.05, max_depth=4, random_state=seed)
    clf.fit(to_numpy(early, CLF_FEATURES), early["y_dnp"].to_numpy())
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(clf.predict_proba(to_numpy(late, CLF_FEATURES))[:, 1],
            late["y_dnp"].to_numpy())

    played = train.filter(pl.col("y_dnp") == 0)
    early_p = played.filter(pl.col("game_date") < cal_cut)
    late_p = played.filter(pl.col("game_date") >= cal_cut)
    reg = HistGradientBoostingRegressor(
        max_iter=400, learning_rate=0.05, max_depth=5, random_state=seed)
    reg.fit(to_numpy(early_p, REG_FEATURES), early_p["y_share"].to_numpy())
    iso_share = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=0.25)
    iso_share.fit(reg.predict(to_numpy(late_p, REG_FEATURES)),
                  late_p["y_share"].to_numpy())
    return {"clf": CalibratedDnp(clf, iso),
            "reg": CalibratedShare(reg, iso_share), "cal_cut": cal_cut}


def predict_p_mu(models: dict, df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(
        pl.Series("p_dnp",
                  models["clf"].predict_proba(to_numpy(df, CLF_FEATURES))[:, 1]),
        pl.Series("mu_raw", np.clip(
            models["reg"].predict(to_numpy(df, REG_FEATURES)), 1e-4, None)))


def sim_quantiles(df: pl.DataFrame, c: float, sims: int,
                  rng: np.random.Generator, gamma: float = 1.0) -> pl.DataFrame:
    """Simulate every team-game in df (needs p_dnp/mu_raw); returns per-player
    quantiles of the full two-stage minutes distribution."""
    QS = [0.5, 0.25, 0.75, 0.10, 0.90, 0.025, 0.975]
    names = ["med", "lo50", "hi50", "lo80", "hi80", "lo95", "hi95"]
    keys, vals = [], []
    for (gid, _), grp in df.group_by(["game_id", "team_id"],
                                     maintain_order=True):
        m = simulate_team(grp["p_dnp"].to_numpy(), grp["mu_raw"].to_numpy(),
                          c, sims, rng, gamma=gamma)
        assert np.abs(m.sum(axis=1) - 200.0).max() < 1e-6, "sum constraint violated"
        q = np.quantile(m, QS, axis=0)
        for j, pid in enumerate(grp["player_id"]):
            keys.append((gid, pid))
            vals.append(q[:, j])
    out = pl.DataFrame({"game_id": [k[0] for k in keys],
                        "player_id": [k[1] for k in keys]})
    arr = np.array(vals)
    return out.with_columns(
        *[pl.Series(n, arr[:, i]) for i, n in enumerate(names)])


def coverage_of(df: pl.DataFrame, pred: pl.DataFrame) -> dict:
    ev = df.join(pred, on=["game_id", "player_id"], how="inner")
    y = ev["minutes"].to_numpy()
    return {lvl: float(((y >= ev[f"lo{lvl}"].to_numpy())
                        & (y <= ev[f"hi{lvl}"].to_numpy())).mean())
            for lvl in (50, 80, 95)}


def fit_concentration(models: dict, train: pl.DataFrame,
                      mcfg: dict) -> tuple[float, float]:
    """Pick the Dirichlet concentration c AND share-exponent gamma by
    matching interval COVERAGE on the late-train calibration slice (never
    the holdout). A likelihood fit conflates the regressor's mu-error with
    true rotation dispersion and lands far too low; a single global c cannot
    calibrate starters and bench at once — gamma>1 tightens big shares."""
    late = predict_p_mu(
        models, train.filter(pl.col("game_date") >= models["cal_cut"]))
    if mcfg.get("eval_regulation_only", True):
        late = late.filter(pl.col("ot") == 0)
    rng = np.random.default_rng(int(mcfg["seed"]) + 1)
    grid = np.geomspace(float(mcfg["concentration_min"]),
                        float(mcfg["concentration_max"]),
                        int(mcfg["concentration_points"]))
    best, best_err = None, None
    for gamma in mcfg.get("gamma_grid", [1.0, 1.15, 1.3, 1.5]):
        for c in grid:
            cov = coverage_of(late, sim_quantiles(late, float(c), 300, rng,
                                                  gamma=float(gamma)))
            # minimize the WORST level's deviation — same criterion the gate applies
            err = max(abs(cov[lvl] - lvl / 100) for lvl in (50, 80, 95))
            if best_err is None or err < best_err:
                best, best_err = (float(c), float(gamma)), err
    log.info(f"Dirichlet (c, gamma) by calibration-slice coverage: "
             f"c = {best[0]:.0f}, gamma = {best[1]:g} "
             f"(max coverage deviation {best_err:.3f})")
    return best


# ---------------------------------------------------------------- simulate


def simulate_team(p_dnp: np.ndarray, mu_raw: np.ndarray, c: float, sims: int,
                  rng: np.random.Generator, ot_periods: int = 0,
                  gamma: float = 1.0) -> np.ndarray:
    """Simulate minutes for one team-game. Returns (sims, n_players).
    Team minutes sum to exactly 200 (+25 per explicit overtime period).
    gamma > 1 concentrates alpha on big shares — established starters get
    relatively tighter minutes than the bench (a single global c cannot fit
    both tiers)."""
    n = len(p_dnp)
    total = 200.0 + 25.0 * ot_periods
    plays = rng.random((sims, n)) >= p_dnp[None, :]
    # a team must field 5 players; force the 5 least-likely-DNP in degenerate sims
    order = np.argsort(p_dnp)
    too_few = plays.sum(axis=1) < 5
    if too_few.any():
        plays[np.ix_(too_few, order[:5])] = True
    mu = np.clip(mu_raw, 1e-4, None) ** gamma
    mp = mu[None, :] * plays
    alpha = c * mp / mp.sum(axis=1, keepdims=True)
    g = rng.gamma(np.clip(alpha, 0, None))
    shares = g / g.sum(axis=1, keepdims=True)
    return shares * total


# ---------------------------------------------------------------- evaluate


def evaluate(models: dict, c: float, test: pl.DataFrame, mcfg: dict,
             gamma: float = 1.0) -> dict:
    rng = np.random.default_rng(int(mcfg["seed"]))
    test = predict_p_mu(models, test)
    pred = sim_quantiles(test, c, int(mcfg["sims_eval"]), rng, gamma=gamma)
    ev = test.join(pred, on=["game_id", "player_id"], how="inner")

    y = ev["minutes"].to_numpy()
    mae_model = float(np.abs(ev["med"].to_numpy() - y).mean())
    mae_t5 = float(np.abs(ev["f_min_l5"].fill_null(0.0).to_numpy() - y).mean())
    mae_last = float(np.abs(ev["f_min_last"].fill_null(0.0).to_numpy() - y).mean())
    cov = {
        lvl: float(((y >= ev[f"lo{lvl}"].to_numpy())
                    & (y <= ev[f"hi{lvl}"].to_numpy())).mean())
        for lvl in (50, 80, 95)
    }
    ll = float(log_loss(ev["y_dnp"].to_numpy(), ev["p_dnp"].to_numpy(), labels=[0, 1]))

    # DNP calibration: deciles of predicted probability vs observed rate
    calib = (ev.select("p_dnp", "y_dnp")
             .with_columns((pl.col("p_dnp") * 10).floor().clip(0, 9).alias("bin"))
             .group_by("bin").agg(pl.col("p_dnp").mean().alias("pred"),
                                  pl.col("y_dnp").mean().alias("obs"),
                                  pl.len().alias("n")).sort("bin"))
    return {"n": ev.height, "mae_model": mae_model, "mae_t5": mae_t5,
            "mae_last": mae_last, "coverage": cov, "dnp_logloss": ll,
            "calibration": calib}


def plot_calibration(calib: pl.DataFrame) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    (ROOT / "reports").mkdir(exist_ok=True)
    fig, ax = plt.subplots(figsize=(5, 5))
    ax.plot([0, 1], [0, 1], "k--", lw=1, label="perfect")
    ax.plot(calib["pred"], calib["obs"], "o-", label="model")
    ax.set_xlabel("predicted P(DNP)")
    ax.set_ylabel("observed DNP rate")
    ax.set_title("DNP classifier calibration (holdout)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(ROOT / "reports" / "minutes_dnp_calibration.png", dpi=120)
    plt.close(fig)


# ---------------------------------------------------------------- entry point


def run_train() -> int:
    cfg = load_config()
    mcfg = cfg["minutes"]
    conn = connect()
    if conn.execute("SELECT COUNT(*) n FROM sqlite_master WHERE name='features'"
                    ).fetchone()["n"] == 0:
        log.info("no features table found — run `python run.py clean` first")
        return 1
    df = build_dataset(conn)
    cut = mcfg["holdout_start"]
    train = df.filter(pl.col("game_date") < cut)
    test = df.filter(pl.col("game_date") >= cut)
    if mcfg["eval_regulation_only"]:
        n0 = test.height
        test = test.filter(pl.col("ot") == 0)
        log.info(f"holdout: {n0} rows -> {test.height} in regulation games "
                 f"(OT evaluation is a phase 7 concern)")
    log.info(f"train {train.height} rows (< {cut}), holdout {test.height} rows; "
             f"time-based split, never random")

    models = fit_models(train, int(mcfg["seed"]))
    c, gamma = fit_concentration(models, train, mcfg)
    res = evaluate(models, c, test, mcfg, gamma=gamma)
    plot_calibration(res["calibration"])

    log.info("")
    log.info("NOTE: this backtest conditions on historical pregame availability —")
    log.info("it knows who was ruled out. A morning projection would not.")
    log.info("")
    log.info(f"minutes model evaluation, holdout n={res['n']} player-games:")
    log.info(f"  {'':24} {'MAE (min)':>10}")
    log.info(f"  {'model (sim median)':24} {res['mae_model']:>10.2f}")
    log.info(f"  {'baseline: trailing-5':24} {res['mae_t5']:>10.2f}")
    log.info(f"  {'baseline: last game':24} {res['mae_last']:>10.2f}")
    log.info(f"  interval coverage: 50% -> {res['coverage'][50]:.1%}, "
             f"80% -> {res['coverage'][80]:.1%}, 95% -> {res['coverage'][95]:.1%}")
    log.info(f"  DNP classifier log loss: {res['dnp_logloss']:.4f}")
    log.info("  DNP calibration (deciles): predicted vs observed")
    for r in res["calibration"].to_dicts():
        log.info(f"    bin {int(r['bin'])}: pred {r['pred']:.3f} "
                 f"obs {r['obs']:.3f} (n={r['n']})")
    log.info("  calibration plot: reports/minutes_dnp_calibration.png")

    beats = res["mae_model"] < res["mae_t5"]
    cov_ok = all(abs(res["coverage"][lvl] - lvl / 100) < 0.07 for lvl in (50, 80, 95))
    if not beats:
        log.info("VERDICT: model does NOT beat the trailing-5 baseline on MAE. "
                 "Stopping here per the build spec — do not proceed to phase 6.")
        return 1
    if not cov_ok:
        log.info("VERDICT: MAE beats baselines but interval coverage is off by "
                 ">7pts somewhere — treat as NOT passing. Stopping per spec.")
        return 1
    log.info("VERDICT: beats trailing-5 on MAE with calibrated coverage.")

    # refit on all data for the saved production model
    models_full = fit_models(df, int(mcfg["seed"]))
    c_full, gamma_full = fit_concentration(models_full, df, mcfg)
    MODEL_PATH.parent.mkdir(exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump({"clf": models_full["clf"], "reg": models_full["reg"],
                     "concentration": c_full, "gamma": gamma_full,
                     "clf_features": CLF_FEATURES,
                     "reg_features": REG_FEATURES,
                     "trained_through": str(df["game_date"].max()),
                     "holdout_metrics": {k: v for k, v in res.items()
                                         if k != "calibration"}}, f)
    log.info(f"saved models/minutes.pkl (refit on all data through "
             f"{df['game_date'].max()})")
    return 0
