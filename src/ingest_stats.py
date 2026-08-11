"""Stats ingestion: sportsdataverse feeds -> data/raw/stats/ -> data/wnba.db.

Flow per season: fetch schedule + player box + team box, write each verbatim to
data/raw/stats/ before parsing, then upsert games / player_games / availability /
players / teams. Validation checks run on every ingest and print failing rows.
"""

from __future__ import annotations

import re
import sqlite3

import polars as pl

from src.config import ROOT, load_config
from src.db import connect, get_meta, set_meta
from src.log import get_logger

log = get_logger("ingest")

RAW_DIR = ROOT / "data" / "raw" / "stats"

INJURY_WORDS = re.compile(
    r"KNEE|ANKLE|FOOT|LEG|SHOULDER|HIP|BACK|HAND|WRIST|ELBOW|CALF|HAMSTRING"
    r"|QUAD|GROIN|ACHILLES|CONCUSSION|INJURY|ILLNESS|HEEL|TOE|FINGER|THUMB"
    r"|NECK|SHIN|EYE|FACIAL|NOSE|RIB|HEALTH"
)


def classify_dnp(reason: str | None) -> str:
    """Map an ESPN DNP reason string onto the availability status vocabulary."""
    if not reason or not reason.strip():
        return "inactive"
    r = reason.upper()
    if "COACH" in r:
        return "dnp_coach"
    if "REST" in r or "LOAD MANAGEMENT" in r:
        return "dnp_rest"
    if INJURY_WORDS.search(r):
        return "dnp_injury"
    return "inactive"  # NOT WITH TEAM, PERSONAL, SUSPENSION, ...


def parse_plus_minus(value) -> tuple[int | None, str | None]:
    if value is None or value == "":
        return None, "plus_minus_missing"
    try:
        return int(str(value).replace("+", "")), None
    except ValueError:
        return None, "plus_minus_unparseable"


# ---------------------------------------------------------------- fetch


def fetch_season(year: int) -> dict[str, pl.DataFrame]:
    """Fetch one season's feeds, writing each raw to disk before any parsing."""
    from sportsdataverse import wnba

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    feeds = {}
    for name, loader in [
        ("schedule", wnba.load_wnba_schedule),
        ("player_box", wnba.load_wnba_player_boxscore),
        ("team_box", wnba.load_wnba_team_boxscore),
    ]:
        df = loader(seasons=[year])
        path = RAW_DIR / f"{name}_{year}.parquet"
        df.write_parquet(path)
        log.info(f"  raw: {name}_{year}.parquet  {df.height} rows")
        feeds[name] = df
    return feeds


def completed_franchise_games(sched: pl.DataFrame, scfg: dict) -> pl.DataFrame:
    excl = list(scfg["exclude_abbreviations"])
    return sched.filter(
        pl.col("status_type_completed")
        & pl.col("season_type").is_in(scfg["season_types"])
        & ~pl.col("home_abbreviation").is_in(excl)
        & ~pl.col("away_abbreviation").is_in(excl)
    )


# ---------------------------------------------------------------- parse + upsert


