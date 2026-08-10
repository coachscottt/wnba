"""Feature layer: for every rostered player-game, predictors computed strictly
as-of (games 1..N-1 plus pregame roster/availability info for game N).

Played rows get shifted-window values; non-played rows (DNP/inactive) get the
same values via a backward as-of join on the player's played history — both are
"everything known before tip". The whole pipeline is a pure function of its
input frames: the leakage test reruns it on truncated history (future deleted,
same-date stats zeroed) and asserts identical feature rows.
"""

from __future__ import annotations

import sqlite3

import polars as pl

from src.config import load_config
from src.db import connect
from src.log import get_logger

log = get_logger("features")

KEYS = ["game_id", "player_id", "season", "game_date", "team_id", "opponent_id"]


# ---------------------------------------------------------------- load


def build_frames(conn: sqlite3.Connection) -> dict[str, pl.DataFrame]:
    def read(sql: str) -> pl.DataFrame:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description]
        return pl.DataFrame([dict(zip(cols, r)) for r in cur.fetchall()],
                            infer_schema_length=None)

    pg = read(
        "SELECT pg.game_id, pg.game_date, pg.player_id, pg.team_id, pg.opponent_id,"
        " pg.home_away, pg.minutes, pg.fga, pg.fgm, pg.fg3a, pg.fg3m, pg.fta,"
        " pg.ftm, pg.points, pg.oreb, pg.dreb, pg.reb, pg.ast, pg.stl, pg.blk,"
        " pg.tov, pg.pf, pg.started, a.status, g.season, g.overtime_periods AS ot "
        "FROM player_games pg "
        "JOIN availability a ON a.game_id = pg.game_id AND a.player_id = pg.player_id "
        "JOIN games g ON g.game_id = pg.game_id")
    games = read("SELECT * FROM games")
    players = read("SELECT player_id, player_name, position FROM players")
    teams = read("SELECT team_id, abbreviation FROM teams")
    return {"pg": pg, "games": games, "players": players, "teams": teams}


# ---------------------------------------------------------------- helpers


def shrunk(n: pl.Expr, obs: pl.Expr, k: float, prior: pl.Expr) -> pl.Expr:
    return (
        pl.when((n > 0) & obs.is_not_null() & obs.is_not_nan())
        .then((n * obs + k * prior) / (n + k))
        .otherwise(prior)
    )


def league_asof(frame: pl.DataFrame, by: list[str], vals: dict[str, str],
                defaults: dict[str, float] | None = None) -> pl.DataFrame:
    """Per (by..., date): cumulative sums of vals strictly before that date,
    with previous-season finals as early-season fallback."""
    daily = frame.group_by(by + ["season", "game_date"]).agg(
        [pl.col(c).sum().alias(c) for c in vals] + [pl.len().alias("n_rows")]
    ).sort(by + ["season", "game_date"])
    grp = by + ["season"]
    daily = daily.with_columns(
        [pl.col(c).cum_sum().shift(1).over(grp).alias(f"cum_{c}") for c in vals]
        + [pl.col("n_rows").cum_sum().shift(1).over(grp).alias("cum_n")])
    finals = daily.group_by(grp).agg(
        [(pl.col(c).sum()).alias(f"fin_{c}") for c in vals])
    finals = finals.sort(by + ["season"]).with_columns(
        [pl.col(f"fin_{c}").shift(1).over(by).alias(f"prev_{c}") for c in vals])
    return daily.join(finals, on=grp, how="left")


