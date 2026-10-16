"""Backtest: would business-jet departures have flagged past Middle East events in advance?

For every event in backtest_events.csv, per country:
  baseline = days -24..-8, lead = days -7..-1 (the "prediction" window), reaction = days 0..+3.
A "signal" means lead-window departures were >= 1.3x baseline with z >= 2, where z uses the
baseline's own day-to-day variance (floored at Poisson). The same rule is applied to control
windows (no event) to estimate how often it fires by chance.

Business jets are also measured as a share of ALL departures at the same airports, which
controls for receiver-coverage changes and for airspace closures that ground everyone.
"""
import csv
import datetime as dt
import json
import statistics
from collections import Counter, defaultdict

from . import opensky
from .analyze import airport_countries, day_of
from .config import COUNTRY_NAMES, DATA, ROOT

EVENTS_FILE = ROOT / "backtest_events.csv"
PRE, POST = 24, 7
BASE = range(-24, -7)    # -24..-8
LEAD = range(-7, 0)      # -7..-1
REACT = range(0, 4)      # 0..+3
SIGNAL_RATIO, SIGNAL_Z = 1.3, 2.0

# Airports with usable historical coverage, by country, per data source.
OPENSKY_AIRPORTS = {
    "AE": ["OMDW", "OMAD", "OMDB"], "QA": ["OTBD", "OTHH"], "BH": ["OBBI"],
    "IL": ["LLBG"], "JO": ["OJAI"], "LB": ["OLBA"], "CY": ["LCLK"],
}
ARCHIVE_AIRPORTS = {"AE": ["OMDB", "OMDW", "OMAD", "OMAA", "OMSJ"], "IL": ["LLBG"], "CY": ["LCLK"]}
# "auto": each country from the source that sees it (adsb.lol archive has no Qatar/Bahrain/
# Jordan/Lebanon receivers; OpenSky is quota-limited, so it only covers those).
MODES = {
    "opensky": {c: ("opensky", aps) for c, aps in OPENSKY_AIRPORTS.items()},
    "adsblol": {c: ("adsblol", aps) for c, aps in ARCHIVE_AIRPORTS.items()},
    "auto": {**{c: ("opensky", OPENSKY_AIRPORTS[c]) for c in ("QA", "BH", "JO", "LB")},
             **{c: ("adsblol", aps) for c, aps in ARCHIVE_AIRPORTS.items()}},
}
REGION = set(COUNTRY_NAMES)


def load_events():
    events, holidays = [], []
    with open(EVENTS_FILE, newline="") as f:
        for r in csv.DictReader(l for l in f if not l.startswith("#")):
            e = dict(date=dt.date.fromisoformat(r["date"]), countries=r["countries"].split(";"),
                     category=r["category"], warning=r["warning"], title=r["title"],
                     priority=int(r["priority"] or 9))
            (holidays if e["category"] == "holiday" else events).append(e)
    return events, holidays


def plan(con, max_priority=9, mode="auto"):
    cmap = {c: aps for c, (src, aps) in MODES[mode].items() if src == "opensky"}
    events, _ = load_events()
    wanted = []
    for e in sorted(events, key=lambda e: (e["priority"], e["date"])):
        if e["priority"] > max_priority:
            continue
        for c in e["countries"]:
            for ap in cmap.get(c, []):
                wanted += [(ap, e["date"] + dt.timedelta(days=k)) for k in range(-PRE, POST + 1)]
    return opensky.plan_requests(con, wanted)


def fetch(con, max_priority=9, budget=None, mode="auto", log=print):
    return opensky.fetch_requests(con, plan(con, max_priority, mode), budget, log)


