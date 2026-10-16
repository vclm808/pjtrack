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


# ---------------------------------------------------------------- backtest
# Same windows and thresholds as backtest.py. Movements = departures + arrivals of airline
# callsigns ("ABC123") on civil, non-business-jet aircraft. Shares normalise away receiver
# coverage, which changes by orders of magnitude in this archive.
import datetime as dt
import random
import statistics
from collections import defaultdict

AP_CC = {ap: cc for ap, (cc, _) in AIRPORTS.items()}
MIN_MOVES = 20            # airline movements/day below this = not covered that day
COVERAGE_FLOOR = 0.4      # ... or below this fraction of the baseline median
CUT = 1 / 1.3


def carrier_home(con):
    """Home country of each airline prefix = the tracked country most of its aircraft are registered
    in. Wet-leased aircraft (e.g. foreign-registered jets flying as ELY) then still count as home."""
    by = defaultdict(Counter)
    for pre, rcc, n in con.execute("""SELECT prefix, reg_cc, sum(dep + arr) FROM air_counts
                                      WHERE cls = 'civ' AND prefix != '-' GROUP BY prefix, reg_cc"""):
        by[pre][rcc] += n
    return {pre: c.most_common(1)[0][0] for pre, c in by.items()}


def load(con):
    """daily[(cc, day)] = Counter of metrics; carriers[(cc, prefix, day)] = movements."""
    daily, carriers = defaultdict(Counter), Counter()
    home = carrier_home(con)
    for ap, day, pre, rcc, cls, dep, arr, turn, stay, rto in con.execute("SELECT * FROM air_counts"):
        cc, d = AP_CC[ap], dt.date.fromisoformat(day)
        x = daily[cc, d]
        x["all"] += dep + arr
        if cls != "civ" or pre == "-":
            continue
        foreign = home.get(pre, rcc) != cc
        x["air"] += dep + arr
        x["air_foreign" if foreign else "air_home"] += dep + arr
        if foreign:
            x["f_arr"] += arr
            x["f_turn"] += turn
            x["f_stay"] += stay
            carriers[cc, pre, d] += dep + arr
            x["f_carriers_" + pre] = 1
        x["rto"] += rto
    days = {d for d, in con.execute("SELECT day FROM air_days")}
    return daily, carriers, {dt.date.fromisoformat(d) for d in days}


METRICS = {
    # name: (numerator, denominator or None, direction that would mean airlines pulling back)
    "foreign_share": ("air_foreign", "air", -1),
    "foreign_moves": ("air_foreign", None, -1),
    "foreign_carriers": ("n_carriers", None, -1),
    "turn_share": ("f_turn", "f_arr", +1),
    "stay_share": ("f_stay", "f_arr", -1),
}


