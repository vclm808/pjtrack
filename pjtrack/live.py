"""Live tracker: polls adsb.lol for every business-jet type worldwide and infers
departures/arrivals for flights touching the Middle East region.

adsb.lol aggregates community feeders and does not filter blocked (LADD/PIA)
aircraft, so coverage is broader than commercial trackers - but it is limited to
where feeders exist (patchy over Iran, Iraq, Syria, Yemen).
"""
import math
import time

from .config import BIZJET_TYPES, LIVE_INTERVAL_S, LIVE_POINTS, LIVE_SWEEP_S, REGION_BBOX
from .db import get_meta, set_meta
from .net import HTTPError, get_json

POINT_API = "https://api.adsb.lol/v2/point/{}/{}/250"
TYPE_API = "https://api.adsb.lol/v2/type/{}"
GROUND_RADIUS_KM = 6       # on the ground within this distance -> at that airport
CLIMB_RADIUS_KM = 45       # first seen climbing low within this distance -> departed there
DESCENT_RADIUS_KM = 45     # lost while descending low within this distance -> arrived there
LOST_LOW_S = 1800          # lost below 8000 ft: close the leg after this long
LOST_CRUISE_S = 14 * 3600  # lost at cruise (likely left coverage): wait for a global sweep to find it


def in_region(lat, lon):
    a, b, c, d = REGION_BBOX
    return lat is not None and a <= lat <= b and c <= lon <= d


def haversine_km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    h = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(h))