def _load(con):
    """fetched[src][airport][date] = all departures; biz[src][(airport, date)] = bizjet departures;
    legs[src] = bizjet legs. Each country's series uses a single source throughout."""
    fetched = {"opensky": defaultdict(dict), "adsblol": defaultdict(dict)}
    for ap, day, n in con.execute("SELECT airport, day, n FROM fetch_log"):
        fetched["opensky"][ap][dt.date.fromisoformat(day)] = n
    for ap, day, n in con.execute("SELECT airport, day, n_all FROM hist_log WHERE source='adsblol'"):
        fetched["adsblol"][ap][dt.date.fromisoformat(day)] = n
    biz = {"opensky": Counter(), "adsblol": Counter()}
    legs = {"opensky": [], "adsblol": []}
    for src, ap, arr, ts in con.execute("""SELECT f.source, f.dep_airport, f.arr_airport, f.dep_time
            FROM flights f JOIN aircraft a USING (icao24)
            WHERE a.is_bizjet = 1 AND f.source IN ('opensky', 'adsblol') AND f.dep_airport IS NOT NULL"""):
        biz[src][(ap, day_of(ts))] += 1
        legs[src].append(dict(dep_airport=ap, arr_airport=arr, dep_time=ts))
    return fetched, biz, legs


# Download order: one event at a time (newest first within a priority tier); within an event the
# days that decide the answer come first: lead window, reaction, near baseline, then the rest.
ARCHIVE_ORDER = [LEAD, REACT, range(-17, -7), range(-24, -17), range(4, POST + 1)]


def archive_days(max_priority=9):
    events, _ = load_events()
    days = []
    for e in sorted((e for e in events if e["priority"] <= max_priority),
                    key=lambda e: (e["priority"], -e["date"].toordinal())):
        core = [e["date"] + dt.timedelta(days=k) for offsets in ARCHIVE_ORDER[:3] for k in offsets]
        days += core
    for e in sorted((e for e in events if e["priority"] <= max_priority), key=lambda e: e["priority"]):
        days += [e["date"] + dt.timedelta(days=k) for offsets in ARCHIVE_ORDER[3:] for k in offsets]
    return list(dict.fromkeys(days))


def _series(data, cmap, countries, date):
    """Per offset: (bizjet count, all departures) summed over countries, or None if incomplete."""
    fetched, biz, _ = data
    pairs = [(cmap[c][0], ap) for c in countries if c in cmap for ap in cmap[c][1]]
    out = {}
    for k in range(-PRE, POST + 1):
        d = date + dt.timedelta(days=k)
        if not pairs or any(d not in fetched[src][ap] for src, ap in pairs):
            out[k] = None
            continue
        out[k] = (sum(biz[src][(ap, d)] for src, ap in pairs), sum(fetched[src][ap][d] for src, ap in pairs))
    return out


def _stats(series, ndays_airports):
    base = [series[k] for k in BASE if series.get(k)]
    lead = [series[k] for k in LEAD if series.get(k)]
    react = [series[k] for k in REACT if series.get(k)]
    if len(base) < 10 or len(lead) < 5:
        return None
    bb = [b for b, _ in base]
    b_mean = statistics.mean(bb)
    tot_mean = statistics.mean(t for _, t in base)
    if tot_mean < 5 * ndays_airports:  # receivers barely saw these airports in this period
        return dict(no_coverage=True)
    var = max(statistics.variance(bb), b_mean, 1)
    l_mean = statistics.mean(b for b, _ in lead)
    z = (l_mean - b_mean) / (var / len(lead) + var / len(base)) ** 0.5
    share = lambda xs: sum(b for b, _ in xs) / max(1, sum(t for _, t in xs))
    r = dict(base=b_mean, lead=l_mean, ratio=l_mean / b_mean if b_mean else None, z=z,
             share_base=share(base), share_lead=share(lead),
             react=statistics.mean(b for b, _ in react) / b_mean if react and b_mean else None,
             react_total=statistics.mean(t for _, t in react) / tot_mean if react else None)
    r["share_ratio"] = r["share_lead"] / r["share_base"] if r["share_base"] else None
    r["signal"] = bool(r["ratio"] and r["ratio"] >= SIGNAL_RATIO and z >= SIGNAL_Z)
    return r