def series(daily, have, countries, date, metric, strict=True):
    """strict: drop days with < MIN_MOVES airline movements (receiver outage and real shutdown look
    the same), used for baseline/lead so outages cannot fake a cut. Non-strict keeps every processed
    day, used for the reaction window where a real shutdown should show as a collapse."""
    num, den, _ = METRICS[metric]
    tot = {k: sum(daily.get((c, date + dt.timedelta(days=k)), Counter())["air"] for c in countries)
           for k in range(-24, 8) if date + dt.timedelta(days=k) in have}
    base = sorted(v for k, v in tot.items() if k < -7)
    # Receiver outages show up as days with a fraction of normal traffic for home and foreign
    # carriers alike (e.g. Israel 2024-09-24..28); drop them rather than read them as cuts.
    floor = max(MIN_MOVES * len(countries), COVERAGE_FLOOR * base[len(base) // 2] if base else 0)
    out = {}
    for k in range(-24, 8):
        d = date + dt.timedelta(days=k)
        if d not in have:
            continue
        xs = [daily.get((c, d), Counter()) for c in countries]
        if strict and tot[k] < floor:
            continue
        if num == "n_carriers":
            out[k] = sum(sum(1 for f in x if f.startswith("f_carriers_")) for x in xs)
            continue
        n = sum(x[num] for x in xs)
        if den:
            dd = sum(x[den] for x in xs)
            if dd < 5:
                continue
            out[k] = n / dd
        else:
            out[k] = n
    return out


def stats(s, base=range(-24, -7), lead=range(-7, 0)):
    b_ = [s[k] for k in base if k in s]
    l_ = [s[k] for k in lead if k in s]
    react = [s[k] for k in range(0, 4) if k in s]
    if len(b_) < min(10, len(base) - 2) or len(l_) < 3:
        return None
    b, l = statistics.mean(b_), statistics.mean(l_)
    var = max(statistics.variance(b_), 1e-9 if b < 1.5 else b)  # counts: floor at Poisson
    z = (l - b) / (var / len(l_) + var / len(b_)) ** 0.5
    return dict(base=b, lead=l, ratio=l / b if b else None, z=z, nb=len(b_), nl=len(l_),
                react=statistics.mean(react) / b if react and b else None)


def is_signal(st, direction):
    if not st or not st["ratio"]:
        return False
    if direction < 0:
        return st["ratio"] <= CUT and st["z"] <= -2
    return st["ratio"] >= 1.3 and st["z"] >= 2


# Archive days exist only around events, so few dates have a full event-free window. The placebo
# therefore shifts the windows back inside each event's own pre-period: pseudo-lead -14..-8 against
# base -24..-15, i.e. same airports, same season, no event inside either window.
PBASE, PLEAD = range(-24, -14), range(-14, -7)


def placebo_z(daily, have, countries, date, metric):
    st = stats(series(daily, have, countries, date, metric), PBASE, PLEAD)
    return st and st["z"]


def covered(daily, have, countries, date):
    """Event countries with enough airline movements in both windows to measure anything."""
    out = []
    for c in countries:
        s = series(daily, have, [c], date, "foreign_moves")
        if stats(s):
            out.append(c)
    return out


def carrier_cuts(carriers, have, countries, date, min_base=2.0, max_ratio=0.3):
    """Foreign carriers averaging >= min_base movements/day in the baseline that fell to
    <= max_ratio of it over the lead window (-7..-1) or its last three days (-3..-1).
    Absent days count as zero on processed days. Returns (cc, prefix, base, lead7, lead3, react)."""
    def days(r):
        return [date + dt.timedelta(days=k) for k in r if date + dt.timedelta(days=k) in have]
    win = [days(range(-24, -7)), days(range(-7, 0)), days(range(-3, 0)), days(range(0, 4))]
    if len(win[0]) < 10 or len(win[1]) < 3:
        return []
    tot = defaultdict(lambda: [0, 0, 0, 0])
    for (c, pre, d), n in carriers.items():
        if c in countries:
            for i, ds in enumerate(win):
                if d in ds:
                    tot[c, pre][i] += n
    out = []
    for (c, pre), v in tot.items():
        b, l7, l3, r = (x / max(1, len(ds)) for x, ds in zip(v, win))
        if b >= min_base and (l7 <= max_ratio * b or (len(win[2]) >= 2 and l3 <= max_ratio * b)):
            out.append((c, pre, b, l7, l3, r))
    return sorted(out, key=lambda x: -x[2])


def _group(e):
    return "control" if e["category"] == "control" else (e["warning"] or "other")


def run(con):
    """One row per (event, covered country): each country is its own series, because coverage and
    carrier mix differ too much between countries to pool (UAE alone would dominate a sum)."""
    from .backtest import load_events
    events, _ = load_events()
    daily, carriers, have = load(con)
    tracked = set(AP_CC.values())
    results = []
    for e in sorted(events, key=lambda e: e["date"]):
        cs = covered(daily, have, [c for c in e["countries"] if c in tracked], e["date"])
        if not cs:
            results.append(dict(event=e, country=None, metrics={}, cuts=[], coverage=None))
        for c in cs:
            ok = series(daily, have, [c], e["date"], "foreign_moves")  # days passing the coverage floor
            ok = {e["date"] + dt.timedelta(days=k) for k in ok} | {d for d in have if d >= e["date"]}
            r = dict(event=e, country=c, metrics={}, coverage=None,
                     cuts=carrier_cuts(carriers, ok, [c], e["date"]))
            results.append(r)
            for m in METRICS:
                st = stats(series(daily, have, [c], e["date"], m))
                if st:
                    loose = series(daily, have, [c], e["date"], m, strict=False)
                    react = [loose[k] for k in range(0, 4) if k in loose]
                    st["react"] = statistics.mean(react) / st["base"] if react and st["base"] else None
                    st["signal"] = is_signal(st, METRICS[m][2])
                    st["pz"] = placebo_z(daily, have, [c], e["date"], m)
                r["metrics"][m] = st
            tot = {k: daily[c, d]["air"] for k in range(-24, 4)
                   for d in [e["date"] + dt.timedelta(days=k)] if d in have and (c, d) in daily}
            st = stats(tot)
            if st:
                lo = min(tot[k] for k in range(-24, 0) if k in tot) / st["base"]
                r["coverage"] = dict(ratio=st["ratio"], react=st["react"], min_day=lo,
                                     brk=not (0.6 <= st["ratio"] <= 1.6) or lo < 0.4)
    # Placebo p: share of shifted-window z's (all events and controls, all countries) at least as
    # extreme in the pull-back direction.
    for m, (_, _, sgn) in METRICS.items():
        pool = [r["metrics"][m]["pz"] for r in results if r["metrics"].get(m) and r["metrics"][m]["pz"] is not None]
        for r in results:
            st = r["metrics"].get(m)
            if st:
                st["p"] = (1 + sum(sgn * z >= sgn * st["z"] for z in pool)) / (1 + len(pool))
                st["npool"] = len(pool)
    return results


def _f(x, f="{:.2f}"):
    return "–" if x is None else f.format(x)


def report(con):
    results = run(con)
    L = []
    for m, (num, den, sgn) in METRICS.items():
        L += [f"\n### {m} ({'cut' if sgn < 0 else 'rise'} = airlines pulling back)\n",
              "| date | event | group | country | baseline | lead | ratio | z | placebo p | react (0..+3) | signal |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in results:
            e, st = r["event"], r["metrics"].get(m)
            fmt = "{:.3f}" if den else "{:.1f}"
            row = f"| {e['date']} | {e['title']} | {_group(e)} | {r['country'] or '–'} | "
            if not st:
                L.append(row + "no data | | | | | | |")
                continue
            flag = "**SIGNAL**" if st["signal"] else ""
            if r["coverage"] and r["coverage"]["brk"]:
                flag += " (coverage break)"
            L.append(row + f"{_f(st['base'], fmt)} | {_f(st['lead'], fmt)} | {_f(st['ratio'])} | {st['z']:+.1f} | "
                     f"{st['p']:.2f} | {_f(st['react'])} | {flag} |")
    return results, "\n".join(L)


if __name__ == "__main__":
    import sys
    _, text = report(connect(sys.argv[1] if len(sys.argv) > 1 else "data/ind_airline.db"))
    print(text)
