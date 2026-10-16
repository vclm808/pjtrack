"""CI worker: stream one adsb.lol archive day into data/ind_airline.db (airline counts only).
Usage: python3 ci/airline_day.py YYYY-MM-DD [OUT.db]"""
import datetime as dt
import pathlib
import sqlite3
import sys

sys.path.insert(0, ".")
from pjtrack import ind_airline, refdata
from pjtrack.db import SCHEMA
from pjtrack.live import AirportIndex

day = dt.date.fromisoformat(sys.argv[1])
ref = sqlite3.connect(":memory:")
ref.executescript(SCHEMA)
refdata.load_airports(ref)
idx = AirportIndex(ref)
out = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "data/ind_airline.db")
out.parent.mkdir(exist_ok=True)
con = ind_airline.connect(out)
for attempt in range(3):
    try:
        r = ind_airline.process_day(con, day, idx, log=lambda m: print(m, flush=True))
        break
    except Exception as e:
        print(f"attempt {attempt + 1} failed: {e}", flush=True)
else:
    sys.exit(1)
