"""OpenSky Network historical departures, per airport per UTC day."""
import datetime as dt
import os
import time

from .config import AIRPORTS, OPENSKY_DAILY_BUDGET
from .db import get_meta, set_meta
from .net import HTTPError, get_json, load_env, request

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
API = "https://opensky-network.org/api"
RESERVE = 100  # credits never spent


class OpenSky:
    def __init__(self):
        load_env()
        self.cid = os.environ.get("OPENSKY_CLIENT_ID")
        self.secret = os.environ.get("OPENSKY_CLIENT_SECRET")
        if not (self.cid and self.secret):
            raise SystemExit("OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET missing from .env")
        self.token, self.expires = None, 0
        self.remaining = None

    def _auth(self):
        if time.time() < self.expires - 60:
            return
        import json
        _, _, body = request(TOKEN_URL, data={"grant_type": "client_credentials",
                                              "client_id": self.cid, "client_secret": self.secret})
        tok = json.loads(body)
        self.token, self.expires = tok["access_token"], time.time() + tok.get("expires_in", 1800)

    def departures(self, airport, day, ndays=1):
        """All departures from `airport` on UTC dates day..day+ndays-1 (max 2).

        A 2-day window spans 2 day-partitions and costs the same 30 credits as 1 day.
        """
        self._auth()
        begin = int(dt.datetime.combine(day, dt.time(), dt.timezone.utc).timestamp())
        url = f"{API}/flights/departure?airport={airport}&begin={begin}&end={begin + ndays * 86400 - 1}"
        try:
            data, headers = get_json(url, headers={"Authorization": f"Bearer {self.token}"}, timeout=120)
        except HTTPError as e:
            if e.status == 401:
                self.expires = 0
                return self.departures(airport, day, ndays)
            if e.status == 429:
                wait = e.headers.get("X-Rate-Limit-Retry-After-Seconds", "?")
                raise QuotaExhausted(f"OpenSky credits exhausted; retry after {wait}s") from None
            raise
        rem = headers.get("X-Rate-Limit-Remaining") or headers.get("x-rate-limit-remaining")
        if rem is not None:
            self.remaining = int(rem)
        return data or []


class QuotaExhausted(Exception):
    pass


def store_departures(con, airport, days, flights):
    rows = [("opensky", f["icao24"].lower(), (f.get("callsign") or "").strip() or None,
             f.get("estDepartureAirport") or airport, f.get("estArrivalAirport"),
             f["firstSeen"], f.get("lastSeen")) for f in flights]
    con.executemany("INSERT OR REPLACE INTO flights VALUES (?,?,?,?,?,?,?)", rows)
    for day in days:
        n = sum(dt.datetime.fromtimestamp(r[5], dt.timezone.utc).date() == day for r in rows)
        con.execute("INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?)",
                    (airport, day.isoformat(), n, int(time.time())))
    con.commit()


def _spent_today(con):
    today = dt.date.today().isoformat()
    if get_meta(con, "opensky_budget_day") != today:
        set_meta(con, "opensky_budget_day", today)
        set_meta(con, "opensky_spent", 0)
    return int(get_meta(con, "opensky_spent", 0))


def plan_requests(con, wanted):
    """Turn an ordered list of (airport, date) into OpenSky requests (first_day, ndays, airport),
    skipping days already fetched and pairing consecutive days into 2-day windows."""
    done = {(r[0], r[1]) for r in con.execute("SELECT airport, day FROM fetch_log")}
    missing = [(a, d) for a, d in dict.fromkeys(wanted) if (a, d.isoformat()) not in done]
    pending, todo = set(missing), []
    for a, d in missing:
        if (a, d) not in pending:
            continue
        pending.discard((a, d))
        nxt = d + dt.timedelta(days=1)
        if (a, nxt) in pending:
            pending.discard((a, nxt))
            todo.append((d, 2, a))
        else:
            todo.append((d, 1, a))
    return todo


def fetch_requests(con, todo, budget=None, log=print):
    """Execute planned requests until done or the daily credit budget is reached."""
    budget = budget or OPENSKY_DAILY_BUDGET
    ndays_total = sum(t[1] for t in todo)
    log(f"{ndays_total} airport-days missing in {len(todo)} requests (~{len(todo) * 30} credits)")
    if not todo:
        return 0, 0
    os_ = OpenSky()
    fetched = 0
    for day, ndays, ap in todo:
        spent = _spent_today(con)
        if spent + 30 > budget:
            log(f"Daily budget of {budget} credits reached; run again tomorrow.")
            break
        try:
            flights = os_.departures(ap, day, ndays)
        except QuotaExhausted as e:
            log(str(e))
            break
        span = [day + dt.timedelta(days=i) for i in range(ndays)]
        store_departures(con, ap, span, flights)
        set_meta(con, "opensky_spent", spent + 30)
        con.commit()
        fetched += 1
        label = f"{day}" + (f"..{span[-1]}" if ndays > 1 else " " * 12)
        log(f"  {label} {ap}: {len(flights):4d} departures  (credits left: {os_.remaining})")
        if os_.remaining is not None and os_.remaining < RESERVE + 30:
            log("OpenSky reports credits nearly exhausted; stopping.")
            break
        time.sleep(1)
    left = len(todo) - fetched
    log(f"Made {fetched} requests; {left} still to go.")
    return fetched, left


def backfill(con, days=30, airports=None, budget=None, log=print):
    """Fetch recent missing airport-days, newest first, tier-1 airports first.

    The most recent complete day is today-2 (OpenSky finalises flights in a nightly batch).
    """
    airports = airports or sorted(AIRPORTS, key=lambda a: (AIRPORTS[a][1], a))
    last = dt.date.today() - dt.timedelta(days=2)
    # Oldest-first within each 2-day block so plan_requests pairs them.
    wanted = [(a, last - dt.timedelta(days=k + j)) for k in range(0, days, 2) for a in airports
              for j in (1, 0) if k + j < days]
    return fetch_requests(con, plan_requests(con, wanted), budget, log)