def _destinations(data, cmap, ap_country, countries, date, offsets):
    days = {date + dt.timedelta(days=k) for k in offsets}
    c = Counter()
    for country in countries:
        src, aps = cmap[country]
        for f in data[2][src]:
            if f["dep_airport"] in aps and day_of(f["dep_time"]) in days:
                c[ap_country.get(f["arr_airport"]) if f["arr_airport"] else None] += 1
    known = sum(n for k, n in c.items() if k)
    out = sum(n for k, n in c.items() if k and k not in REGION)
    return c, (out / known if known else None)


def run(con, mode="auto"):
    cmap = MODES[mode]
    events, holidays = load_events()
    data = _load(con)
    ap_country = airport_countries(con)
    results = []
    for e in sorted(events, key=lambda e: e["date"]):
        countries = [c for c in e["countries"] if c in cmap]
        per_country = {}
        for c in countries:
            s = _series(data, cmap, [c], e["date"])
            per_country[c] = dict(stats=_stats(s, len(cmap[c][1])), series=s, source=cmap[c][0])
        # Combined series: only countries that have coverage for this event.
        covered = [c for c in countries if per_country[c]["stats"] and not per_country[c]["stats"].get("no_coverage")]
        s_all = _series(data, cmap, covered, e["date"])
        n_aps = sum(len(cmap[c][1]) for c in covered)
        _, out_base = _destinations(data, cmap, ap_country, covered, e["date"], BASE)
        dest_lead, out_lead = _destinations(data, cmap, ap_country, covered, e["date"], LEAD)
        lo, hi = e["date"] - dt.timedelta(days=PRE), e["date"] + dt.timedelta(days=POST)
        conf = [h for h in holidays if lo <= h["date"] <= hi
                and (h["countries"] == ["ALL"] or set(h["countries"]) & set(countries))]
        results.append(dict(event=e, combined=dict(stats=_stats(s_all, n_aps) if covered else None, series=s_all),
                            countries=per_country, covered=covered, out_base=out_base, out_lead=out_lead,
                            dest_lead=dest_lead, confounders=conf))
    return results


def _fmt(x, f="{:.2f}"):
    return "–" if x is None else f.format(x)


def report(con, mode="auto", log=print):
    results = run(con, mode)
    log("Sources: " + ", ".join(f"{c}={src}" for c, (src, _) in MODES[mode].items()) + "\n")
    todo = plan(con, mode=mode)
    if todo:
        log(f"Note: {len(todo)} OpenSky requests (~{len(todo) * 30} credits) still needed for full coverage "
            f"- run `backtest --fetch` daily.\n")
    hits = defaultdict(lambda: [0, 0])
    for r in results:
        e, st = r["event"], r["combined"]["stats"]
        log(f"{e['date']}  {e['title']}  [{e['category']}, {e['warning'] or '-'}]")
        if not st:
            log("    not enough data yet\n")
            continue
        if st.get("no_coverage"):
            log("    no receiver coverage in this period\n")
            continue
        flag = "SIGNAL" if st["signal"] else "no signal"
        log(f"    lead (-7..-1): {st['lead']:.1f}/day vs baseline {st['base']:.1f}  ratio {_fmt(st['ratio'])}  "
            f"z {st['z']:+.1f}  -> {flag}")
        log(f"    bizjet share of all departures: {st['share_base']:.1%} -> {st['share_lead']:.1%}   "
            f"reaction (0..+3): bizjets x{_fmt(st['react'])}, all traffic x{_fmt(st['react_total'])}")
        if r["out_lead"] is not None:
            log(f"    flying out of region: {_fmt(r['out_base'], '{:.0%}')} baseline -> {r['out_lead']:.0%} in lead window")
        parts = []
        for c, v in r["countries"].items():
            s = v["stats"]
            if s and not s.get("no_coverage"):
                parts.append(f"{c} x{_fmt(s['ratio'])}{'*' if s['signal'] else ''}")
            elif s:
                parts.append(f"{c} n/c")
        if parts:
            log("    by country: " + ", ".join(parts) + "   (* = signal)")
        if r["confounders"]:
            log("    confounders in window: " + "; ".join(f"{h['title']} ({h['date']})" for h in r["confounders"]))
        log("")
        key = "control" if e["category"] == "control" else (e["warning"] or "other")
        hits[key][0] += st["signal"]
        hits[key][1] += 1
    log("Summary (events with data):")
    for k in ("telegraphed", "surprise", "control"):
        if hits[k][1]:
            log(f"  {k:<12} signal in {hits[k][0]}/{hits[k][1]}")
    return results