def _player_exprs(fcfg: dict, shifted: bool) -> list[pl.Expr]:
    """Player-level as-of expressions over the played frame, sorted
    (player_id, game_date). shifted=True excludes the current row (for the
    played row itself); shifted=False includes it (for backward-asof lookup
    by later non-played rows — their own game is never in the played frame)."""
    over_p = ["player_id", "season"]
    stats = fcfg["form_stats"]
    windows = fcfg["windows"]

    def base(col: str) -> pl.Expr:
        e = pl.col(col)
        return e.shift(1).over(over_p) if shifted else e

    def ratio40(num: pl.Expr, den: pl.Expr) -> pl.Expr:
        return pl.when(den > 0).then(40 * num / den).otherwise(None)

    exprs = []
    for s in stats:
        bs, bm = base(s), base("minutes")
        for w in windows:
            exprs.append(ratio40(
                bs.rolling_sum(w, min_samples=1).over(over_p),
                bm.rolling_sum(w, min_samples=1).over(over_p),
            ).alias(f"f_{s}40_l{w}"))
        rate = pl.when(pl.col("minutes") > 0).then(
            40 * pl.col(s) / pl.col("minutes")).otherwise(None)
        rate = rate.shift(1).over(over_p) if shifted else rate
        exprs.append(rate.ewm_mean(
            half_life=float(fcfg["ewm_half_life_games"]), ignore_nulls=True)
            .over(over_p).alias(f"f_{s}40_ewm"))
        exprs.append(ratio40(
            bs.cum_sum().over(over_p), bm.cum_sum().over(over_p),
        ).alias(f"f_{s}40_szn_raw"))
    exprs += [
        base("minutes").rolling_mean(w, min_samples=1).over(over_p)
        .alias(f"f_min_l{w}") for w in windows]
    exprs.append(base("minutes").rolling_mean(1, min_samples=1).over(over_p)
                 .alias("f_min_last"))
    share = pl.col("minutes") / pl.col("team_min")
    share = share.shift(1).over(over_p) if shifted else share
    exprs.append(share.rolling_mean(5, min_samples=1).over(over_p)
                 .alias("f_min_share_l5"))
    exprs.append(base("started").rolling_mean(10, min_samples=1).over(over_p)
                 .alias("f_start_rate_l10"))
    for name, num in (("f_ftr_l5", "fta"), ("f_3par_l5", "fg3a")):
        exprs.append(
            pl.when(base("fga").rolling_sum(5, min_samples=1).over(over_p) > 0)
            .then(base(num).rolling_sum(5, min_samples=1).over(over_p)
                  / base("fga").rolling_sum(5, min_samples=1).over(over_p))
            .alias(name))
    n = pl.col("minutes").cum_count().over(over_p)
    exprs.append(((n - 1) if shifted else n).cast(pl.Float64)
                 .alias("f_games_played"))
    # season as-of make/attempt counters for Beta accuracy shrinkage (phase 6)
    for c in ("fg3m", "fg3a", "fg2m", "fg2a", "ftm", "fta"):
        exprs.append(base(c).cum_sum().over(over_p).fill_null(0.0)
                     .alias(f"f_{c}_cum"))
    usage = pl.col("usage_g").shift(1).over(over_p) if shifted else pl.col("usage_g")
    exprs.append(usage.rolling_mean(5, min_samples=1).over(over_p)
                 .alias("f_usage_l5"))
    return exprs


PLAYER_COLS = None  # filled on first pipeline run


# ---------------------------------------------------------------- pipeline