def ingest_season(conn: sqlite3.Connection, year: int, scfg: dict) -> dict:
    feeds = fetch_season(year)
    sched, pbox, tbox = feeds["schedule"], feeds["player_box"], feeds["team_box"]

    # -- games: filter to completed franchise games, reporting every drop
    games = completed_franchise_games(sched, scfg)
    n_not_final = sched.filter(~pl.col("status_type_completed")).height
    n_event = (
        sched.filter(pl.col("status_type_completed")).height
        - completed_franchise_games(
            sched.filter(pl.col("status_type_completed")), scfg
        ).height
    )
    log.info(
        f"  [{year}] schedule {sched.height} rows -> {games.height} completed "
        f"franchise games (dropped {n_not_final} not-final, "
        f"{n_event} event-team e.g. All-Star)"
    )

    game_ids = set(games["game_id"].cast(pl.Utf8).to_list())

    # -- possessions per team-game from the team box
    coeff = float(scfg["ft_possession_coeff"])
    tb = tbox.with_columns(
        pl.coalesce(pl.col("total_turnovers"), pl.col("turnovers")).alias("tov_all")
    ).with_columns(
        (
            pl.col("field_goals_attempted")
            - pl.col("offensive_rebounds")
            + pl.col("tov_all")
            + coeff * pl.col("free_throws_attempted")
        ).alias("poss")
    )
    poss = {
        (str(r["game_id"]), str(r["team_id"])): r["poss"]
        for r in tb.select("game_id", "team_id", "poss").to_dicts()
    }

    game_rows, games_missing_poss = [], []
    for g in games.to_dicts():
        gid = str(g["game_id"])
        ot = max(0, int(g["status_period"] or 4) - 4)
        hp = poss.get((gid, str(g["home_id"])))
        ap = poss.get((gid, str(g["away_id"])))
        if hp is None or ap is None:
            games_missing_poss.append(gid)
            poss_est = pace = h_rtg = a_rtg = None
        else:
            poss_est = (hp + ap) / 2
            pace = 40 * poss_est / (40 + 5 * ot)
            h_rtg = 100 * float(g["home_score"]) / hp if hp else None
            a_rtg = 100 * float(g["away_score"]) / ap if ap else None
        game_rows.append(
            (gid, str(g["game_date"]), int(g["season"]), int(g["season_type"]),
             str(g["home_id"]), str(g["away_id"]),
             g["home_score"], g["away_score"], ot,
             poss_est, pace, h_rtg, a_rtg, g["attendance"])
        )
    if games_missing_poss:
        log.info(
            f"  [{year}] {len(games_missing_poss)} games missing team box -> "
            f"possessions null (reason: team_box_missing): {games_missing_poss[:10]}"
        )

    # -- player_games + availability from the player box
    pb = pbox.filter(pl.col("game_id").cast(pl.Utf8).is_in(list(game_ids)))
    n_pb_dropped = pbox.height - pb.height
    log.info(
        f"  [{year}] player box {pbox.height} rows -> {pb.height} in franchise "
        f"games (dropped {n_pb_dropped}, reason: non_franchise_game)"
    )

    dupes = (
        pb.group_by("game_id", "athlete_id")
        .len()
        .filter(pl.col("len") > 1)
    )
    if dupes.height:
        log.info(f"  [{year}] {dupes.height} duplicate player-game rows in source, keeping first:")
        for d in dupes.head(10).to_dicts():
            log.info(f"    game {d['game_id']} athlete {d['athlete_id']} x{d['len']}")
        pb = pb.unique(subset=["game_id", "athlete_id"], keep="first")

    pg_rows, avail_rows, issues = [], [], {}
    for r in pb.to_dicts():
        gid, pid = str(r["game_id"]), str(r["athlete_id"])
        dnp = bool(r["did_not_play"])
        minutes = r["minutes"]
        issue = None
        if dnp:
            minutes = 0.0
        elif minutes is None:
            issue = "minutes_null_for_played_row"
        pm, pm_issue = parse_plus_minus(r["plus_minus"]) if not dnp else (None, None)
        issue = issue or pm_issue
        if issue:
            issues[issue] = issues.get(issue, 0) + 1
        pg_rows.append(
            (gid, str(r["game_date"]), pid, r["athlete_display_name"],
             str(r["team_id"]), str(r["opponent_team_id"]),
             (r["home_away"] or "").lower(), minutes,
             r["field_goals_attempted"], r["field_goals_made"],
             r["three_point_field_goals_attempted"], r["three_point_field_goals_made"],
             r["free_throws_attempted"], r["free_throws_made"], r["points"],
             r["offensive_rebounds"], r["defensive_rebounds"], r["rebounds"],
             r["assists"], r["steals"], r["blocks"], r["turnovers"], r["fouls"],
             pm, int(bool(r["starter"])), r["reason"] if dnp else None, issue)
        )
        if dnp:
            status = classify_dnp(r["reason"])
        else:
            status = "played"
        avail_rows.append((gid, pid, status, minutes if minutes is not None else 0.0))

    if issues:
        log.info(f"  [{year}] kept rows with issues (null + reason code): {issues}")

    players = pb.unique(subset=["athlete_id"], keep="last").select(
        "athlete_id", "athlete_display_name",
        "athlete_position_abbreviation", "athlete_jersey",
    )
    team_side = tb.filter(pl.col("game_id").cast(pl.Utf8).is_in(list(game_ids))).unique(
        subset=["team_id"], keep="last"
    ).select("team_id", "team_abbreviation", "team_location", "team_name", "team_display_name")

    # -- upsert, with before/after counts
    counts_before = {t: table_count(conn, t) for t in
                     ("games", "player_games", "availability", "players", "teams")}
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", game_rows)
        conn.executemany(
            "INSERT OR REPLACE INTO player_games VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", pg_rows)
        conn.executemany(
            "INSERT OR REPLACE INTO availability VALUES (?,?,?,?)", avail_rows)
        conn.executemany(
            "INSERT OR REPLACE INTO players VALUES (?,?,?,?)",
            [(str(p["athlete_id"]), p["athlete_display_name"],
              p["athlete_position_abbreviation"], p["athlete_jersey"])
             for p in players.to_dicts()])
        conn.executemany(
            "INSERT OR REPLACE INTO teams VALUES (?,?,?,?,?)",
            [(str(t["team_id"]), t["team_abbreviation"], t["team_location"],
              t["team_name"], t["team_display_name"]) for t in team_side.to_dicts()])
    counts_after = {t: table_count(conn, t) for t in counts_before}
    log.info(
        f"  [{year}] db rows: " + ", ".join(
            f"{t} {counts_before[t]}->{counts_after[t]}" for t in counts_before)
    )
    return {"games": len(game_rows), "player_games": len(pg_rows)}


