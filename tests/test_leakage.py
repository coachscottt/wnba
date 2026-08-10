"""As-of leakage test (build/04-features.md) + expansion-team fallback test.

Samples player-games, then reruns the ENTIRE feature pipeline on a snapshot of
history as it looked at tip-off: every later game deleted, and the sampled
game's own box-score stats zeroed out (its roster, starters, and availability
kept — those are pregame information). If any feature differs from the
full-history run, something leaked.

Run directly (no test framework needed):  python -m tests.test_leakage
"""

from __future__ import annotations

import random
import sys

import polars as pl

from src.config import load_config
from src.db import connect
from src.features import build_frames, pipeline
from src.log import get_logger

log = get_logger("test_leakage")

# postgame columns that must not influence same-date features
PG_STATS = ["minutes", "fga", "fgm", "fg3a", "fg3m", "fta", "ftm", "points",
            "oreb", "dreb", "reb", "ast", "stl", "blk", "tov", "pf"]
GAME_STATS = ["home_score", "away_score", "possessions_est", "pace",
              "home_off_rating", "away_off_rating", "attendance"]


def truncate(frames: dict, cutoff: str) -> dict:
    """History exactly as known at tip-off on `cutoff` date."""
    out = dict(frames)
    out["pg"] = frames["pg"].filter(pl.col("game_date") <= cutoff).with_columns(
        [pl.when(pl.col("game_date") == cutoff).then(None).otherwise(pl.col(c))
         .alias(c) for c in PG_STATS])
    out["games"] = frames["games"].filter(pl.col("game_date") <= cutoff).with_columns(
        [pl.when(pl.col("game_date") == cutoff).then(None).otherwise(pl.col(c))
         .alias(c) for c in GAME_STATS])
    return out


def main() -> int:
    cfg = load_config()["features"]
    conn = connect()
    frames = build_frames(conn)
    # zeroed minutes break the played-row per-40 denominators identically in
    # both runs, so full-run parity requires filling nulls the same way
    full = pipeline(frames, cfg)

    rng = random.Random(20260810)
    sample = full.sample(min(int(cfg["test_sample"]), full.height),
                         seed=rng.randrange(1 << 30))
    dates = sorted(sample["game_date"].unique().to_list())
    log.info(f"leakage test: {sample.height} sampled player-games across "
             f"{len(dates)} cutoff dates")

    fcols = [c for c in full.columns if c.startswith("f_")]
    failures = 0
    for cutoff in dates:
        snap = pipeline(truncate(frames, cutoff), cfg)
        want = sample.filter(pl.col("game_date") == cutoff)
        got = snap.join(want.select("game_id", "player_id"),
                        on=["game_id", "player_id"], how="inner")
        if got.height != want.height:
            log.info(f"  FAIL {cutoff}: {want.height} sampled rows, "
                     f"{got.height} present in snapshot run")
            failures += want.height - got.height
            continue
        merged = want.join(got, on=["game_id", "player_id"], suffix="_snap")
        for c in fcols:
            diff = merged.filter(
                ((pl.col(c) - pl.col(f"{c}_snap")).abs() > 1e-9)
                | (pl.col(c).is_null() != pl.col(f"{c}_snap").is_null()))
            if diff.height:
                failures += diff.height
                r = diff.row(0, named=True)
                log.info(f"  FAIL {cutoff} {c}: full={r[c]} asof={r[c + '_snap']} "
                         f"(game {r['game_id']}, player {r['player_id']})")

    if failures == 0:
        log.info(f"  PASS: all {sample.height} rows x {len(fcols)} features "
                 "identical when recomputed from as-of snapshots")

    # expansion teams: pipeline must produce non-null team features for TOR/POR
    teams = frames["teams"]
    exp_ids = [str(r["team_id"]) for r in teams.to_dicts()
               if r["abbreviation"] in ("TOR", "POR")]
    exp = full.filter(pl.col("team_id").is_in(exp_ids) & (pl.col("season") == 2026))
    bad = exp.filter(pl.col("f_team_pace").is_null() | pl.col("f_team_ortg").is_null()
                     | pl.col("f_opp_drtg").is_null())
    if exp.height == 0:
        log.info("  FAIL expansion: no TOR/POR feature rows at all")
        failures += 1
    elif bad.height:
        log.info(f"  FAIL expansion: {bad.height} TOR/POR rows with null team features")
        failures += bad.height
    else:
        first = exp.sort("game_date").head(1).row(0, named=True)
        log.info(f"  PASS expansion: {exp.height} TOR/POR rows, all team features "
                 f"non-null; game 1 fell back to league prior "
                 f"(pace {first['f_team_pace']:.1f}, ortg {first['f_team_ortg']:.1f})")

    log.info(f"leakage test result: {'PASS' if failures == 0 else 'FAIL'} "
             f"({failures} failures)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
