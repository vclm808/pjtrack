"""Merge CI result databases (one per day) into data/pjtrack.db.
Usage: python3 ci/merge.py DIR   (DIR contains */pjtrack.db from `gh run download`)"""
import pathlib
import sys

sys.path.insert(0, ".")
from pjtrack.db import connect

con = connect()
n = 0
for p in sorted(pathlib.Path(sys.argv[1]).glob("*/pjtrack.db")):
    con.execute("ATTACH ? AS r", (str(p),))
    if not con.execute("SELECT count(*) FROM r.hist_days").fetchone()[0]:
        con.execute("DETACH r")
        continue
    con.execute("INSERT OR REPLACE INTO flights SELECT * FROM r.flights WHERE source='adsblol'")
    con.execute("INSERT OR REPLACE INTO hist_log SELECT * FROM r.hist_log")
    con.execute("INSERT OR REPLACE INTO hist_days SELECT * FROM r.hist_days")
    con.commit()
    con.execute("DETACH r")
    n += 1
print(f"merged {n} days")
