"""SQLite schema and connection helpers — data/wnba.db is the single source of truth."""

import sqlite3

from src.config import ROOT

DB_PATH = ROOT / "data" / "wnba.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_id           TEXT PRIMARY KEY,
    game_date         TEXT NOT NULL,
    season            INTEGER NOT NULL,
    season_type       INTEGER NOT NULL,
    home_team_id      TEXT NOT NULL,
    away_team_id      TEXT NOT NULL,
    home_score        INTEGER,
    away_score        INTEGER,
    overtime_periods  INTEGER NOT NULL DEFAULT 0,
    possessions_est   REAL,
    pace              REAL,
    home_off_rating   REAL,
    away_off_rating   REAL,
    attendance        INTEGER
);

CREATE TABLE IF NOT EXISTS player_games (
    game_id      TEXT NOT NULL,
    game_date    TEXT NOT NULL,
    player_id    TEXT NOT NULL,
    player_name  TEXT,
    team_id      TEXT NOT NULL,
    opponent_id  TEXT,
    home_away    TEXT,
    minutes      REAL,
    fga INTEGER, fgm INTEGER, fg3a INTEGER, fg3m INTEGER,
    fta INTEGER, ftm INTEGER, points INTEGER,
    oreb INTEGER, dreb INTEGER, reb INTEGER,
    ast INTEGER, stl INTEGER, blk INTEGER, tov INTEGER, pf INTEGER,
    plus_minus   INTEGER,
    started      INTEGER,
    dnp_reason   TEXT,
    issue        TEXT,
    PRIMARY KEY (game_id, player_id)
);

CREATE TABLE IF NOT EXISTS availability (
    game_id        TEXT NOT NULL,
    player_id      TEXT NOT NULL,
    status         TEXT NOT NULL
                   CHECK (status IN ('played','dnp_coach','dnp_injury',
                                     'dnp_rest','inactive','not_on_roster')),
    minutes_played REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (game_id, player_id)
);

CREATE TABLE IF NOT EXISTS players (
    player_id   TEXT PRIMARY KEY,
    player_name TEXT,
    position    TEXT,
    jersey      TEXT
);

CREATE TABLE IF NOT EXISTS teams (
    team_id      TEXT PRIMARY KEY,
    abbreviation TEXT,
    location     TEXT,
    name         TEXT,
    display_name TEXT
);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at_utc TEXT NOT NULL,
    game_id         TEXT NOT NULL,   -- provider event id; mapped to ESPN games in phase 3
    book            TEXT NOT NULL,
    market          TEXT NOT NULL,
    player_name_raw TEXT NOT NULL,   -- book's exact spelling, never overwritten
    line            REAL,
    over_price      INTEGER,
    under_price     INTEGER,
    is_alternate    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (captured_at_utc, game_id, book, market, player_name_raw, line, is_alternate)
);

CREATE TABLE IF NOT EXISTS odds_event_map (
    event_id          TEXT PRIMARY KEY,   -- The Odds API event id
    game_id           TEXT,               -- ESPN game id, null if unmatched
    game_date_et      TEXT NOT NULL,      -- Eastern calendar date of tip-off
    commence_time_utc TEXT NOT NULL,
    home_team_raw     TEXT,
    away_team_raw     TEXT,
    match_status      TEXT NOT NULL       -- ok | stats_pending | game_unmatched
);

CREATE TABLE IF NOT EXISTS name_map (
    raw_name   TEXT NOT NULL,             -- book spelling, untouched
    source     TEXT NOT NULL,
    player_id  TEXT NOT NULL,
    confidence REAL NOT NULL,
    mapped_by  TEXT NOT NULL,             -- exact | team_date | user
    PRIMARY KEY (raw_name, source)
);

CREATE TABLE IF NOT EXISTS prop_lines (
    snapshot_id     INTEGER PRIMARY KEY,  -- 1:1 with odds_snapshots: nothing dropped
    captured_at_utc TEXT NOT NULL,
    event_id        TEXT NOT NULL,
    game_id         TEXT,
    game_date_et    TEXT,
    book            TEXT NOT NULL,
    market          TEXT NOT NULL,
    player_name_raw TEXT NOT NULL,
    player_id       TEXT,
    line            REAL,
    over_price      INTEGER,
    under_price     INTEGER,
    is_alternate    INTEGER NOT NULL DEFAULT 0,
    is_whole_line   INTEGER NOT NULL DEFAULT 0,
    is_voided       INTEGER NOT NULL DEFAULT 0,  -- scratched/DNP: NOT an under
    actual          REAL,
    result          TEXT CHECK (result IN ('over','under','push') OR result IS NULL),
    match_status    TEXT NOT NULL         -- ok | voided | stats_pending |
                                          -- game_unmatched | name_unmatched | market_unknown
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
