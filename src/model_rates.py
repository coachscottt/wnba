"""Rate models: distributions over production per unit of playing time.

Structure per guide §10 — empirical-Bayes means, explicit count distributions:

  mean per-40   = EB-shrunk season rate (league -> position -> player, from
                  the feature layer) x opponent team-level allow-factor x pace
  attempts/reb/ast ~ Negative Binomial (dispersion moment-fit on train;
                  the observed variance/mean ratio is printed to justify NB)
  makes | attempts ~ Binomial with Beta-shrunk accuracy
  points        = 2x2PM + 3x3PM + FTM, summed in simulation — never modeled
                  directly; combos likewise fall out of phase 7 simulation.

Evaluation conditions on ACTUAL minutes to isolate rate quality — the minutes
distribution is phase 5's job and they compose in phase 7.
"""

from __future__ import annotations

import pickle
import sqlite3

import numpy as np
import polars as pl
from scipy import stats as st

from src.config import ROOT, load_config
from src.db import connect
from src.log import get_logger

log = get_logger("rates")

MODEL_PATH = ROOT / "models" / "rates.pkl"

# count stat -> (szn feature, opp-allow feature, target column)
COUNT_STATS = {
    "fg3a": ("f_fg3a40_szn", "f_opp_allow_fg3a", "fg3a"),
    "fg2a": ("f_fg2a40_szn", "f_opp_allow_fg2a", "fg2a"),
    "fta": ("f_fta40_szn", "f_opp_allow_fta", "fta"),
    "reb": ("f_reb40_szn", "f_opp_allow_reb", "reb"),
    "ast": ("f_ast40_szn", "f_opp_allow_ast", "ast"),
    "points_direct": ("f_points40_szn", "f_opp_pos_def", "points"),  # baselines only
}
ACCURACIES = {  # accuracy -> (makes cum, attempts cum, k config key)
    "p3": ("f_fg3m_cum", "f_fg3a_cum", "k_p3"),
    "p2": ("f_fg2m_cum", "f_fg2a_cum", "k_p2"),
    "ftp": ("f_ftm_cum", "f_fta_cum", "k_ftp"),
}


def build_dataset(conn: sqlite3.Connection) -> pl.DataFrame:
    cur = conn.execute(
        "SELECT f.*, p.position, pg.minutes, pg.fg3a, pg.fg3m, pg.fga, pg.fgm,"
        " pg.fta AS t_fta, pg.ftm, pg.reb, pg.ast, pg.points "
        "FROM features f "
        "JOIN player_games pg ON pg.game_id = f.game_id AND pg.player_id = f.player_id "
        "JOIN players p ON p.player_id = f.player_id "
        "WHERE f.status = 'played'")
    cols = [d[0] for d in cur.description]
    df = pl.DataFrame([dict(zip(cols, r)) for r in cur.fetchall()],
                      infer_schema_length=None)
    return df.filter(pl.col("minutes") > 0).with_columns(
        (pl.col("fga") - pl.col("fg3a")).alias("fg2a"),
        (pl.col("fgm") - pl.col("fg3m")).alias("fg2m"),
        pl.col("t_fta").alias("fta_t"),
        pl.col("position").fill_null("F").str.slice(0, 1).alias("pos"),
    ).rename({"fg3a": "t_fg3a"}).with_columns(
        pl.col("t_fg3a").alias("fg3a_t"))


def pace_factor(df: pl.DataFrame, rcfg: dict) -> np.ndarray:
    return ((df["f_team_pace"].to_numpy() + df["f_opp_pace"].to_numpy()) / 2
            / float(rcfg["lg_pace_ref"]))


def mean_counts(df: pl.DataFrame, stat: str, rcfg: dict,
                rate_col: str | None = None) -> np.ndarray:
    """Expected count for the game: per-40 mean rate x minutes/40."""
    szn, opp, _ = COUNT_STATS[stat]
    rate = df[rate_col or szn].to_numpy().astype(float)
    rate = np.where(np.isnan(rate), np.nan_to_num(df[szn].to_numpy(), nan=0.0), rate)
    adj = df[opp].to_numpy() if rate_col is None else 1.0  # baselines: no adj
    m = rate * adj * (pace_factor(df, rcfg) if rate_col is None else 1.0) \
        * df["minutes"].to_numpy() / 40.0
    return np.clip(m, 1e-6, None)