def html_report(con, mode="auto"):
    results = run(con, mode)
    rows, charts = [], []
    for i, r in enumerate(results):
        e, st = r["event"], r["combined"]["stats"]
        ok = st and not st.get("no_coverage")
        verdict = ("Signal" if st["signal"] else "No signal") if ok else ("No coverage" if st else "Awaiting data")
        rows.append(dict(date=e["date"].isoformat(), title=e["title"], cat=e["category"], warn=e["warning"],
                         verdict=verdict, ratio=st["ratio"] if ok else None, z=st["z"] if ok else None,
                         share=[st["share_base"], st["share_lead"]] if ok else None,
                         out=[r["out_base"], r["out_lead"]],
                         conf=[f"{h['title']} ({h['date']})" for h in r["confounders"]]))
        base = st["base"] if ok else None
        charts.append(dict(title=f"{e['date']} · {e['title']}", base=base,
                           pts=[[k, v[0] if v else None, v[1] if v else None] for k, v in sorted(r["combined"]["series"].items())]))
    out = DATA / f"backtest_{mode}.html"
    out.write_text(PAGE.replace("/*DATA*/null", json.dumps(dict(rows=rows, charts=charts, ratio=SIGNAL_RATIO, z=SIGNAL_Z))))
    return out


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jet Departure Backtest</title><style>
:root{color-scheme:light;--bg:#f6f6f4;--surface:#fcfcfb;--border:#e4e3df;--grid:#ecebe7;--text:#0b0b0b;--text2:#52514e;--muted:#8a8984;
 --s1:#2a78d6;--lead:#eb683422;--ev:#6250d6;--good:#008300;--bad:#e34948}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#111110;--surface:#1a1a19;--border:#2c2c2a;--grid:#262624;
 --text:#f0efec;--text2:#c3c2b7;--muted:#8d8c86;--s1:#3987e5;--lead:#d9592633;--ev:#9085e9;--good:#2fa82f;--bad:#e66767}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#111110;--surface:#1a1a19;--border:#2c2c2a;--grid:#262624;--text:#f0efec;--text2:#c3c2b7;
 --muted:#8d8c86;--s1:#3987e5;--lead:#d9592633;--ev:#9085e9;--good:#2fa82f;--bad:#e66767}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,sans-serif}
main{max-width:1180px;margin:0 auto;padding:20px 16px 48px}h1{font-size:20px;margin:0 0 4px}h2{font-size:14px;margin:0 0 6px}
.sub{color:var(--text2);font-size:13px}.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px;min-width:0}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}th,td{text-align:left;padding:6px;border-bottom:1px solid var(--grid);vertical-align:top}
th{color:var(--text2);font-weight:500}.n{text-align:right}.wrap{overflow-x:auto}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(min(100%,360px),1fr));margin-top:12px}
svg{display:block;width:100%;height:auto}svg text{fill:var(--text2);font-size:10px}.pill{font-size:12px;padding:1px 8px;border-radius:99px;border:1px solid var(--border);white-space:nowrap}
.sig{color:var(--bad);border-color:var(--bad)}.conf{color:var(--text2);font-size:12px}
</style></head><body><main>
<h1>Could private-jet departures have flagged it in advance?</h1>
<p class="sub">Lead window = 7 days before the event, compared with days −24..−8. Signal = lead ≥ <span id="r"></span>× baseline and z ≥ <span id="z"></span>.
Controls are quiet periods: signals there are false alarms. Shaded band = lead window; dashed line = baseline mean; purple line = event day.</p>
<div class="card wrap"><table id="t"></table></div>
<div class="grid" id="charts"></div></main>
<script>
const D=/*DATA*/null;const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const f=(x,d=2)=>x==null?'–':x.toFixed(d), pct=x=>x==null?'–':(100*x).toFixed(0)+'%';
document.getElementById('r').textContent=D.ratio;document.getElementById('z').textContent=D.z;
document.getElementById('t').innerHTML='<tr><th>Date</th><th>Event</th><th>Type</th><th>Verdict</th><th class="n">Lead ratio</th><th class="n">z</th><th class="n">Bizjet share</th><th class="n">Out of region</th></tr>'+
 D.rows.map(r=>`<tr><td style="white-space:nowrap">${r.date}</td><td>${esc(r.title)}${r.conf.length?`<div class="conf">Confounder: ${esc(r.conf.join('; '))}</div>`:''}</td>
 <td>${esc(r.cat)}${r.warn?' · '+esc(r.warn):''}</td><td><span class="pill ${r.verdict=='Signal'?'sig':''}">${r.verdict}</span></td>
 <td class="n">${f(r.ratio)}</td><td class="n">${r.z==null?'–':(r.z>0?'+':'')+f(r.z,1)}</td>
 <td class="n">${r.share?pct(r.share[0])+' → '+pct(r.share[1]):'–'}</td><td class="n">${pct(r.out[0])} → ${pct(r.out[1])}</td></tr>`).join('');
