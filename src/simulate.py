"""Monte Carlo simulation: the joint distribution over every stat for every
player in a game.

Per game, N sims share a pace draw and an overtime draw (both teams move
together); each team draws who plays (DNP coin flips) and a Dirichlet
minutes-share vector (sum = 200 + 25 x OT, exactly); attempts are Negative
Binomial scaled by that simulation's minutes and pace; makes are Binomial;
points and combos are derived per simulation. Correlations come free:
minutes are zero-sum, pace lifts everyone, a DNP's minutes flow to teammates
(and their attempts follow their minutes — a separate usage-share layer would
double-count that redistribution; see DECISIONS.md).

Stored per player-stat: mean, sd, P(stat >= k) at 1-unit intervals — never
the raw draws.

Run the phase 7 checks:  python -m src.simulate
"""

from __future__ import annotations

import pickle
import sqlite3

import numpy as np
import polars as pl

from src.config import ROOT, load_config
from src.db import connect
from src.log import get_logger
from src.model_minutes import CLF_FEATURES, REG_FEATURES  # noqa: F401 (unpickling)
from src.model_rates import ACCURACIES, COUNT_STATS, shrunk_rate40

log = get_logger("simulate")

SIM_STATS = ["points", "reb", "ast", "fg3m", "pra", "pr", "pa", "ra"]


# ---------------------------------------------------------------- loading


def load_models() -> dict:
    with open(ROOT / "models" / "minutes.pkl", "rb") as f:
        minutes = pickle.load(f)
    with open(ROOT / "models" / "rates.pkl", "rb") as f:
        rates = pickle.load(f)
    return {"minutes": minutes, "rates": rates}


def load_slate(conn: sqlite3.Connection, game_ids: list[str]) -> pl.DataFrame:
    """Pregame-available roster rows (played + dnp_coach) with features."""
    ph = ",".join("?" * len(game_ids))
    cur = conn.execute(
        f"SELECT f.*, p.player_name, p.position, g.overtime_periods AS ot_actual,"
        f" pg.minutes AS actual_minutes, pg.points AS actual_points,"
        f" pg.reb AS actual_reb "
        f"FROM features f "
        f"JOIN players p ON p.player_id = f.player_id "
        f"JOIN games g ON g.game_id = f.game_id "
        f"LEFT JOIN player_games pg ON pg.game_id = f.game_id"
        f" AND pg.player_id = f.player_id "
        f"WHERE f.game_id IN ({ph}) AND f.status IN ('played', 'dnp_coach')",
        game_ids)
    cols = [d[0] for d in cur.description]
    df = pl.DataFrame([dict(zip(cols, r)) for r in cur.fetchall()],
                      infer_schema_length=None)
    return df.with_columns(
        ((pl.col("f_team_ortg") - pl.col("f_team_drtg"))
         - (pl.col("f_opp_ortg") - pl.col("f_opp_drtg"))).alias("blowout_proxy"))


def fit_game_params(conn: sqlite3.Connection, holdout_start: str) -> tuple[float, float]:
    """OT probability and pace residual sd from pre-holdout games only."""
    cur = conn.execute(
        "SELECT g.overtime_periods AS ot, g.pace,"
        " (f.f_team_pace + f.f_opp_pace) / 2 AS pace_pred "
        "FROM games g JOIN features f ON f.game_id = g.game_id "
        "WHERE g.game_date < ? AND g.pace IS NOT NULL "
        "GROUP BY g.game_id", (holdout_start,))
    rows = cur.fetchall()
    ot_prob = float(np.mean([r["ot"] > 0 for r in rows]))
    resid = np.array([r["pace"] - r["pace_pred"] for r in rows], dtype=float)
    return ot_prob, float(resid.std())


# ---------------------------------------------------------------- engine


def to_np(df: pl.DataFrame, cols: list[str]) -> np.ndarray:
    return df.select([pl.col(c).cast(pl.Float64) for c in cols]).to_numpy()