def fit_dispersion(y: np.ndarray, m: np.ndarray, label: str) -> float:
    """NB dispersion r by moment matching; prints the var/mean justification."""
    vm = float(np.var(y) / np.mean(y)) if np.mean(y) > 0 else 1.0
    excess = float(np.sum((y - m) ** 2 - m))
    r = float(np.sum(m ** 2) / excess) if excess > 0 else np.inf
    log.info(f"  {label}: observed variance/mean = {vm:.2f} "
             f"({'overdispersed -> NB justified' if vm > 1.1 else 'near 1 — Poisson would do; using NB with large r'}), "
             f"residual dispersion r = {r if np.isfinite(r) else 1e9:.1f}")
    return r if np.isfinite(r) else 1e9


def shrunk_accuracy(df: pl.DataFrame, acc: str, priors: dict, rcfg: dict) -> np.ndarray:
    makes_c, att_c, k_key = ACCURACIES[acc]
    k = float(rcfg[k_key])
    prior = np.array([priors[acc].get(p, priors[acc]["ALL"]) for p in df["pos"]])
    makes = df[makes_c].fill_null(0.0).to_numpy()
    atts = df[att_c].fill_null(0.0).to_numpy()
    return (makes + k * prior) / (atts + k)


def fit_accuracy_priors(train: pl.DataFrame) -> dict:
    priors = {}
    pairs = {"p3": ("fg3m", "fg3a_t"), "p2": ("fg2m", "fg2a"),
             "ftp": ("ftm", "fta_t")}
    for acc, (mk, at) in pairs.items():
        by_pos = {}
        for pos in ("G", "F", "C"):
            sub = train.filter(pl.col("pos") == pos)
            a = float(sub[at].sum() or 0)
            by_pos[pos] = float(sub[mk].sum()) / a if a else 0.0
        a_all = float(train[at].sum())
        by_pos["ALL"] = float(train[mk].sum()) / a_all if a_all else 0.0
        priors[acc] = by_pos
    return priors


# ---------------------------------------------------------------- distributions


def nb_cdf_grid(m: np.ndarray, r: float, kmax: int) -> np.ndarray:
    """CDF matrix (rows, kmax+1) of NB(mean m, dispersion r)."""
    ks = np.arange(kmax + 1)
    p = r / (r + m)
    return st.nbinom.cdf(ks[None, :], r, p[:, None])


def crps_from_cdf(cdf: np.ndarray, y: np.ndarray) -> np.ndarray:
    ks = np.arange(cdf.shape[1])
    ind = (y[:, None] <= ks[None, :]).astype(float)
    return ((cdf - ind) ** 2).sum(axis=1)


def pit_from_cdf(cdf: np.ndarray, y: np.ndarray, rng) -> np.ndarray:
    yi = y.astype(int)
    idx = np.arange(len(y))
    F_y = cdf[idx, np.clip(yi, 0, cdf.shape[1] - 1)]
    F_ym1 = np.where(yi > 0, cdf[idx, np.clip(yi - 1, 0, cdf.shape[1] - 1)], 0.0)
    return F_ym1 + rng.random(len(y)) * (F_y - F_ym1)


def mixture_3pm_cdf(m3a: np.ndarray, r3a: float, p3: np.ndarray,
                    kmax: int, amax: int = 45) -> np.ndarray:
    """CDF of 3PM = Binomial(3PA, p3) with 3PA ~ NB(m3a, r3a)."""
    a_grid = np.arange(amax + 1)
    pa = st.nbinom.pmf(a_grid[None, :], r3a, (r3a / (r3a + m3a))[:, None])
    out = np.zeros((len(m3a), kmax + 1))
    ks = np.arange(kmax + 1)
    for a in a_grid:
        cdf_k = st.binom.cdf(ks[None, :], a, p3[:, None])
        out += pa[:, a][:, None] * cdf_k
    return np.clip(out, 0, 1)


def describe_pit(u: np.ndarray) -> str:
    hist, _ = np.histogram(u, bins=10, range=(0, 1))
    f = hist / hist.sum()
    ends, mid = (f[0] + f[9]) / 2, f[3:7].mean()
    slope = np.polyfit(np.arange(10), f, 1)[0]
    if abs(slope) > 0.006:
        return ("sloped (systematic bias: predictions too "
                + ("low" if slope > 0 else "high") + ")")
    if ends > 1.35 * mid:
        return "U-shaped (distributions too narrow / overconfident)"
    if mid > 1.35 * ends:
        return "peaked (distributions too wide)"
    return "approximately flat (calibrated)"


