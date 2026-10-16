"""SQLite storage."""
import sqlite3

from .config import DATA, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS aircraft (
    icao24 TEXT PRIMARY KEY, reg TEXT, type TEXT, is_bizjet INTEGER, mil INTEGER
);
CREATE TABLE IF NOT EXISTS airports (
    icao TEXT PRIMARY KEY, name TEXT, country TEXT, lat REAL, lon REAL, kind TEXT
);
-- One row per observed flight leg. source = 'opensky' or 'live'.
CREATE TABLE IF NOT EXISTS flights (
    source TEXT NOT NULL, icao24 TEXT NOT NULL, callsign TEXT,
    dep_airport TEXT, arr_airport TEXT,
    dep_time INTEGER NOT NULL, arr_time INTEGER,
    PRIMARY KEY (source, icao24, dep_time)
);
CREATE INDEX IF NOT EXISTS flights_dep ON flights(dep_airport, dep_time);
-- Which OpenSky airport-days have been fetched (so backfill is resumable).
CREATE TABLE IF NOT EXISTS fetch_log (
    airport TEXT, day TEXT, n INTEGER, fetched_at INTEGER, PRIMARY KEY (airport, day)
);
-- Live tracker per-aircraft state.
CREATE TABLE IF NOT EXISTS live_state (
    icao24 TEXT PRIMARY KEY, callsign TEXT, dep_airport TEXT, dep_time INTEGER,
    last_seen INTEGER, last_lat REAL, last_lon REAL, last_alt INTEGER, last_rate INTEGER,
    on_ground INTEGER
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
-- adsb.lol archive: all-aircraft departure counts per tracked airport/day, and processed days.
CREATE TABLE IF NOT EXISTS hist_log (
    source TEXT, airport TEXT, day TEXT, n_all INTEGER, PRIMARY KEY (source, airport, day)
);
CREATE TABLE IF NOT EXISTS hist_days (source TEXT, day TEXT, seconds REAL, bytes INTEGER,
    PRIMARY KEY (source, day));
"""


def connect():
    DATA.mkdir(exist_ok=True)
    # Default rollback journal (not WAL): the project lives in iCloud Drive,
    # which does not sync -wal/-shm sidecar files reliably.
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def get_meta(con, k, default=None):
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def set_meta(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, str(v)))