def pipeline(frames: dict[str, pl.DataFrame], fcfg: dict) -> pl.DataFrame:
    stats = fcfg["form_stats"]
    kf, kt, ko, kop = (float(fcfg[k]) for k in ("k_form", "k_team", "k_opp", "k_opp_pos"))

    pg_all = frames["pg"].with_columns(
        pl.col("game_date").cast(pl.Utf8),
        pl.col("minutes").fill_null(0.0),
        (pl.col("fga") - pl.col("fg3a")).alias("fg2a"),
        (pl.col("fgm") - pl.col("fg3m")).alias("fg2m"),
    ).join(
        frames["players"].select("player_id", "position"), on="player_id", how="left"
    ).with_columns(
        pl.col("position").fill_null("F").str.slice(0, 1).alias("pos"),
        (200 + 25 * pl.col("ot").fill_null(0)).alias("team_min"),
    )
    played = pg_all.filter(pl.col("status") == "played").sort(
        ["player_id", "game_date"])

    # team totals per game (for usage), then per-game usage
    tm = played.group_by("game_id", "team_id").agg(
        pl.col("fga").sum().alias("tm_fga"), pl.col("fta").sum().alias("tm_fta"),
        pl.col("tov").sum().alias("tm_tov"))
    pw = played.join(tm, on=["game_id", "team_id"], how="left").with_columns(
        (100 * (pl.col("fga") + 0.44 * pl.col("fta") + pl.col("tov"))
         * (pl.col("team_min") / 5)
         / (pl.when(pl.col("minutes") > 0).then(pl.col("minutes")).otherwise(None)
            * (pl.col("tm_fga") + 0.44 * pl.col("tm_fta") + pl.col("tm_tov"))))
        .alias("usage_g")
    ).sort(["player_id", "game_date"])

    meta_cols = KEYS + ["pos", "home_away", "status", "started"]
    shifted = pw.with_columns(_player_exprs(fcfg, shifted=True))
    pcols = [c for c in shifted.columns if c.startswith(("f_", "raw_"))]
    base_played = shifted.select(meta_cols + pcols)

    # non-played roster rows: same values via backward as-of on played history
    lookup = pw.with_columns(_player_exprs(fcfg, shifted=False)).select(
        "player_id", "season",
        pl.col("game_date").str.to_date().alias("d"), *pcols
    ).sort("d")
    nonplayed = pg_all.filter(pl.col("status") != "played").select(
        meta_cols).with_columns(
        pl.col("game_date").str.to_date().alias("d")).sort("d")
    base_np = nonplayed.join_asof(
        lookup, on="d", by=["player_id", "season"], strategy="backward",
    ).drop("d").select(meta_cols + pcols)

    feat = pl.concat([base_played, base_np])

    # ---- league as-of priors by position; shrink season-to-date form
    lp = league_asof(
        played.select("season", "game_date", "pos", *stats, "minutes"),
        ["pos"], {s: s for s in stats} | {"minutes": "minutes"})
    min_n = int(fcfg["min_league_games_for_prior"])
    for s in stats:
        default = float(fcfg["league_prior_defaults"][s])
        lp = lp.with_columns(
            pl.when(pl.col("cum_n") >= min_n)
            .then(40 * pl.col(f"cum_{s}") / pl.col("cum_minutes"))
            .otherwise(40 * pl.col(f"prev_{s}") / pl.col("prev_minutes"))
            .fill_null(default).fill_nan(default)
            .alias(f"lg_{s}40"))
    feat = feat.join(
        lp.select("pos", "season", "game_date", *[f"lg_{s}40" for s in stats]),
        on=["pos", "season", "game_date"], how="left")
    for s in stats:
        default = float(fcfg["league_prior_defaults"][s])
        feat = feat.with_columns(pl.col(f"lg_{s}40").fill_null(default))
        feat = feat.with_columns(
            shrunk(pl.col("f_games_played"), pl.col(f"f_{s}40_szn_raw"), kf,
                   pl.col(f"lg_{s}40")).alias(f"f_{s}40_szn"))
    feat = feat.with_columns(
        (pl.col("f_games_played") / (pl.col("f_games_played") + kf))
        .alias("f_form_weight"),
        pl.col("started").cast(pl.Float64).alias("f_started"))
    # expose the as-of league priors so downstream models can re-shrink with
    # their own validated k (phase 6 tunes per-stat k on validation folds)
    feat = feat.rename({f"lg_{s}40": f"f_lg_{s}40" for s in stats})

    # ---- team context + opponent, as-of from the games table
    g = frames["games"].with_columns(pl.col("game_date").cast(pl.Utf8))
    sides = pl.concat([
        g.select(pl.col("game_id"), pl.col("season"), pl.col("game_date"),
                 pl.col("home_team_id").alias("team_id"), pl.col("pace"),
                 pl.col("home_off_rating").alias("ortg"),
                 pl.col("away_off_rating").alias("drtg")),
        g.select(pl.col("game_id"), pl.col("season"), pl.col("game_date"),
                 pl.col("away_team_id").alias("team_id"), pl.col("pace"),
                 pl.col("away_off_rating").alias("ortg"),
                 pl.col("home_off_rating").alias("drtg")),
    ]).sort(["team_id", "season", "game_date"])
    over_t = ["team_id", "season"]
    for c in ("pace", "ortg", "drtg"):
        sides = sides.with_columns(
            pl.col(c).fill_null(0.0).cum_sum().shift(1).over(over_t)
            .alias(f"cum_{c}"),
            pl.col(c).is_not_null().cast(pl.Int64).cum_sum().shift(1).over(over_t)
            .alias(f"n_{c}"))
    lgd = sides.group_by("season", "game_date").agg(
        pl.col("pace").fill_null(0.0).sum().alias("s_pace"),
        pl.col("ortg").fill_null(0.0).sum().alias("s_ortg"),
        pl.col("pace").is_not_null().sum().alias("n_p"),
        pl.col("ortg").is_not_null().sum().alias("n_o"),
    ).sort(["season", "game_date"]).with_columns(
        (pl.col("s_pace").cum_sum().shift(1).over("season")
         / pl.col("n_p").cum_sum().shift(1).over("season")).alias("lg_pace_asof"),
        (pl.col("s_ortg").cum_sum().shift(1).over("season")
         / pl.col("n_o").cum_sum().shift(1).over("season")).alias("lg_ortg_asof"))
    prev = lgd.group_by("season").agg(
        (pl.col("s_pace").sum() / pl.col("n_p").sum()).alias("fin_pace"),
        (pl.col("s_ortg").sum() / pl.col("n_o").sum()).alias("fin_ortg"),
    ).sort("season").with_columns(
        pl.col("fin_pace").shift(1).alias("prev_pace"),
        pl.col("fin_ortg").shift(1).alias("prev_ortg"))
    sides = sides.join(
        lgd.select("season", "game_date", "lg_pace_asof", "lg_ortg_asof"),
        on=["season", "game_date"], how="left")
    sides = sides.join(prev.select("season", "prev_pace", "prev_ortg"),
                       on="season", how="left").with_columns(
        pl.coalesce("lg_pace_asof", "prev_pace", pl.lit(78.0)).alias("lg_pace"),
        pl.coalesce("lg_ortg_asof", "prev_ortg", pl.lit(100.0)).alias("lg_ortg"))
    team_ctx = sides.with_columns(
        shrunk(pl.col("n_pace"), pl.col("cum_pace") / pl.col("n_pace"), kt,
               pl.col("lg_pace")).alias("t_pace"),
        shrunk(pl.col("n_ortg"), pl.col("cum_ortg") / pl.col("n_ortg"), kt,
               pl.col("lg_ortg")).alias("t_ortg"),
        shrunk(pl.col("n_drtg"), pl.col("cum_drtg") / pl.col("n_drtg"), kt,
               pl.col("lg_ortg")).alias("t_drtg"),
        shrunk(pl.col("n_pace"), pl.col("cum_pace") / pl.col("n_pace"), ko,
               pl.col("lg_pace")).alias("o_pace"),
        shrunk(pl.col("n_ortg"), pl.col("cum_ortg") / pl.col("n_ortg"), ko,
               pl.col("lg_ortg")).alias("o_ortg"),
        shrunk(pl.col("n_drtg"), pl.col("cum_drtg") / pl.col("n_drtg"), ko,
               pl.col("lg_ortg")).alias("o_drtg"),
    ).select("game_id", "team_id", "t_pace", "t_ortg", "t_drtg",
             "o_pace", "o_ortg", "o_drtg")
    feat = feat.join(
        team_ctx.select("game_id", "team_id", "t_pace", "t_ortg", "t_drtg"),
        on=["game_id", "team_id"], how="left").rename(
        {"t_pace": "f_team_pace", "t_ortg": "f_team_ortg", "t_drtg": "f_team_drtg"})
    feat = feat.join(
        team_ctx.select("game_id", pl.col("team_id").alias("opponent_id"),
                        "o_pace", "o_ortg", "o_drtg"),
        on=["game_id", "opponent_id"], how="left").rename(
        {"o_pace": "f_opp_pace", "o_ortg": "f_opp_ortg", "o_drtg": "f_opp_drtg"})

    # ---- opponent positional defense (as-of, shrunk hard, ratio to league)
    allowed = played.group_by("opponent_id", "season", "game_date", "pos").agg(
        pl.col("points").sum().alias("pts"), pl.col("minutes").sum().alias("mins")
    ).sort(["opponent_id", "pos", "season", "game_date"]).with_columns(
        pl.col("pts").cum_sum().shift(1).over(["opponent_id", "pos", "season"])
        .alias("cum_pts"),
        pl.col("mins").cum_sum().shift(1).over(["opponent_id", "pos", "season"])
        .alias("cum_mins"),
        (pl.col("pts").cum_count().over(["opponent_id", "pos", "season"]) - 1)
        .alias("n_g"))
    allowed = allowed.join(
        lp.select("pos", "season", "game_date", "lg_points40"),
        on=["pos", "season", "game_date"], how="left").with_columns(
        pl.col("lg_points40").fill_null(float(fcfg["league_prior_defaults"]["points"])))
    allowed = allowed.with_columns(
        (shrunk(pl.col("n_g"), 40 * pl.col("cum_pts") / pl.col("cum_mins"), kop,
                pl.col("lg_points40")) / pl.col("lg_points40"))
        .alias("f_opp_pos_def"))
    feat = feat.join(
        allowed.select("opponent_id", "season", "game_date", "pos", "f_opp_pos_def"),
        on=["opponent_id", "season", "game_date", "pos"], how="left")
    feat = feat.with_columns(pl.col("f_opp_pos_def").fill_null(1.0))

    # ---- opponent team-level allow-factors per stat (as-of, shrunk hard):
    # "this team allows more threes" is estimable; player-vs-team is not.
    for s in fcfg.get("opp_allow_stats", []):
        al = played.group_by("opponent_id", "season", "game_date").agg(
            pl.col(s).sum().alias("v"), pl.col("minutes").sum().alias("mins")
        ).sort(["opponent_id", "season", "game_date"]).with_columns(
            pl.col("v").cum_sum().shift(1).over(["opponent_id", "season"])
            .alias("cum_v"),
            pl.col("mins").cum_sum().shift(1).over(["opponent_id", "season"])
            .alias("cum_m"),
            (pl.col("v").cum_count().over(["opponent_id", "season"]) - 1)
            .alias("n_g"))
        lg_s = al.group_by("season", "game_date").agg(
            pl.col("v").sum().alias("sv"), pl.col("mins").sum().alias("sm")
        ).sort(["season", "game_date"]).with_columns(
            (40 * pl.col("sv").cum_sum().shift(1).over("season")
             / pl.col("sm").cum_sum().shift(1).over("season")).alias("lg40"))
        default = float(fcfg["league_prior_defaults"].get(s, 1.0))
        al = al.join(lg_s.select("season", "game_date", "lg40"),
                     on=["season", "game_date"], how="left").with_columns(
            pl.col("lg40").fill_null(default))
        al = al.with_columns(
            (shrunk(pl.col("n_g"), 40 * pl.col("cum_v") / pl.col("cum_m"), kop,
                    pl.col("lg40")) / pl.col("lg40")).alias(f"f_opp_allow_{s}"))
        feat = feat.join(
            al.select("opponent_id", "season", "game_date", f"f_opp_allow_{s}"),
            on=["opponent_id", "season", "game_date"], how="left").with_columns(
            pl.col(f"f_opp_allow_{s}").fill_null(1.0))

    # ---- situational, from the team schedule
    tz = fcfg["team_tz_offsets"]
    abbr = {str(r["team_id"]): r["abbreviation"] for r in frames["teams"].to_dicts()}
    g2 = g.with_columns(
        pl.col("home_team_id").cast(pl.Utf8)
        .replace_strict({k: float(tz.get(v, 0)) for k, v in abbr.items()},
                        default=0.0).alias("game_tz"))
    sched = pl.concat([
        g2.select("game_id", "season", "game_date", "game_tz",
                  pl.col("home_team_id").alias("team_id")),
        g2.select("game_id", "season", "game_date", "game_tz",
                  pl.col("away_team_id").alias("team_id")),
    ]).sort(["team_id", "season", "game_date"]).with_columns(
        pl.col("game_date").str.to_date())
    cap = int(fcfg["rest_cap_days"])
    sched = sched.with_columns(
        (pl.col("game_date") - pl.col("game_date").shift(1).over(over_t))
        .dt.total_days().alias("rest_raw"),
        (pl.col("game_tz") - pl.col("game_tz").shift(1).over(over_t)).abs()
        .alias("f_tz_crossed"),
        pl.col("game_id").cum_count().over(over_t).cast(pl.Float64)
        .alias("f_game_number"),
    ).with_columns(
        pl.min_horizontal(pl.col("rest_raw"), cap).cast(pl.Float64)
        .fill_null(float(cap)).alias("f_days_rest"),
        (pl.col("rest_raw") > 7).cast(pl.Float64).fill_null(0.0)
        .alias("f_long_break"),
        (pl.col("rest_raw") == 1).cast(pl.Float64).fill_null(0.0).alias("f_b2b"),
        pl.col("f_tz_crossed").fill_null(0.0),
        pl.col("game_date").cast(pl.Utf8),
    )
    feat = feat.join(
        sched.select("game_id", "team_id", "f_days_rest", "f_long_break", "f_b2b",
                     "f_tz_crossed", "f_game_number"),
        on=["game_id", "team_id"], how="left").with_columns(
        (pl.col("home_away") == "home").cast(pl.Float64).alias("f_home"))

    # ---- teammate availability (pregame info for game N)
    sh_min = pl.col("minutes").shift(1).over("player_id")
    sh_ast = pl.col("ast").shift(1).over("player_id")
    trail = played.sort(["player_id", "game_date"]).select(
        "player_id",
        pl.col("game_date").str.to_date().alias("d"),
        pl.col("minutes").rolling_mean(10, min_samples=1).over("player_id")
        .alias("v_min"),
        pl.when(pl.col("minutes").rolling_sum(15, min_samples=1)
                .over("player_id") > 0)
        .then(40 * pl.col("ast").rolling_sum(15, min_samples=1).over("player_id")
              / pl.col("minutes").rolling_sum(15, min_samples=1).over("player_id"))
        .alias("v_ast40"),
        pl.col("minutes").cum_count().over("player_id").alias("v_n"),
        sh_min.rolling_mean(10, min_samples=1).over("player_id").alias("v_min_sh"),
        pl.when(sh_min.rolling_sum(15, min_samples=1).over("player_id") > 0)
        .then(40 * sh_ast.rolling_sum(15, min_samples=1).over("player_id")
              / sh_min.rolling_sum(15, min_samples=1).over("player_id"))
        .alias("v_ast40_sh"),
        (pl.col("minutes").cum_count().over("player_id") - 1).alias("v_n_sh"),
    )
    roster = pg_all.select(
        "game_id", "team_id", "player_id", "pos", "status",
        pl.col("game_date").str.to_date().alias("d"))
    r_played = roster.filter(pl.col("status") == "played").join(
        trail.select("player_id", "d", pl.col("v_min_sh").alias("v_min"),
                     pl.col("v_ast40_sh").alias("v_ast40"),
                     pl.col("v_n_sh").alias("v_n")),
        on=["player_id", "d"], how="left")
    r_out = roster.filter(pl.col("status") != "played").sort("d").join_asof(
        trail.select("player_id", "d", "v_min", "v_ast40", "v_n").sort("d"),
        on="d", by="player_id", strategy="backward")
    roster2 = pl.concat([r_played, r_out.select(r_played.columns)])

    is_out = pl.col("status").is_in(list(fcfg["out_statuses"]))
    vac_team = roster2.group_by("game_id", "team_id").agg(
        (pl.col("v_min").fill_null(0) * is_out).sum().alias("f_min_vacated"))
    vac_pos = roster2.group_by("game_id", "team_id", "pos").agg(
        (pl.col("v_min").fill_null(0) * is_out).sum().alias("f_min_vacated_samepos"))
    feat = feat.join(vac_team, on=["game_id", "team_id"], how="left")
    feat = feat.join(vac_pos, on=["game_id", "team_id", "pos"], how="left")

    bh_flag = roster2.filter(
        pl.col("v_n") >= int(fcfg["ballhandler_min_games"])
    ).sort("v_ast40", descending=True, nulls_last=True).group_by(
        "game_id", "team_id", maintain_order=True).agg(
        pl.col("status").first().alias("bh_status"))
    feat = feat.join(bh_flag, on=["game_id", "team_id"], how="left").with_columns(
        pl.col("bh_status").is_in(list(fcfg["out_statuses"]))
        .cast(pl.Float64).fill_null(0.0).alias("f_bh_out"))

    fcols = [c for c in feat.columns if c.startswith("f_")]
    return feat.select(KEYS + ["status"] + fcols).sort(
        ["game_date", "game_id", "player_id"])


