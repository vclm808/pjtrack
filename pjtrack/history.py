"""adsb.lol historical archives (github.com/adsblol/globe_history_YYYY, ODbL).

Each UTC day is a tar (split into ~2 GB parts) of per-aircraft readsb trace files.
We stream it - nothing is written to disk - and:
  * for business jets: extract every leg (departure/arrival airport + times) into `flights`
    with source='adsblol';
  * for all other aircraft: only count departures from tracked airports, stored in
    `hist_log` as the denominator for the business-jet share (coverage control).
"""
import gzip
import io
import json
import re
import tarfile
import time
import urllib.request
import zlib

from .config import AIRPORTS
from .live import AirportIndex
from .net import UA, check_scope, get_json

RELEASE_API = "https://api.github.com/repos/adsblol/globe_history_{y}/releases/tags/v{y}.{m:02d}.{d:02d}-planes-readsb-prod-0{sfx}"
NAME_RE = re.compile(r"trace_full_([0-9a-f]{6})\.json$")
LEG_GAP_S = 1800          # gap that starts a new leg
DEP_RADIUS_KM = 12        # leg starting on the ground / low within this radius -> departed there
LOW_ALT_FT = 3000



class _Chain(io.RawIOBase):
    """Concatenate several HTTP responses into one readable stream."""

    def __init__(self, urls):
        self.urls, self.resp, self.bytes = list(urls), None, 0

    def readable(self):
        return True

    def readinto(self, b):
        while True:
            if self.resp is None:
                if not self.urls:
                    return 0
                url = self.urls.pop(0)
                check_scope(url)
                self.resp = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}),
                                                   timeout=120)
            n = self.resp.readinto(b)
            if n:
                self.bytes += n
                return n
            self.resp.close()
            self.resp = None


def asset_urls(day):
    # Some days (e.g. 2025-05-28..06-10) were only published under a "-prod-0tmp" tag.
    for sfx in ("", "tmp"):
        data, _ = get_json(RELEASE_API.format(y=day.year, m=day.month, d=day.day, sfx=sfx))
        if data and data.get("assets"):
            return [a["browser_download_url"] for a in sorted(data["assets"], key=lambda a: a["name"])
                    if ".tar" in a["name"]]
    return []


def _load(raw):
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def _legs(doc):
    """Yield (start_ts, end_ts, first_point, last_point) per leg; point = (lat, lon, alt, ground)."""
    base = doc.get("timestamp", 0)
    leg, last_t = [], None
    for p in doc.get("trace") or ():
        t = base + p[0]
        flags = p[6] if len(p) > 6 and isinstance(p[6], int) else 0
        if leg and ((flags & 2) or t - last_t > LEG_GAP_S):
            yield leg
            leg = []
        alt = p[3]
        leg.append((t, p[1], p[2], 0 if alt == "ground" else alt, alt == "ground"))
        last_t = t
    if leg:
        yield leg


def _dep_airport(idx, pt):
    _, lat, lon, alt, ground = pt
    if ground or (isinstance(alt, (int, float)) and alt < LOW_ALT_FT):
        return idx.nearest(lat, lon, DEP_RADIUS_KM)
    return None


def process_day(con, day, bizjets, idx, log=print):
    if con.execute("SELECT 1 FROM hist_days WHERE source='adsblol' AND day=?", (day.isoformat(),)).fetchone():
        return "done"
    urls = asset_urls(day)
    if not urls:
        log(f"  {day}: no archive")
        return "missing"
    t0 = time.time()
    stream = _Chain(urls)
    counts = {a: 0 for a in AIRPORTS}
    rows = []
    tracked = set(AIRPORTS)
    with tarfile.open(fileobj=io.BufferedReader(stream, 1 << 20), mode="r|") as tar:
        for m in tar:
            mm = NAME_RE.search(m.name)
            if not mm or not m.isfile():
                continue
            hx = mm.group(1)
            f = tar.extractfile(m)
            try:
                doc = _load(f.read())
            except (ValueError, OSError, zlib.error):
                continue
            is_biz = hx in bizjets
            for leg in _legs(doc):
                if not any(not p[4] and isinstance(p[3], (int, float)) and p[3] > 1500 for p in leg):
                    continue  # taxiing / ground-only segment, not a flight
                dep = _dep_airport(idx, leg[0])
                if dep in tracked:
                    counts[dep] += 1
                if not is_biz:
                    continue
                arr = _dep_airport(idx, leg[-1])
                if dep or arr:
                    rows.append(("adsblol", hx, None, dep, arr, int(leg[0][0]), int(leg[-1][0])))
    con.executemany("INSERT OR REPLACE INTO flights VALUES (?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT OR REPLACE INTO hist_log VALUES ('adsblol',?,?,?)",
                    [(a, day.isoformat(), n) for a, n in counts.items()])
    secs = time.time() - t0
    con.execute("INSERT OR REPLACE INTO hist_days VALUES ('adsblol',?,?,?)", (day.isoformat(), secs, stream.bytes))
    con.commit()
    log(f"  {day}: {len(rows)} bizjet legs, {sum(counts.values())} departures at tracked airports, "
        f"{stream.bytes / 1e9:.2f} GB in {secs:.0f}s ({stream.bytes / 1e6 / max(secs, 1):.0f} MB/s)")
    return "ok"


def run(con, days, log=print):
    idx = AirportIndex(con)
    bizjets = {r[0] for r in con.execute("SELECT icao24 FROM aircraft WHERE is_bizjet=1")}
    for d in days:
        try:
            process_day(con, d, bizjets, idx, log)
        except Exception as e:  # keep going; a failed day is retried on the next run
            log(f"  {d}: failed ({e})")


def _worker(day):
    from .db import connect
    con = connect()
    idx = AirportIndex(con)
    bizjets = {r[0] for r in con.execute("SELECT icao24 FROM aircraft WHERE is_bizjet=1")}
    for attempt in range(2):
        try:
            return day, process_day(con, day, bizjets, idx, log=lambda m: print(m, flush=True))
        except Exception as e:
            err = e
    print(f"  {day}: failed ({err})", flush=True)
    return day, "failed"


def run_parallel(days, workers=3):
    """Process archive days in parallel processes (each streams its own download)."""
    from concurrent.futures import ProcessPoolExecutor
    from .db import connect
    con = connect()
    done = {r[0] for r in con.execute("SELECT day FROM hist_days WHERE source='adsblol'")}
    todo = [d for d in days if d.isoformat() not in done]
    print(f"{len(todo)} archive days to process ({len(days) - len(todo)} already done), {workers} workers", flush=True)
    with ProcessPoolExecutor(workers) as ex:
        results = list(ex.map(_worker, todo))
    failed = [d for d, r in results if r == "failed"]
    if failed:
        print(f"{len(failed)} days failed; rerun to retry: {', '.join(map(str, failed))}")