const NS='http://www.w3.org/2000/svg';function el(t,a,p){const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);p&&p.appendChild(e);return e}
const host=document.getElementById('charts');
D.charts.forEach(c=>{const card=document.createElement('div');card.className='card';card.innerHTML=`<h2>${esc(c.title)}</h2>`;host.appendChild(card);
 const W=360,H=150,m={l:26,r:6,t:6,b:18},pts=c.pts,vals=pts.map(p=>p[1]).filter(v=>v!=null);
 const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':'Daily business-jet departures around event'},card);
 if(!vals.length){el('text',{x:W/2,y:H/2,'text-anchor':'middle'},svg).textContent='awaiting data';return}
 const max=Math.max(...vals,c.base||0)*1.15||1,k0=pts[0][0],k1=pts[pts.length-1][0];
 const x=k=>m.l+(k-k0)*(W-m.l-m.r)/(k1-k0),y=v=>H-m.b-v*(H-m.t-m.b)/max;
 el('rect',{x:x(-7),y:m.t,width:x(-1)-x(-7),height:H-m.t-m.b,fill:'var(--lead)'},svg);
 [0,max/2,max].forEach(v=>{el('line',{x1:m.l,x2:W-m.r,y1:y(v),y2:y(v),stroke:'var(--grid)'},svg);el('text',{x:m.l-4,y:y(v)+3,'text-anchor':'end'},svg).textContent=Math.round(v)});
 [-21,-14,-7,0,7].forEach(k=>el('text',{x:x(k),y:H-4,'text-anchor':'middle'},svg).textContent=k);
 el('line',{x1:x(0),x2:x(0),y1:m.t,y2:H-m.b,stroke:'var(--ev)','stroke-width':1.5},svg);
 if(c.base)el('line',{x1:x(-24),x2:x(-8),y1:y(c.base),y2:y(c.base),stroke:'var(--muted)','stroke-dasharray':'4 3'},svg);
 let d='',pen=false;pts.forEach(p=>{if(p[1]==null){pen=false;return}d+=(pen?'L':'M')+x(p[0])+' '+y(p[1]);pen=true});
 el('path',{d,fill:'none',stroke:'var(--s1)','stroke-width':2},svg);
 pts.forEach(p=>{if(p[1]==null)return;const g=el('circle',{cx:x(p[0]),cy:y(p[1]),r:6,fill:'transparent'},svg);
  el('title',{},g).textContent=`Day ${p[0]}: ${p[1]} business jets of ${p[2]} departures`})});
</script></body></html>"""