# ---------------------------------------------------------------- entry point


def run_features(conn: sqlite3.Connection | None = None, quiet: bool = False) -> pl.DataFrame:
    cfg = load_config()["features"]
    conn = conn or connect()
    frames = build_frames(conn)
    feat = pipeline(frames, cfg)

    fcols = [c for c in feat.columns if c.startswith("f_")]
    ddl = ", ".join(f'"{c}" REAL' for c in fcols)
    with conn:
        conn.execute("DROP TABLE IF EXISTS features")
        conn.execute(
            "CREATE TABLE features (game_id TEXT, player_id TEXT, season INTEGER,"
            f" game_date TEXT, team_id TEXT, opponent_id TEXT, status TEXT, {ddl},"
            " PRIMARY KEY (game_id, player_id))")
        conn.executemany(
            f"INSERT INTO features VALUES ({','.join('?' * (len(KEYS) + 1 + len(fcols)))})",
            feat.rows())
    if quiet:
        return feat

    n_played = feat.filter(pl.col("status") == "played").height
    log.info(f"features: {feat.height} rows ({n_played} played, "
             f"{feat.height - n_played} non-played rostered), {len(fcols)} features")
    log.info("feature summary (flag: >20% nulls or zero variance):")
    log.info(f"  {'feature':26} {'cover%':>7} {'mean':>8} {'sd':>8} "
             f"{'min':>8} {'max':>8} {'nulls':>6}")
    for c in fcols:
        s = feat[c]
        nulls = s.null_count() + (s.is_nan().sum() if s.dtype == pl.Float64 else 0)
        cover = 100 * (1 - nulls / feat.height)
        sd = s.std() or 0
        flag = " ⚠" if (nulls / feat.height > 0.20 or sd == 0) else ""
        log.info(f"  {c:26} {cover:7.1f} {s.mean() or 0:8.2f} {sd:8.2f} "
                 f"{s.min() or 0:8.2f} {s.max() or 0:8.2f} {nulls:6d}{flag}")

    w = feat.group_by("player_id").agg(pl.col("f_form_weight").last().alias("w"))
    qs = [round(q, 3) for q in (
        w["w"].quantile(0.1), w["w"].median(), w["w"].quantile(0.9))]
    log.info(f"effective form shrinkage weight n/(n+k), latest game per player: "
             f"p10 {qs[0]}, median {qs[1]}, p90 {qs[2]} "
             f"(k_form={cfg['k_form']}; weight 0 = all prior, 1 = all observed)")
    return feat