def table_count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


# ---------------------------------------------------------------- live gap-fill


def _split_pair(v) -> tuple[int | None, int | None]:
    try:
        a, b = str(v).split("-")
        return int(a), int(b)
    except (ValueError, AttributeError):
        return None, None


def gapfill_live(conn: sqlite3.Connection, scfg: dict) -> int:
    """The release feed lags ~a week. Fetch completed games newer than it via
    the live ESPN scoreboard + summary endpoints, raw-first, same tables.
    When the release later catches up, its canonical rows replace these."""
    from datetime import date, datetime, timedelta, timezone

    from sportsdataverse import wnba

    last = conn.execute("SELECT MAX(game_date) d FROM games").fetchone()["d"]
    today_et = (datetime.now(timezone.utc) - timedelta(hours=4)).date()
    d = date.fromisoformat(last) + timedelta(days=1)
    if d > today_et:
        return 0
    excl = set(scfg["exclude_abbreviations"])
    new_games = 0
    while d <= today_et:
        try:
            sb = wnba.espn_wnba_scoreboard(dates=d.strftime("%Y%m%d"))
        except Exception as e:  # noqa: BLE001 — live API failure must not kill update
            log.info(f"  live scoreboard {d}: {type(e).__name__}: {e} — skipping day")
            d += timedelta(days=1)
            continue
        if sb.height:
            sb.write_parquet(RAW_DIR / f"live_scoreboard_{d}.parquet")
        for g in (sb.to_dicts() if sb.height else []):
            gid = str(g["game_id"])
            if (not g["status_type_completed"]
                    or g.get("season_type") not in (2, 3)
                    or g["home_abbreviation"] in excl
                    or g["away_abbreviation"] in excl
                    or conn.execute("SELECT 1 FROM games WHERE game_id = ?",
                                    (gid,)).fetchone()):
                continue
            try:
                s = wnba.espn_wnba_summary(int(gid))
                pb = s["boxscore_player"]
            except Exception as e:  # noqa: BLE001
                log.info(f"  live box {gid}: {type(e).__name__}: {e} — skipping game")
                continue
            pb.write_parquet(RAW_DIR / f"live_box_{gid}.parquet")
            _insert_live_game(conn, g, pb, d.isoformat(), scfg)
            new_games += 1
        d += timedelta(days=1)
    if new_games:
        log.info(f"live gap-fill: {new_games} games ingested ahead of the "
                 f"release feed (possessions from player sums — replaced by "
                 f"canonical release data when it catches up)")
    return new_games


