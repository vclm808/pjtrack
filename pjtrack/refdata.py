"""Reference data: aircraft type database and world airports."""
import csv
import gzip
import io
import json

from .config import BIZJET_TYPES
from .net import request

AIRCRAFT_DB_URL = "https://downloads.adsbexchange.com/downloads/basic-ac-db.json.gz"
AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"


def load_aircraft(con):
    _, _, body = request(AIRCRAFT_DB_URL, timeout=300)
    rows = []
    with gzip.open(io.BytesIO(body), "rt") as f:
        for line in f:
            a = json.loads(line)
            t = a.get("icaotype")
            # Owner/operator is intentionally not stored: analysis is aggregate only.
            rows.append((a["icao"].lower(), a.get("reg"), t, int(t in BIZJET_TYPES), int(bool(a.get("mil")))))
    con.executemany("INSERT OR REPLACE INTO aircraft VALUES (?,?,?,?,?)", rows)
    con.commit()
    return len(rows), sum(r[3] for r in rows)


def load_airports(con):
    _, _, body = request(AIRPORTS_URL, timeout=120)
    rows = []
    for r in csv.DictReader(io.StringIO(body.decode("utf-8"))):
        code = r["icao_code"] or r["gps_code"]
        if r["type"] in ("large_airport", "medium_airport", "small_airport") and len(code) == 4:
            rows.append((code, r["name"], r["iso_country"], float(r["latitude_deg"]),
                         float(r["longitude_deg"]), r["type"]))
    con.executemany("INSERT OR REPLACE INTO airports VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    return len(rows)
