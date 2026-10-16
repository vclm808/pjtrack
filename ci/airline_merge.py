"""Merge CI airline databases into data/ind_airline.db.
Usage: python3 ci/airline_merge.py DIR   (DIR contains */ind_airline.db from `gh run download`)"""
import pathlib
import sys

sys.path.insert(0, ".")
from pjtrack import ind_airline

con = ind_airline.connect("data/ind_airline.db")
n = 0
for p in sorted(pathlib.Path(sys.argv[1]).glob("*/ind_airline.db")):
    con.execute("ATTACH ? AS r", (str(p),))
    if con.execute("SELECT count(*) FROM r.air_days").fetchone()[0]:
        con.execute("INSERT OR REPLACE INTO air_counts SELECT * FROM r.air_counts")
        con.execute("INSERT OR REPLACE INTO air_days SELECT * FROM r.air_days")
        con.commit()
        n += 1
    con.execute("DETACH r")
print(f"merged {n} days")