def _insert_live_game(conn: sqlite3.Connection, g: dict, pb: pl.DataFrame,
                      game_date: str, scfg: dict) -> None:
    gid = str(g["game_id"])
    ot = max(0, int(g.get("status_period") or 4) - 4)
    coeff = float(scfg["ft_possession_coeff"])

    pg_rows, avail_rows, team_tot = [], [], {}
    for r in pb.to_dicts():
        pid = str(r["athlete_id"])
        tid = str(r["team_id"])
        fgm, fga = _split_pair(r["field_goals_made_field_goals_attempted"])
        f3m, f3a = _split_pair(
            r["three_point_field_goals_made_three_point_field_goals_attempted"])
        ftm, fta = _split_pair(r["free_throws_made_free_throws_attempted"])
        dnp = bool(r["did_not_play"])
        try:
            minutes = 0.0 if dnp else float(r["minutes"] or 0)
        except (TypeError, ValueError):
            minutes = 0.0
        pm, _ = parse_plus_minus(r.get("plus_minus")) if not dnp else (None, None)
        home_away = ("home" if tid == str(g["home_id"]) else "away")
        opp = str(g["away_id"]) if home_away == "home" else str(g["home_id"])

        def num(key):
            v = r.get(key)
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        oreb, dreb = num("offensive_rebounds"), num("defensive_rebounds")
        reb = num("rebounds")
        pg_rows.append(
            (gid, game_date, pid, r["athlete_display_name"], tid, opp,
             home_away, minutes, fga, fgm, f3a, f3m, fta, ftm, num("points"),
             oreb, dreb, reb, num("assists"), num("steals"), num("blocks"),
             num("turnovers"), num("fouls"), pm, int(bool(r["starter"])),
             r["reason"] if dnp else None, "live_ingest"))
        avail_rows.append((gid, pid, classify_dnp(r["reason"]) if dnp
                           else "played", minutes))
        t = team_tot.setdefault(tid, {"fga": 0, "oreb": 0, "tov": 0, "fta": 0,
                                      "pts": 0})
        if not dnp:
            t["fga"] += fga or 0
            t["oreb"] += oreb or 0
            t["tov"] += num("turnovers") or 0
            t["fta"] += fta or 0
            t["pts"] += num("points") or 0

    poss = {tid: t["fga"] - t["oreb"] + t["tov"] + coeff * t["fta"]
            for tid, t in team_tot.items()}
    hp, ap = poss.get(str(g["home_id"])), poss.get(str(g["away_id"]))
    poss_est = (hp + ap) / 2 if hp and ap else None
    pace = 40 * poss_est / (40 + 5 * ot) if poss_est else None
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (gid, game_date, int(g["season_year"]), int(g["season_type"]),
             str(g["home_id"]), str(g["away_id"]), g["home_score"],
             g["away_score"], ot, poss_est, pace,
             100 * float(g["home_score"]) / hp if hp else None,
             100 * float(g["away_score"]) / ap if ap else None,
             g.get("attendance")))
        conn.executemany(
            "INSERT OR REPLACE INTO player_games VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", pg_rows)
        conn.executemany(
            "INSERT OR REPLACE INTO availability VALUES (?,?,?,?)", avail_rows)
        conn.executemany(
            "INSERT OR IGNORE INTO players VALUES (?,?,?,?)",
            [(str(r["athlete_id"]), r["athlete_display_name"],
              (r.get("athlete_position") or "")[:1] or None,
              r.get("athlete_jersey")) for r in pb.to_dicts()])
    log.info(f"  live: {g['away_abbreviation']} @ {g['home_abbreviation']} "
             f"{game_date} ({gid}): {len(pg_rows)} player rows")


# ---------------------------------------------------------------- validation