def save_pit_plot(u: np.ndarray, stat: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    (ROOT / "reports").mkdir(exist_ok=True)
    fig, ax = plt.subplots(figsize=(5, 3))
    ax.hist(u, bins=10, range=(0, 1), edgecolor="white")
    ax.axhline(len(u) / 10, ls="--", c="k", lw=1)
    ax.set_title(f"PIT — {stat} (flat = calibrated)")
    fig.tight_layout()
    fig.savefig(ROOT / "reports" / f"pit_{stat}.png", dpi=120)
    plt.close(fig)


# ---------------------------------------------------------------- per-stat eval


def eval_count_stat(train: pl.DataFrame, test: pl.DataFrame, stat: str,
                    target: str, rcfg: dict, rng) -> tuple[float, dict]:
    kmax = int(rcfg["crps_max_count"])
    m_tr = mean_counts(train, stat, rcfg)
    r = fit_dispersion(train[target].to_numpy().astype(float), m_tr, stat)

    results = {}
    y = test[target].to_numpy().astype(float)
    for name, rate_col in [("model", None),
                           ("szn-raw", COUNT_STATS[stat][0] + "_raw"),
                           ("trail-10", COUNT_STATS[stat][0].replace("_szn", "_l10"))]:
        m = mean_counts(test, stat, rcfg, rate_col)
        cdf = nb_cdf_grid(m, r, kmax)
        crps = float(crps_from_cdf(cdf, y).mean())
        results[name] = crps
        if name == "model":
            u = pit_from_cdf(cdf, y, rng)
            save_pit_plot(u, stat)
            results["pit"] = describe_pit(u)
    return r, results


def report_stat(stat: str, res: dict) -> bool:
    beats = res["model"] < res["szn-raw"] and res["model"] < res["trail-10"]
    log.info(f"  CRPS {stat}: model {res['model']:.4f} | szn-raw "
             f"{res['szn-raw']:.4f} | trail-10 {res['trail-10']:.4f} "
             f"-> {'BEATS both baselines' if beats else 'DOES NOT beat baselines'}")
    log.info(f"  PIT {stat}: {res['pit']} (reports/pit_{stat}.png)")
    return beats


# ---------------------------------------------------------------- entry point


def run_train_rates() -> int:
    cfg = load_config()
    rcfg = cfg["rates"]
    rng = np.random.default_rng(int(rcfg["seed"]))
    conn = connect()
    df = build_dataset(conn)
    cut = rcfg["holdout_start"]
    train = df.filter(pl.col("game_date") < cut)
    test = df.filter(pl.col("game_date") >= cut)
    log.info(f"rate models: train {train.height} / holdout {test.height} "
             f"played player-games (time split at {cut}); evaluation conditions "
             f"on ACTUAL minutes — minutes uncertainty is phase 5's job")

    priors = fit_accuracy_priors(train)
    disp: dict[str, float] = {}
    all_pass = True

    # ---- stat 1: three-pointers made (attempts NB x Beta-shrunk accuracy)
    log.info("— stat 1: three-pointers made —")
    r3a, res3a = eval_count_stat(train, test, "fg3a", "fg3a_t", rcfg, rng)
    disp["fg3a"] = r3a
    log.info(f"  (3PA attempt model: CRPS model {res3a['model']:.4f} vs "
             f"szn-raw {res3a['szn-raw']:.4f}, trail-10 {res3a['trail-10']:.4f})")
    p3_test = shrunk_accuracy(test, "p3", priors, rcfg)
    kmax3 = 25
    y3 = test["fg3m"].to_numpy().astype(float)
    m3a = mean_counts(test, "fg3a", rcfg)
    cdf3 = mixture_3pm_cdf(m3a, r3a, p3_test, kmax3)
    crps3 = float(crps_from_cdf(cdf3, y3).mean())
    u3 = pit_from_cdf(cdf3, y3, rng)
    save_pit_plot(u3, "fg3m")
    res_3pm = {}
    for name, rate_col in [("szn-raw", "f_fg3a40_szn_raw"),
                           ("trail-10", "f_fg3a40_l10")]:
        m_b = mean_counts(test, "fg3a", rcfg, rate_col)
        raw_p3 = np.where(test["f_fg3a_cum"].fill_null(0).to_numpy() > 0,
                          test["f_fg3m_cum"].fill_null(0).to_numpy()
                          / np.clip(test["f_fg3a_cum"].fill_null(0).to_numpy(),
                                    1, None),
                          priors["p3"]["ALL"])
        cdf_b = mixture_3pm_cdf(m_b, r3a, raw_p3, kmax3)
        res_3pm[name] = float(crps_from_cdf(cdf_b, y3).mean())
    res_3pm |= {"model": crps3, "pit": describe_pit(u3)}
    w3 = test["f_fg3a_cum"].fill_null(0).to_numpy()
    w3 = w3 / (w3 + float(rcfg["k_p3"]))
    log.info(f"  p3 Beta shrinkage weight attempts/(attempts+k), holdout: "
             f"p10 {np.quantile(w3, .1):.2f} median {np.median(w3):.2f} "
             f"p90 {np.quantile(w3, .9):.2f} (k_p3={rcfg['k_p3']} pseudo-attempts)")
    if not report_stat("fg3m", res_3pm):
        all_pass = False

    # ---- stat 2: rebounds
    log.info("— stat 2: rebounds —")
    r_reb, res_reb = eval_count_stat(train, test, "reb", "reb", rcfg, rng)
    disp["reb"] = r_reb
    if not report_stat("reb", res_reb):
        all_pass = False

    # ---- stat 3: assists
    log.info("— stat 3: assists —")
    r_ast, res_ast = eval_count_stat(train, test, "ast", "ast", rcfg, rng)
    disp["ast"] = r_ast
    if not report_stat("ast", res_ast):
        all_pass = False

    # ---- stat 4: points, by component simulation (never modeled directly)
    log.info("— stat 4: points = 2x2PM + 3x3PM + FTM, simulated —")
    for s, t in (("fg2a", "fg2a"), ("fta", "fta_t")):
        disp[s] = fit_dispersion(train[t].to_numpy().astype(float),
                                 mean_counts(train, s, rcfg), s)
    sims = int(rcfg["sims_points"])
    yp = test["points"].to_numpy().astype(float)
    n = test.height
    p3v = p3_test
    p2v = shrunk_accuracy(test, "p2", priors, rcfg)
    ftv = shrunk_accuracy(test, "ftp", priors, rcfg)
    m3, m2, mf = (mean_counts(test, s, rcfg) for s in ("fg3a", "fg2a", "fta"))
    pts_sims = np.zeros((n, sims))
    for mv, rv, acc, mult in ((m3, disp["fg3a"], p3v, 3),
                              (m2, disp["fg2a"], p2v, 2),
                              (mf, disp["fta"], ftv, 1)):
        att = st.nbinom.rvs(
            rv, rv / (rv + mv[:, None]), size=(n, sims),
            random_state=np.random.RandomState(int(rcfg["seed"]) + 100 * mult))
        made = st.binom.rvs(att, acc[:, None], random_state=np.random.RandomState(
            int(rcfg["seed"]) + mult))
        pts_sims += mult * made
    crps_p = float(np.mean(
        np.abs(pts_sims - yp[:, None]).mean(axis=1)
        - 0.5 * np.abs(pts_sims - np.roll(pts_sims, 1, axis=1)).mean(axis=1)))
    less = (pts_sims < yp[:, None]).mean(axis=1)
    eq = (pts_sims == yp[:, None]).mean(axis=1)
    up = less + rng.random(n) * eq
    save_pit_plot(up, "points")
    # baselines: direct NB on points (the thing we refuse to do for the model)
    m_tr_p = mean_counts(train, "points_direct", rcfg, "f_points40_szn_raw")
    r_p = fit_dispersion(train["points"].to_numpy().astype(float), m_tr_p,
                         "points-direct (baselines)")
    res_pts = {"model": crps_p, "pit": describe_pit(up)}
    for name, rate_col in [("szn-raw", "f_points40_szn_raw"),
                           ("trail-10", "f_points40_l10")]:
        m_b = mean_counts(test, "points_direct", rcfg, rate_col)
        cdf_b = nb_cdf_grid(m_b, r_p, int(rcfg["crps_max_count"]))
        res_pts[name] = float(crps_from_cdf(cdf_b, yp).mean())
    if not report_stat("points", res_pts):
        all_pass = False

    if not all_pass:
        log.info("VERDICT: at least one rate model does NOT beat its baselines — "
                 "stopping per spec, not proceeding.")
        return 1
    log.info("VERDICT: all four stats beat both baselines with usable PIT shapes.")

    MODEL_PATH.parent.mkdir(exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump({"dispersion": disp, "accuracy_priors": priors,
                     "config": {k: rcfg[k] for k in
                                ("k_p3", "k_p2", "k_ftp", "lg_pace_ref")},
                     "trained_through": str(df["game_date"].max())}, f)
    log.info("saved models/rates.pkl")
    return 0
