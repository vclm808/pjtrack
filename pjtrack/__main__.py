"""pjtrack CLI.  Usage: python3 -m pjtrack <command> [options]"""
import argparse

from . import analyze, backtest, dashboard, history, live, opensky, refdata
from .db import connect


def main():
    p = argparse.ArgumentParser(prog="pjtrack", description="Middle East private-jet departure tracker")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("refdata", help="download aircraft type DB and airports (run first, refresh monthly)")
    b = sub.add_parser("backfill", help="fetch historical departures from OpenSky (resumable, budgeted)")
    b.add_argument("--days", type=int, default=30, help="how many days back (default 30)")
    b.add_argument("--airports", nargs="*", help="limit to these ICAO codes")
    b.add_argument("--budget", type=int, help="max credits to spend today (default from config)")
    l = sub.add_parser("live", help="run the live adsb.lol tracker loop")
    l.add_argument("--once", action="store_true", help="single polling cycle")
    l.add_argument("--interval", type=int, default=600)
    r = sub.add_parser("report", help="print summary, anomalies, destinations, event study")
    r.add_argument("--days", type=int, default=30)
    d = sub.add_parser("dashboard", help="write data/dashboard.html")
    d.add_argument("--days", type=int, default=90)
    sub.add_parser("status", help="show what data is stored")
    bt = sub.add_parser("backtest", help="test whether departures rose before past events (backtest_events.csv)")
    bt.add_argument("--fetch", action="store_true", help="fetch missing history first (budgeted)")
    bt.add_argument("--priority", type=int, default=9, help="only fetch events up to this priority")
    bt.add_argument("--budget", type=int)
    bt.add_argument("--plan", action="store_true", help="only show how many credits are still needed")
    bt.add_argument("--source", choices=["auto", "opensky", "adsblol"], default="auto")
    bt.add_argument("--archive", action="store_true", help="download missing days from the adsb.lol archive first")
    bt.add_argument("--workers", type=int, default=3, help="parallel archive downloads")
    a = p.parse_args()

    con = connect()
    if a.cmd == "refdata":
        n, biz = refdata.load_aircraft(con)
        print(f"aircraft: {n:,} ({biz:,} business jets)")
        print(f"airports: {refdata.load_airports(con):,}")
    elif a.cmd == "backfill":
        opensky.backfill(con, days=a.days, airports=a.airports, budget=a.budget)
    elif a.cmd == "live":
        live.run(con, once=a.once, interval=a.interval)
    elif a.cmd == "report":
        analyze.report(con, days=a.days)
    elif a.cmd == "dashboard":
        print(dashboard.build(con, days=a.days))
    elif a.cmd == "backtest":
        if a.plan:
            for p in range(1, 4):
                todo = backtest.plan(con, p, a.source)
                print(f"priority <= {p}: {len(todo)} requests (~{len(todo) * 30:,} credits, "
                      f"~{len(todo) * 30 / 3600:.1f} days of quota)")
            return
        if a.fetch:
            backtest.fetch(con, a.priority, a.budget, a.source)
            print()
        if a.archive:
            history.run_parallel(backtest.archive_days(a.priority), a.workers)
            print()
        backtest.report(con, a.source)
        print("\n" + str(backtest.html_report(con, a.source)))
    elif a.cmd == "status":
        q = lambda s: con.execute(s).fetchone()[0]
        print(f"aircraft rows:        {q('SELECT COUNT(*) FROM aircraft'):,}")
        for src in ("opensky", "live"):
            n = con.execute("SELECT COUNT(*) FROM flights WHERE source=?", (src,)).fetchone()[0]
            print(f"flights ({src}):{' ' * (13 - len(src))}{n:,}")
        print(f"bizjet legs merged:   {len(analyze.load_bizjet_flights(con)):,}")
        print(f"airport-days fetched: {q('SELECT COUNT(*) FROM fetch_log'):,}")
        lo, hi = con.execute("SELECT MIN(day), MAX(day) FROM fetch_log").fetchone()
        print(f"opensky day range:    {lo} .. {hi}")
        print(f"live aircraft state:  {q('SELECT COUNT(*) FROM live_state'):,}")


if __name__ == "__main__":
    main()
