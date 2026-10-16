"""Airline-behaviour indicator from the adsb.lol archive (see history.py for the stream format).

Per (tracked airport, day, callsign prefix, registration country, class) we count departures,
arrivals, turnarounds (arrived and left again the same UTC day), stays (last arrival of the day
with no later departure: the aircraft overnights) and returns-to-origin (departed and landed back
at the same airport). The prefix is the ICAO airline designator (first 3 letters of an
"ABC123"-style callsign); anything else is '-'. Registration country comes from the ICAO 24-bit
address block, so "foreign carrier" needs no external airline database.
Aggregate counts only; no per-aircraft rows are stored.
"""
import io
import re
import sqlite3
import tarfile
import time
import zlib
from collections import Counter

from .config import AIRPORTS, BIZJET_TYPES
from .history import NAME_RE, _Chain, _load, asset_urls

LEG_GAP_S = 1800
NEAR_KM = 12
LOW_ALT_FT = 3000
AIR_ALT_FT = 1500
TURN_MAX_S = 6 * 3600     # ground time for a "turnaround" (longer = parked, not turned)
AIRLINE_RE = re.compile(r"^([A-Z]{3})\d")

# ICAO 24-bit address blocks of the countries we track (Annex 10 allocation).
HEX_BLOCKS = [
    (0x896000, 0x896FFF, "AE"), (0x738000, 0x73FFFF, "IL"), (0x06A000, 0x06A3FF, "QA"),
    (0x894000, 0x894FFF, "BH"), (0x740000, 0x747FFF, "JO"), (0x748000, 0x74FFFF, "LB"),
    (0x4C8000, 0x4C83FF, "CY"), (0x710000, 0x717FFF, "SA"), (0x706000, 0x706FFF, "KW"),
    (0x70C000, 0x70C3FF, "OM"), (0x010000, 0x017FFF, "EG"), (0x4B8000, 0x4BFFFF, "TR"),
    (0x730000, 0x737FFF, "IR"), (0x728000, 0x72FFFF, "IQ"), (0x778000, 0x77FFFF, "SY"),
    (0x890000, 0x890FFF, "YE"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS air_counts (
    airport TEXT, day TEXT, prefix TEXT, reg_cc TEXT, cls TEXT,
    dep INTEGER, arr INTEGER, turn INTEGER, stay INTEGER, rto INTEGER,
    PRIMARY KEY (airport, day, prefix, reg_cc, cls)
);
CREATE TABLE IF NOT EXISTS air_days (day TEXT PRIMARY KEY, seconds REAL, bytes INTEGER, traces INTEGER);
"""


def connect(path):
    con = sqlite3.connect(path, timeout=30)
    con.executescript(SCHEMA)
    return con


def reg_country(hx):
    n = int(hx, 16)
    for lo, hi, cc in HEX_BLOCKS:
        if lo <= n <= hi:
            return cc
    return "??"


def flights(doc):
    """Yield (t0, t1, first, last, callsign) per airborne flight; point = (lat, lon, low).
    Unlike history._legs, a leg is also cut where the aircraft lands, so a quick turn seen
    without a reception gap still counts as an arrival plus a departure."""
    base = doc.get("timestamp", 0)
    cs, leg, last_t, airborne = None, [], None, False
    for p in doc.get("trace") or ():
        t = base + p[0]
        d = p[8] if len(p) > 8 and isinstance(p[8], dict) else None
        if d and d.get("flight"):
            cs = d["flight"].strip()
        flags = p[6] if len(p) > 6 and isinstance(p[6], int) else 0
        alt = p[3]
        ground = alt == "ground"
        low = ground or (isinstance(alt, (int, float)) and alt < LOW_ALT_FT)
        if leg and ((flags & 2) or t - last_t > LEG_GAP_S):
            if airborne:
                yield leg[0][0], leg[-1][0], leg[0][1:], leg[-1][1:], leg_cs
            leg, airborne = [], False
        if not leg:
            leg_cs = cs
        leg_cs = leg_cs or cs
        leg.append((t, p[1], p[2], low))
        if not ground and isinstance(alt, (int, float)) and alt > AIR_ALT_FT:
            airborne = True
        elif ground and airborne:  # landed: close this flight, the ground point also starts the next
            yield leg[0][0], t, leg[0][1:], (p[1], p[2], True), leg_cs
            leg, airborne, leg_cs = [(t, p[1], p[2], True)], False, cs
        last_t = t
    if leg and airborne:
        yield leg[0][0], leg[-1][0], leg[0][1:], leg[-1][1:], leg_cs


def _airport(idx, pt, tracked):
    lat, lon, low = pt
    if not low:
        return None
    ap = idx.nearest(lat, lon, NEAR_KM)
    return ap if ap in tracked else None


def process_day(con, day, idx, log=print):
    if con.execute("SELECT 1 FROM air_days WHERE day=?", (day.isoformat(),)).fetchone():
        return "done"
    urls = asset_urls(day)
    if not urls:
        log(f"  {day}: no archive")
        return "missing"
    t0 = time.time()
    stream = _Chain(urls)
    tracked = set(AIRPORTS)
    c = Counter()  # (airport, prefix, cc, cls, field) -> n
    ntr = 0
    with tarfile.open(fileobj=io.BufferedReader(stream, 1 << 20), mode="r|") as tar:
        for m in tar:
            mm = NAME_RE.search(m.name)
            if not mm or not m.isfile():
                continue
            try:
                doc = _load(tar.extractfile(m).read())
            except (ValueError, OSError, zlib.error):
                continue
            ntr += 1
            hx = mm.group(1)
            cls = "mil" if doc.get("dbFlags", 0) & 1 else "biz" if doc.get("t") in BIZJET_TYPES else "civ"
            cc = None
            events = []  # (t, 'dep'|'arr', airport, prefix)
            for ta, tb, first, last, cs in flights(doc):
                dep, arr = _airport(idx, first, tracked), _airport(idx, last, tracked)
                if not (dep or arr):
                    continue
                mp = AIRLINE_RE.match(cs or "")
                pre = mp.group(1) if mp else "-"
                if dep:
                    events.append((ta, "dep", dep, pre))
                if arr:
                    events.append((tb, "arr", arr, pre))
                if dep and dep == arr:
                    cc = cc or reg_country(hx)
                    c[dep, pre, cc, cls, "rto"] += 1
            if not events:
                continue
            cc = cc or reg_country(hx)
            events.sort()
            for i, (t, kind, ap, pre) in enumerate(events):
                c[ap, pre, cc, cls, kind] += 1
                if kind != "arr":
                    continue
                nxt = events[i + 1] if i + 1 < len(events) else None
                if nxt and nxt[1] == "dep" and nxt[2] == ap and nxt[0] - t <= TURN_MAX_S:
                    c[ap, pre, cc, cls, "turn"] += 1
                elif nxt is None:
                    c[ap, pre, cc, cls, "stay"] += 1
    rows = {}
    for (ap, pre, cc, cls, f), n in c.items():
        rows.setdefault((ap, pre, cc, cls), Counter())[f] = n
    con.executemany("INSERT OR REPLACE INTO air_counts VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(ap, day.isoformat(), pre, cc, cls, v["dep"], v["arr"], v["turn"], v["stay"], v["rto"])
                     for (ap, pre, cc, cls), v in rows.items()])
    secs = time.time() - t0
    con.execute("INSERT OR REPLACE INTO air_days VALUES (?,?,?,?)", (day.isoformat(), secs, stream.bytes, ntr))
    con.commit()
    deps = sum(n for k, n in c.items() if k[4] == "dep")
    arrs = sum(n for k, n in c.items() if k[4] == "arr")
    log(f"  {day}: {deps} departures, {arrs} arrivals at tracked airports, {ntr} traces, "
        f"{stream.bytes / 1e9:.2f} GB in {secs:.0f}s")
    return "ok"