def simulate_game(game_df: pl.DataFrame, models: dict, scfg: dict, rcfg: dict,
                  ot_prob: float, pace_sd: float,
                  rng: np.random.Generator) -> dict[str, np.ndarray]:
    """Returns {stat: (n_players, sims)} plus 'minutes'; row order = game_df."""
    S = int(scfg["n_sims"])
    n = game_df.height
    mm, rm = models["minutes"], models["rates"]

    # game-level draws, shared by both teams
    ot = (rng.random(S) < ot_prob).astype(int)  # 1 OT period when it happens
    totals = 200.0 + 25.0 * ot
    pace_mean = float((game_df["f_team_pace"] + game_df["f_opp_pace"]).mean()) / 2
    pace = rng.normal(pace_mean, pace_sd, S)
    pace_f = np.clip(pace / float(rcfg["lg_pace_ref"]), 0.7, 1.3)

    # stage 1+2: who plays, and minutes shares per team
    p_dnp = mm["clf"].predict_proba(to_np(game_df, mm["clf_features"]))[:, 1]
    mu_raw = np.clip(mm["reg"].predict(to_np(game_df, mm["reg_features"])),
                     1e-4, None)
    minutes = np.zeros((n, S))
    team_ids = game_df["team_id"].to_list()
    c = float(mm["concentration"])
    for tid in set(team_ids):
        idx = np.array([i for i, t in enumerate(team_ids) if t == tid])
        plays = rng.random((S, len(idx))) >= p_dnp[idx][None, :]
        order = np.argsort(p_dnp[idx])
        few = plays.sum(axis=1) < 5
        if few.any():
            plays[np.ix_(few, order[:5])] = True
        # Dirichlet via per-element gamma draws (vectorized over sims);
        # alpha = 0 for non-playing rows -> gamma = 0 -> share = 0
        mp = mu_raw[idx][None, :] * plays
        alpha = c * mp / mp.sum(axis=1, keepdims=True)
        g = rng.gamma(np.clip(alpha, 0, None))
        shares = g / g.sum(axis=1, keepdims=True)
        minutes[idx, :] = (shares * totals[:, None]).T

    # stage 3: attempts (NB, minutes- and pace-scaled) and makes (Binomial)
    disp = rm["dispersion"]
    kcfg = rm["config"]

    def rate40(stat: str) -> np.ndarray:
        opp = COUNT_STATS[stat][1]
        kw = rm["k_rate"][stat]
        return (shrunk_rate40(game_df, stat, float(kw["k"]), float(kw["w"]))
                * game_df[opp].fill_null(1.0).to_numpy())

    def acc(name: str) -> np.ndarray:
        makes_c, att_c, k_key = ACCURACIES[name]
        k = float(kcfg[k_key])
        prior = np.array([
            rm["accuracy_priors"][name].get((p or "F")[0],
                                            rm["accuracy_priors"][name]["ALL"])
            for p in game_df["position"].to_list()])
        makes = game_df[makes_c].fill_null(0.0).to_numpy()
        atts = game_df[att_c].fill_null(0.0).to_numpy()
        return (makes + k * prior) / (atts + k)

    def nb_counts(stat: str) -> np.ndarray:
        m = np.clip(rate40(stat)[:, None] * pace_f[None, :] * minutes / 40.0,
                    1e-9, None)
        r = float(disp[stat])
        return rng.negative_binomial(r, r / (r + m))

    a3, a2, af = nb_counts("fg3a"), nb_counts("fg2a"), nb_counts("fta")
    fg3m = rng.binomial(a3, acc("p3")[:, None])
    fg2m = rng.binomial(a2, acc("p2")[:, None])
    ftm = rng.binomial(af, acc("ftp")[:, None])
    reb = nb_counts("reb")
    ast = nb_counts("ast")
    points = 3 * fg3m + 2 * fg2m + ftm

    # sum-constraint sanity, every game, every sim
    for tid in set(team_ids):
        idx = [i for i, t in enumerate(team_ids) if t == tid]
        sums = minutes[idx, :].sum(axis=0)
        assert np.abs(sums - totals).max() < 1e-6, "minutes sum violated"

    return {"minutes": minutes, "points": points, "reb": reb, "ast": ast,
            "fg3m": fg3m, "pra": points + reb + ast, "pr": points + reb,
            "pa": points + ast, "ra": reb + ast}


def summarize(game_df: pl.DataFrame, sims: dict, p_ge_max: int) -> list[tuple]:
    rows = []
    for stat in SIM_STATS:
        arr = sims[stat]
        S = arr.shape[1]
        for i in range(game_df.height):
            x = arr[i].astype(np.int64)
            counts = np.bincount(np.clip(x, 0, p_ge_max + 1),
                                 minlength=p_ge_max + 2)
            # P(X >= k) = 1 - F(k-1)
            below = np.concatenate([[0], np.cumsum(counts)[:-1]])[:p_ge_max + 1]
            pge = 1.0 - below / S
            rows.append((game_df["game_id"][i], game_df["player_id"][i], stat,
                         float(x.mean()), float(x.std()),
                         ",".join(f"{p:.4f}" for p in pge)))
    return rows


