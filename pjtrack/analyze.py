"""Aggregate analysis: daily departure counts, anomalies, event study, destinations.

Counts only cover the tracked airports in config.AIRPORTS so that OpenSky history and
live data measure the same thing. A day counts as "covered" for a country only if every
tracked airport in that country was fetched from OpenSky, or the live tracker ran for
most of that day - uncovered days are None, never 0.
"""
import csv
import datetime as dt
import random
import statistics
from collections import Counter, defaultdict

from .config import AIRPORTS, COUNTRY_NAMES, DEST_NAMES, EVENTS_CSV, LIVE_INTERVAL_S

BASELINE_DAYS = 28
WINDOW = 7  # event study looks at days -WINDOW..+WINDOW


def country_name(cc):
    if cc in (None, "?"):
        return "Unknown"
    return COUNTRY_NAMES.get(cc) or DEST_NAMES.get(cc, cc)


def day_of(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).date()


def load_bizjet_flights(con):
    """Deduplicated business-jet legs touching tracked airports, both sources merged."""
    rows = con.execute("""
        SELECT f.source, f.icao24, f.callsign, f.dep_airport, f.arr_airport, f.dep_time, f.arr_time,
               a.type
        FROM flights f JOIN aircraft a ON a.icao24 = f.icao24
        WHERE a.is_bizjet = 1 AND f.dep_airport IS NOT NULL
        ORDER BY f.icao24, f.dep_time, f.source DESC""").fetchall()  # 'opensky' sorts after 'live' -> first
    out, last = [], {}
    for r in rows:
        prev = last.get(r["icao24"])
        if prev and prev["dep_airport"] == r["dep_airport"] and abs(prev["dep_time"] - r["dep_time"]) < 7200:
            if not prev["arr_airport"] and r["arr_airport"]:
                prev["arr_airport"] = r["arr_airport"]
            continue
        d = dict(r)
        out.append(d)
        last[r["icao24"]] = d
    return out


def airport_countries(con):
    m = {r[0]: r[1] for r in con.execute("SELECT icao, country FROM airports")}
    m.update({a: c for a, (c, _) in AIRPORTS.items()})
    return m


def blind_airports(con):
    """Tracked airports where OpenSky has (almost) no receiver coverage: fewer than 5
    departures/day of *any* aircraft on average. Every tracked airport handles far more
    than that, so their low counts are meaningless."""
    rows = con.execute("SELECT airport, SUM(n), COUNT(*) FROM fetch_log GROUP BY airport").fetchall()
    return sorted(a for a, total, days in rows if days >= 2 and total / days < 5)


def coverage(con):
    """{country: set(dates)} of days with complete data (blind airports excluded)."""
    blind = set(blind_airports(con))
    by_country = defaultdict(set)
    for a, (c, _) in AIRPORTS.items():
        if a not in blind:
            by_country[c].add(a)
    fetched = defaultdict(set)
    for ap, day in con.execute("SELECT airport, day FROM fetch_log"):
        fetched[day].add(ap)
    cov = defaultdict(set)
    for day, aps in fetched.items():
        for c, need in by_country.items():
            if need <= aps:
                cov[c].add(dt.date.fromisoformat(day))
    min_cycles = 0.7 * 86400 / LIVE_INTERVAL_S
    for k, v in con.execute("SELECT k, v FROM meta WHERE k LIKE 'live:%'"):
        if int(v) >= min_cycles:
            for c in by_country:
                cov[c].add(dt.date.fromisoformat(k[5:]))
    cov["ALL"] = set.intersection(*(cov[c] for c in by_country)) if cov else set()
    return cov


def daily_counts(flights, cov, blind=()):
    """{country: {date: count or None}} plus 'ALL', over the covered date span.

    Blind airports are left out on every day (even when live data sees them) so each
    series measures the same set of airports throughout.
    """
    counts = defaultdict(Counter)
    for f in flights:
        ap = f["dep_airport"]
        if ap in AIRPORTS and ap not in blind:
            d = day_of(f["dep_time"])
            counts[AIRPORTS[ap][0]][d] += 1
            counts["ALL"][d] += 1
    all_days = set().union(*cov.values()) if cov else set()
    if not all_days:
        return {}
    start, end = min(all_days), max(all_days)
    span = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    return {c: {d: (counts[c][d] if d in cov.get(c, ()) else None) for d in span}
            for c in list(COUNTRY_NAMES) + ["ALL"]}


def baseline(series, d):
    """Median of the previous BASELINE_DAYS covered days (skipping the week before d)."""
    vals = [series.get(d - dt.timedelta(days=i)) for i in range(WINDOW + 1, WINDOW + 1 + BASELINE_DAYS)]
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if len(vals) >= BASELINE_DAYS // 2 else None


def anomalies(series, z_min=2.5, min_delta=5):
    """Days deviating from baseline by |z| >= z_min AND by at least min_delta flights
    (the absolute floor stops low-volume countries flagging 1 -> 4 as an event)."""
    out = []
    for d, x in series.items():
        if x is None:
            continue
        b = baseline(series, d)
        if b is None:
            continue
        z = (x - b) / max(b, 1) ** 0.5  # Poisson-scaled deviation
        if abs(z) >= z_min and abs(x - b) >= min_delta:
            out.append((d, x, b, z))
    return out


