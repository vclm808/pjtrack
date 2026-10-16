"""CI worker: stream one adsb.lol archive day into a fresh SQLite file (no aircraft/airport tables kept).
Usage: python3 ci/archive_day.py YYYY-MM-DD"""
import datetime as dt
import sys

sys.path.insert(0, ".")
from pjtrack import history, refdata
from pjtrack.db import connect
from pjtrack.live import AirportIndex

day = dt.date.fromisoformat(sys.argv[1])
con = connect()
refdata.load_aircraft(con)
refdata.load_airports(con)
con.commit()
bizjets = {r[0] for r in con.execute("SELECT icao24 FROM aircraft WHERE is_bizjet=1")}
idx = AirportIndex(con)
for attempt in range(3):
    try:
        r = history.process_day(con, day, bizjets, idx, log=lambda m: print(m, flush=True))
        break
    except Exception as e:
        print(f"attempt {attempt + 1} failed: {e}", flush=True)
else:
    sys.exit(1)
if r == "missing":
    sys.exit(0)
con.execute("DROP TABLE aircraft")
con.execute("DROP TABLE airports")
con.commit()
con.execute("VACUUM")