def store_summaries(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    with conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sim_summary ("
            " game_id TEXT, player_id TEXT, stat TEXT, mean REAL, sd REAL,"
            " p_ge TEXT,"  # comma-joined P(stat>=k) for k = 0..p_ge_max
            " PRIMARY KEY (game_id, player_id, stat))")
        conn.executemany("INSERT OR REPLACE INTO sim_summary VALUES (?,?,?,?,?,?)",
                         rows)


# ---------------------------------------------------------------- phase 7 checks


def run_checks() -> int:
    cfg = load_config()
    scfg, rcfg = cfg["simulation"], cfg["rates"]
    conn = connect()
    models = load_models()
    rng = np.random.default_rng(int(scfg["seed"]))

    holdout = cfg["minutes"]["holdout_start"]
    ot_p, pace_sd = fit_game_params(conn, holdout)
    ot_p = float(scfg["ot_prob"] or ot_p)
    pace_sd = float(scfg["pace_sd"] or pace_sd)
    log.info(f"game params (pre-{holdout} fit): P(OT) = {ot_p:.3f}, "
             f"pace residual sd = {pace_sd:.2f}")

    # simulate every regulation holdout game that has results
    game_rows = conn.execute(
        "SELECT game_id, home_score, away_score FROM games "
        "WHERE game_date >= ? AND home_score IS NOT NULL", (holdout,)).fetchall()
    game_ids = [r["game_id"] for r in game_rows]
    log.info(f"simulating {len(game_ids)} holdout games x "
             f"{scfg['n_sims']} sims (seed {scfg['seed']})")

    # overlay targets resolved up front so draws can be pooled during the loop
    names = scfg["overlay_players"] or [r["player_name"] for r in conn.execute(
        "SELECT p.player_name, COUNT(*) n FROM prop_lines pl "
        "JOIN players p ON p.player_id = pl.player_id "
        "WHERE pl.player_id IS NOT NULL GROUP BY pl.player_id "
        "ORDER BY n DESC LIMIT 5")]
    pid_of = {}
    for nm in names:
        r = conn.execute("SELECT player_id FROM players WHERE player_name = ?",
                         (nm,)).fetchone()
        if r:
            pid_of[nm] = r["player_id"]
    pools: dict[str, list] = {pid: [] for pid in pid_of.values()}

    slate = load_slate(conn, game_ids)
    all_rows, team_pts_err, team_reb_means, actual_team_pts = [], [], [], []
    for gid in game_ids:
        gdf = slate.filter(pl.col("game_id") == gid)
        if gdf.height < 10:
            log.info(f"  skip {gid}: only {gdf.height} roster rows")
            continue
        sims = simulate_game(gdf, models, scfg, rcfg, ot_p, pace_sd, rng)
        all_rows += summarize(gdf, sims, int(scfg["p_ge_max"]))
        pids = gdf["player_id"].to_list()
        for i, pid in enumerate(pids):
            if pid in pools:
                pools[pid].append(sims["points"][i, :])
        for tid in set(gdf["team_id"].to_list()):
            idx = [i for i, t in enumerate(gdf["team_id"].to_list()) if t == tid]
            team_pts = sims["points"][idx, :].sum(axis=0)
            actual = gdf.filter(pl.col("team_id") == tid)["actual_points"].sum()
            team_pts_err.append(float(team_pts.mean()) - float(actual or 0))
            actual_team_pts.append(float(actual or 0))
            team_reb_means.append(float(sims["reb"][idx, :].sum(axis=0).mean()))
    store_summaries(conn, all_rows)

    log.info("sanity checks:")
    log.info("  pass  team minutes sum to 200 (+25 only on simulated OT) — "
             "asserted in every simulation of every game")
    err = np.array(team_pts_err)
    log.info(f"  team points: sim mean vs actual, mean error {err.mean():+.2f}, "
             f"MAE {np.abs(err).mean():.2f} (n={len(err)} team-games; "
             f"actual avg {np.mean(actual_team_pts):.1f}) — no game total "
             "lines captured, compared to realized scores instead")
    reb_avg = float(np.mean(team_reb_means))
    log.info(f"  team rebounds: sim mean {reb_avg:.1f} per team "
             f"({'realistic' if 28 <= reb_avg <= 42 else 'OUT OF RANGE'})")

    # P(stat >= k) around posted lines
    lines = conn.execute(
        "SELECT pl.game_id, pl.player_id, MAX(pl.line) AS line, p.player_name "
        "FROM prop_lines pl JOIN players p ON p.player_id = pl.player_id "
        "WHERE pl.match_status = 'ok' AND pl.is_alternate = 0 "
        "AND pl.game_id IN (SELECT game_id FROM sim_summary) "
        "AND pl.market = 'player_points' "
        "GROUP BY pl.game_id, pl.player_id "
        "ORDER BY line DESC LIMIT 8").fetchall()
    if lines:
        log.info("P(points >= k) around posted lines (sample):")
        for r in lines:
            s = conn.execute(
                "SELECT p_ge FROM sim_summary WHERE game_id=? AND player_id=? "
                "AND stat='points'", (r["game_id"], r["player_id"])).fetchone()
            if not s:
                continue
            pge = [float(x) for x in s["p_ge"].split(",")]
            lo, hi = max(0, int(r["line"] - 2)), min(len(pge) - 1, int(r["line"] + 3))
            ks = "  ".join(f"P≥{k}:{pge[k]:.2f}" for k in range(lo, hi + 1))
            log.info(f"  {r['player_name']:22} line {r['line']:5}: {ks}")

    overlay(conn, pools, pid_of)
    return 0


def overlay(conn: sqlite3.Connection, pools: dict, pid_of: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    (ROOT / "reports").mkdir(exist_ok=True)
    log.info("overlay check — simulated points distribution vs actual 2026 game log:")
    for name, pid in pid_of.items():
        actual = [r["points"] for r in conn.execute(
            "SELECT pg.points FROM player_games pg JOIN games g "
            "ON g.game_id = pg.game_id WHERE pg.player_id = ? AND g.season = 2026 "
            "AND pg.dnp_reason IS NULL AND pg.minutes > 0", (pid,))]
        holdout = load_config()["minutes"]["holdout_start"]
        act_hold = [r["points"] for r in conn.execute(
            "SELECT pg.points FROM player_games pg JOIN games g "
            "ON g.game_id = pg.game_id WHERE pg.player_id = ? AND g.season = 2026 "
            "AND g.game_date >= ? AND pg.dnp_reason IS NULL AND pg.minutes > 0",
            (pid, holdout))]
        sim_draws = pools.get(pid, [])
        if not sim_draws or not actual:
            log.info(f"  {name}: no sims or no game log, skipped")
            continue
        pool = np.concatenate(sim_draws)
        cond = pool[pool > 0]  # ~conditional on playing (DNP sims land at 0)
        hi25 = float((pool >= 25).mean())
        act25 = float(np.mean([a >= 25 for a in actual]))
        log.info(f"  {name}: sim mean {pool.mean():.1f} "
                 f"(cond. on playing {cond.mean():.1f}) vs actual: season "
                 f"{np.mean(actual):.1f} ({len(actual)} gm), holdout-period "
                 f"{np.mean(act_hold) if act_hold else float('nan'):.1f} "
                 f"({len(act_hold)} gm); P(25+) sim {hi25:.2f} vs actual {act25:.2f}")
        fig, ax = plt.subplots(figsize=(6, 3.5))
        bins = np.arange(0, max(pool.max(), max(actual)) + 2) - 0.5
        ax.hist(pool, bins=bins, density=True, alpha=0.55,
                label=f"simulated ({len(sim_draws)} holdout games)")
        ax.hist(actual, bins=bins, density=True, histtype="step", lw=2,
                label=f"actual 2026 log ({len(actual)} games)")
        ax.set_title(f"{name} — points: simulated vs actual")
        ax.set_xlabel("points")
        ax.legend()
        fig.tight_layout()
        safe = name.replace(" ", "_").replace("'", "")
        fig.savefig(ROOT / "reports" / f"overlay_{safe}.png", dpi=120)
        plt.close(fig)
        log.info(f"    saved reports/overlay_{safe}.png")


if __name__ == "__main__":
    import sys

    sys.exit(run_checks())