class AirportIndex:
    """1-degree grid for nearest-airport lookups."""

    def __init__(self, con):
        self.grid, self.pos = {}, {}
        for r in con.execute("SELECT icao, lat, lon, kind FROM airports"):
            self.pos[r[0]] = (r[1], r[2])
            self.grid.setdefault((int(r[1] // 1), int(r[2] // 1)), []).append((r[0], r[1], r[2], r[3]))
        if not self.pos:
            raise SystemExit("Airports table empty - run `python3 -m pjtrack refdata` first.")

    def nearest(self, lat, lon, max_km):
        best, best_d = None, max_km
        gy, gx = int(lat // 1), int(lon // 1)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for icao, alat, alon, kind in self.grid.get((gy + dy, gx + dx), ()):
                    d = haversine_km(lat, lon, alat, alon)
                    # Prefer real airports over small strips when nearly tied.
                    if kind == "small_airport":
                        d += 3
                    if d < best_d:
                        best, best_d = icao, d
        return best

    def in_region(self, icao):
        return icao in self.pos and in_region(*self.pos[icao])


def _fetch(urls, is_bizjet, log, pause):
    """GET each url; keep business jets with a position. Backs off on HTTP 429."""
    seen, now, backoff = {}, time.time(), pause
    for url in urls:
        for _ in range(3):
            try:
                data, _ = get_json(url, timeout=30)
                break
            except HTTPError as e:
                if e.status != 429:
                    log(f"  {url}: HTTP {e.status}")
                    data = None
                    break
                backoff = min(backoff * 2, 120)
                time.sleep(backoff)
            except Exception as e:  # network hiccup - skip this url this cycle
                log(f"  {url}: {e}")
                data = None
                break
        else:
            log(f"  {url}: rate-limited, skipped")
            data = None
        if data:
            now = data.get("now", now * 1000) / 1000
            for ac in data.get("ac") or []:
                hx = ac.get("hex", "").lower()
                if ac.get("lat") is not None and (ac.get("t") in BIZJET_TYPES or hx in is_bizjet):
                    ac["_ts"] = int(now - float(ac.get("seen_pos") or ac.get("seen") or 0))
                    seen[hx] = ac
        time.sleep(pause)
    return seen, int(now)


def fetch_region(is_bizjet, log):
    return _fetch([POINT_API.format(lat, lon) for lat, lon in LIVE_POINTS], is_bizjet, log, pause=1.5)


def fetch_global(is_bizjet, log):
    return _fetch([TYPE_API.format(t) for t in sorted(BIZJET_TYPES)], is_bizjet, log, pause=3)


def _alt(ac):
    a = ac.get("alt_baro")
    if a == "ground":
        return 0, True
    a = a if isinstance(a, (int, float)) else ac.get("alt_geom")
    return (int(a) if a is not None else None), False


def _save_flight(con, idx, icao24, callsign, dep, dep_time, arr, arr_time):
    if not (idx.in_region(dep) or idx.in_region(arr)):
        return
    con.execute("INSERT OR REPLACE INTO flights VALUES ('live',?,?,?,?,?,?)",
                (icao24, callsign, dep, arr, dep_time, arr_time))


def process(con, idx, seen, now):
    state = {r["icao24"]: dict(r) for r in con.execute("SELECT * FROM live_state")}
    started = closed = 0

    for hx, ac in seen.items():
        lat, lon, ts = ac["lat"], ac["lon"], ac["_ts"]
        alt, ground_flag = _alt(ac)
        rate = ac.get("baro_rate") or ac.get("geom_rate") or 0
        gs = ac.get("gs") or 0
        cs = (ac.get("flight") or "").strip() or None
        on_ground = ground_flag or (alt is not None and alt < 1000 and gs < 50)
        s = state.get(hx)

        if on_ground:
            if s and s["dep_time"] and not s["on_ground"]:
                arr = idx.nearest(lat, lon, GROUND_RADIUS_KM * 2)
                _save_flight(con, idx, hx, s["callsign"] or cs, s["dep_airport"], s["dep_time"], arr, ts)
                closed += 1
            state[hx] = dict(icao24=hx, callsign=cs, dep_airport=idx.nearest(lat, lon, GROUND_RADIUS_KM),
                             dep_time=None, last_seen=ts, last_lat=lat, last_lon=lon,
                             last_alt=0, last_rate=0, on_ground=1)
            continue

        if s and s["dep_time"]:  # flight in progress
            s.update(last_seen=ts, last_lat=lat, last_lon=lon, last_alt=alt, last_rate=rate,
                     callsign=s["callsign"] or cs)
            continue

        # Airborne with no open flight: start one.
        dep, dep_time = None, ts
        if s and s["on_ground"] and s["dep_airport"] and ts - s["last_seen"] < 3 * 3600:
            dep, dep_time = s["dep_airport"], s["last_seen"]
        elif alt is not None and alt < 10000 and rate > 300:
            dep = idx.nearest(lat, lon, CLIMB_RADIUS_KM)
        state[hx] = dict(icao24=hx, callsign=cs, dep_airport=dep, dep_time=dep_time, last_seen=ts,
                         last_lat=lat, last_lon=lon, last_alt=alt, last_rate=rate, on_ground=0)
        if dep:
            _save_flight(con, idx, hx, cs, dep, dep_time, None, None)
            started += idx.in_region(dep)

    # Close flights whose aircraft vanished.
    for hx, s in list(state.items()):
        if hx in seen:
            continue
        low = s["last_alt"] is not None and s["last_alt"] < 8000 and (s["last_rate"] or 0) <= 0
        if now - s["last_seen"] < (LOST_LOW_S if low or s["on_ground"] else LOST_CRUISE_S):
            continue
        if s["dep_time"] and not s["on_ground"]:
            arr = idx.nearest(s["last_lat"], s["last_lon"], DESCENT_RADIUS_KM) if low else None
            if s["dep_airport"] or arr:
                _save_flight(con, idx, hx, s["callsign"], s["dep_airport"], s["dep_time"], arr, s["last_seen"])
                closed += 1
        if now - s["last_seen"] > 12 * 3600:
            del state[hx]
        elif s["dep_time"]:
            s["dep_time"], s["dep_airport"] = None, None

    con.execute("DELETE FROM live_state")
    con.executemany(
        "INSERT INTO live_state VALUES (:icao24,:callsign,:dep_airport,:dep_time,:last_seen,"
        ":last_lat,:last_lon,:last_alt,:last_rate,:on_ground)", state.values())
    con.commit()
    return started, closed


def run(con, once=False, interval=LIVE_INTERVAL_S, log=print):
    idx = AirportIndex(con)
    is_bizjet = {r[0] for r in con.execute("SELECT icao24 FROM aircraft WHERE is_bizjet=1")}
    last_sweep = 0
    while True:
        t0 = time.time()
        seen, now = fetch_region(is_bizjet, log)
        if time.time() - last_sweep >= LIVE_SWEEP_S and not once:
            extra, _ = fetch_global(is_bizjet, log)
            extra.update(seen)  # regional data is fresher
            seen, last_sweep = extra, time.time()
        region = sum(in_region(a["lat"], a["lon"]) for a in seen.values())
        started, closed = process(con, idx, seen, now)
        if seen:  # count cycles per UTC day so analysis knows which days live data covers
            key = "live:" + time.strftime("%Y-%m-%d", time.gmtime(now))
            set_meta(con, key, int(get_meta(con, key, 0)) + 1)
            con.commit()
        log(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  bizjets seen: {len(seen)}, "
            f"in region: {region}, new regional departures: {started}, legs closed: {closed}")
        if once:
            return
        time.sleep(max(30, interval - (time.time() - t0)))