def run_validation(conn: sqlite3.Connection, cfg: dict) -> tuple[int, int]:
    """Run every check, print failing rows. Returns (passed, total)."""
    scfg = cfg["stats"]
    checks = []

    # 1. games per team match the schedule feed's completed count
    fails = []
    for sched_path in sorted(RAW_DIR.glob("schedule_*.parquet")):
        year = int(sched_path.stem.split("_")[1])
        sched = completed_franchise_games(pl.read_parquet(sched_path), scfg)
        expected = {}
        for side in ("home", "away"):
            for r in sched.group_by(f"{side}_id").len().to_dicts():
                tid = str(r[f"{side}_id"])
                expected[tid] = expected.get(tid, 0) + r["len"]
        sched_ids = set(sched["game_id"].cast(pl.Utf8).to_list())
        live_only = [str(r["game_id"]) for r in conn.execute(
            "SELECT game_id FROM games WHERE season = ?", (year,))
            if str(r["game_id"]) not in sched_ids]
        if live_only:
            log.info(f"  note: {len(live_only)} season-{year} games are "
                     f"live-ingested ahead of the release schedule — excluded "
                     f"from the schedule-count comparison")
        ph = ",".join("?" * len(live_only)) or "''"
        got = {
            str(r["team_id"]): r["n"]
            for r in conn.execute(
                f"SELECT team_id, COUNT(*) n FROM ("
                f" SELECT home_team_id team_id FROM games WHERE season = ?"
                f"  AND game_id NOT IN ({ph})"
                f" UNION ALL SELECT away_team_id FROM games WHERE season = ?"
                f"  AND game_id NOT IN ({ph})"
                f") GROUP BY team_id",
                (year, *live_only, year, *live_only)).fetchall()
        }
        for tid, exp in expected.items():
            if got.get(tid, 0) != exp:
                fails.append(f"season {year} team {tid}: db {got.get(tid, 0)} vs schedule {exp}")
        cap = int(scfg["expected_games_per_team"])
        cup_finals = [
            str(g) for g in sched.filter(
                pl.col("notes_headline").str.contains(scfg["cup_final_note"], literal=True)
            )["game_id"].to_list()
        ]
        for tid, n in got.items():
            reg = conn.execute(
                "SELECT COUNT(*) n FROM games WHERE season = ? AND season_type = 2 "
                "AND (home_team_id = ? OR away_team_id = ?) "
                f"AND game_id NOT IN ({','.join('?' * len(cup_finals)) or 'NULL'})",
                (year, tid, tid, *cup_finals)).fetchone()["n"]
            if reg > cap:
                fails.append(f"season {year} team {tid}: {reg} regular-season games > {cap}")
    checks.append(("games per team match published schedule", fails))

    # 2. team minutes sum to 200 + 25 per OT
    tol = float(scfg["minutes_sum_tolerance"])
    rows = conn.execute(
        "SELECT pg.game_id, pg.team_id, SUM(pg.minutes) mins, g.overtime_periods ot "
        "FROM player_games pg JOIN games g ON g.game_id = pg.game_id "
        "GROUP BY pg.game_id, pg.team_id "
        "HAVING ABS(SUM(pg.minutes) - (200 + 25 * g.overtime_periods)) > ?", (tol,)
    ).fetchall()
    checks.append((f"team minutes sum to 200 (+25/OT, tol ±{tol:g})",
                   [f"game {r['game_id']} team {r['team_id']}: {r['mins']} "
                    f"(expected {200 + 25 * r['ot']})" for r in rows]))

    # 3. points reconcile
    rows = conn.execute(
        "SELECT game_id, player_id, player_name, fgm, fg3m, ftm, points "
        "FROM player_games WHERE dnp_reason IS NULL "
        "AND 2 * (fgm - fg3m) + 3 * fg3m + ftm != points").fetchall()
    checks.append(("2*(FGM-FG3M) + 3*FG3M + FTM = PTS",
                   [f"game {r['game_id']} {r['player_name']}: "
                    f"fgm {r['fgm']} fg3m {r['fg3m']} ftm {r['ftm']} != pts {r['points']}"
                    for r in rows]))

    # 4. rebounds reconcile
    rows = conn.execute(
        "SELECT game_id, player_id, player_name, oreb, dreb, reb "
        "FROM player_games WHERE dnp_reason IS NULL AND oreb + dreb != reb").fetchall()
    checks.append(("REB = OREB + DREB",
                   [f"game {r['game_id']} {r['player_name']}: "
                    f"{r['oreb']}+{r['dreb']} != {r['reb']}" for r in rows]))

    # 5. no duplicate player-games (source dupes are deduped and logged at ingest)
    rows = conn.execute(
        "SELECT game_id, player_id, COUNT(*) n FROM player_games "
        "GROUP BY game_id, player_id HAVING n > 1").fetchall()
    checks.append(("no player twice in one game",
                   [f"game {r['game_id']} player {r['player_id']} x{r['n']}" for r in rows]))

    # 6. no game date outside the season window
    fails = []
    win = scfg["season_window"]
    rows = conn.execute(
        "SELECT game_id, game_date FROM games WHERE season = ? "
        "AND (game_date < ? OR game_date > ?)",
        (cfg["season"], win["start"], win["end"])).fetchall()
    fails += [f"game {r['game_id']} on {r['game_date']} outside "
              f"[{win['start']}, {win['end']}]" for r in rows]
    for sched_path in sorted(RAW_DIR.glob("schedule_*.parquet")):
        year = int(sched_path.stem.split("_")[1])
        if year == int(cfg["season"]):
            continue
        sched = completed_franchise_games(pl.read_parquet(sched_path), scfg)
        lo, hi = str(sched["game_date"].min()), str(sched["game_date"].max())
        rows = conn.execute(
            "SELECT game_id, game_date FROM games WHERE season = ? "
            "AND (game_date < ? OR game_date > ?)", (year, lo, hi)).fetchall()
        fails += [f"game {r['game_id']} on {r['game_date']} outside "
                  f"season {year} [{lo}, {hi}]" for r in rows]
    checks.append(("no game date outside season window", fails))

    # 7. every player_id in player_games exists in players
    rows = conn.execute(
        "SELECT DISTINCT player_id FROM player_games "
        "WHERE player_id NOT IN (SELECT player_id FROM players)").fetchall()
    checks.append(("player_games.player_id all exist in players",
                   [f"player_id {r['player_id']}" for r in rows]))

    passed = 0
    log.info("validation:")
    for name, fails in checks:
        if fails:
            log.info(f"  FAIL  {name} — {len(fails)} rows:")
            for f in fails[:10]:
                log.info(f"        {f}")
            if len(fails) > 10:
                log.info(f"        ... and {len(fails) - 10} more")
        else:
            passed += 1
            log.info(f"  pass  {name}")
    return passed, len(checks)