def load_events():
    if not EVENTS_CSV.exists():
        return []
    events = []
    with open(EVENTS_CSV, newline="") as f:
        for r in csv.DictReader(row for row in f if not row.startswith("#")):
            if not r.get("date"):
                continue
            countries = [c.strip().upper() for c in (r.get("countries") or "ALL").split(";") if c.strip()]
            events.append(dict(date=dt.date.fromisoformat(r["date"].strip()), countries=countries,
                               category=(r.get("category") or "").strip(), title=(r.get("title") or "").strip()))
    return events


def _ratios(series, d):
    """Count/baseline for each offset in -WINDOW..+WINDOW, or None if not computable."""
    b = baseline(series, d)
    if not b:
        return None
    r = []
    for k in range(-WINDOW, WINDOW + 1):
        x = series.get(d + dt.timedelta(days=k))
        r.append(None if x is None else x / b)
    return r


def event_study(counts, events, n_placebo=2000, seed=7):
    """Average departure ratio around events vs. random placebo dates.

    For each event, uses the summed series of the event's countries. The test statistic is
    the mean ratio over days -3..+1 (anticipation + reaction). The p-value is the share of
    placebo draws (same countries, random dates) with a statistic at least as large.
    """
    rng = random.Random(seed)
    results = []
    for ev in events:
        cs = ev["countries"] if ev["countries"] != ["ALL"] else ["ALL"]
        series = _sum_series(counts, cs)
        if not series:
            continue
        r = _ratios(series, ev["date"])
        stat = _stat(r)
        if stat is None:
            results.append(dict(event=ev, ratios=r, stat=None, p=None))
            continue
        dates = [d for d in series if baseline(series, d)]
        dates = [d for d in dates if abs((d - ev["date"]).days) > WINDOW]
        draws = [_stat(_ratios(series, rng.choice(dates))) for _ in range(n_placebo)] if dates else []
        draws = [s for s in draws if s is not None]
        p = (sum(s >= stat for s in draws) + 1) / (len(draws) + 1) if draws else None
        results.append(dict(event=ev, ratios=r, stat=stat, p=p))
    return results


def _sum_series(counts, countries):
    series = [counts[c] for c in countries if c in counts]
    if not series:
        return {}
    out = {}
    for d in series[0]:
        vals = [s.get(d) for s in series]
        out[d] = None if any(v is None for v in vals) else sum(vals)
    return out


def _stat(r):
    if not r:
        return None
    window = [x for x in r[WINDOW - 3: WINDOW + 2] if x is not None]
    return statistics.mean(window) if len(window) >= 3 else None


def destinations(flights, ap_country, start, end, origin_countries=None):
    """Destination-country shares for departures from tracked airports in [start, end]."""
    c = Counter()
    for f in flights:
        ap = f["dep_airport"]
        if ap not in AIRPORTS:
            continue
        if origin_countries and AIRPORTS[ap][0] not in origin_countries:
            continue
        if start <= day_of(f["dep_time"]) <= end:
            c[ap_country.get(f["arr_airport"], "?") if f["arr_airport"] else "?"] += 1
    return c


def top_routes(flights, start, end, n=25):
    c = Counter((f["dep_airport"], f["arr_airport"] or "?") for f in flights
                if f["dep_airport"] in AIRPORTS and start <= day_of(f["dep_time"]) <= end)
    return c.most_common(n)


def report(con, days=30):
    flights = load_bizjet_flights(con)
    cov = coverage(con)
    counts = daily_counts(flights, cov, set(blind_airports(con)))
    if not counts:
        print("No covered days yet. Run `backfill` and/or `live` first.")
        return
    end = max(counts["ALL"])
    start = end - dt.timedelta(days=days - 1)
    print(f"Business-jet departures from {len(AIRPORTS)} tracked airports, {start} .. {end}")
    blind = blind_airports(con)
    if blind:
        print(f"No OpenSky receiver coverage (excluded): {', '.join(blind)}")
    print()
    print(f"{'Country':<14}{'total':>7}{'per day':>9}{'covered days':>14}")
    for c, name in COUNTRY_NAMES.items():
        vals = [v for d, v in counts[c].items() if start <= d <= end and v is not None]
        if vals:
            print(f"{name:<14}{sum(vals):>7}{sum(vals) / len(vals):>9.1f}{len(vals):>14}")

    print("\nAnomalous days (|z| >= 2.5 vs. 28-day median):")
    found = False
    for c in ["ALL"] + list(COUNTRY_NAMES):
        for d, x, b, z in anomalies(counts[c]):
            if start <= d <= end:
                found = True
                print(f"  {d}  {COUNTRY_NAMES.get(c, 'All region'):<14} {x:4d} departures vs ~{b:.0f}  (z={z:+.1f})")
    if not found:
        print("  none")

    ap_country = airport_countries(con)
    dest = destinations(flights, ap_country, start, end)
    tot = sum(dest.values()) or 1
    print("\nDestination countries:")
    for cc, n in dest.most_common(12):
        print(f"  {country_name(cc):<16}{n:>6}  {100 * n / tot:5.1f}%")

    print("\nTop routes:")
    for (a, b), n in top_routes(flights, start, end, 15):
        print(f"  {a} -> {b:<6}{n:>5}")

    events = load_events()
    if events:
        print("\nEvent study (ratio = departures / baseline; stat = mean ratio days -3..+1):")
        for r in event_study(counts, events):
            ev = r["event"]
            stat = f"{r['stat']:.2f}" if r["stat"] is not None else "n/a (no data)"
            p = "" if r["p"] is None else ("p<0.001" if r["p"] < 0.001 else f"p={r['p']:.3f}")
            print(f"  {ev['date']}  {';'.join(ev['countries']):<10} {ev['title'][:40]:<40}  stat={stat} {p}")