# ---------------------------------------------------------------- entry point


def print_summary(conn: sqlite3.Connection, passed: int, total: int) -> None:
    seasons = [r["season"] for r in
               conn.execute("SELECT DISTINCT season FROM games ORDER BY season")]
    n_games = table_count(conn, "games")
    n_pg = table_count(conn, "player_games")
    dates = conn.execute("SELECT MIN(game_date) lo, MAX(game_date) hi FROM games").fetchone()
    avail = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM availability GROUP BY status ORDER BY n DESC")}
    log.info("summary:")
    log.info(f"  seasons: {seasons}")
    log.info(f"  games: {n_games}   player-games: {n_pg}")
    log.info(f"  date range: {dates['lo']} .. {dates['hi']}")
    log.info(f"  availability: {avail}")
    log.info(f"  validation: {passed}/{total} checks passed")


def run_update() -> int:
    cfg = load_config()
    scfg = cfg["stats"]
    current = int(cfg["season"])
    conn = connect()
    last = get_meta(conn, "last_ingested_game_date")

    # cheap new-game check: fetch only the current-season schedule first
    from sportsdataverse import wnba

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    sched = wnba.load_wnba_schedule(seasons=[current])
    sched.write_parquet(RAW_DIR / f"schedule_{current}.parquet")
    done = completed_franchise_games(sched, scfg)
    max_done = str(done["game_date"].max())

    if last is not None and max_done <= last:
        n_live = gapfill_live(conn, scfg)
        if n_live == 0:
            log.info(f"0 new games (release feed through {max_done}, "
                     f"live gap-fill current)")
        passed, total = run_validation(conn, cfg)
        print_summary(conn, passed, total)
        return 0

    if last is None:
        seasons = list(range(int(scfg["first_season"]), current + 1))
        log.info(f"first run: backfilling seasons {seasons[0]}..{seasons[-1]}")
    else:
        seasons = [current]
        log.info(f"new games since {last} (latest now {max_done}), refreshing {current}")

    for year in seasons:
        ingest_season(conn, year, scfg)

    set_meta(conn, "last_ingested_game_date", max_done)
    gapfill_live(conn, scfg)
    passed, total = run_validation(conn, cfg)
    print_summary(conn, passed, total)
    return 0
