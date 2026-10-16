"""Renders data/dashboard.html: a self-contained page (no external scripts)."""
import datetime as dt
import json

from . import analyze as A
from .config import AIRPORTS, COUNTRY_NAMES, DATA


def build(con, days=90):
    flights = A.load_bizjet_flights(con)
    cov = A.coverage(con)
    counts = A.daily_counts(flights, cov, set(A.blind_airports(con)))
    if not counts:
        raise SystemExit("No covered days yet. Run `backfill` and/or `live` first.")
    end = max(counts["ALL"])
    start = max(min(counts["ALL"]), end - dt.timedelta(days=days - 1))
    span = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    ap_country = A.airport_countries(con)
    names = {r[0]: r[1] for r in con.execute("SELECT icao, name FROM airports")}
    events = A.load_events()
    study = A.event_study(counts, events)

    series = {}
    for c in ["ALL"] + list(COUNTRY_NAMES):
        s = counts[c]
        series[c] = [[d.isoformat(), s.get(d), A.baseline(s, d)] for d in span]
    vol = {c: sum(v or 0 for _, v, _ in series[c]) for c in COUNTRY_NAMES}

    avg = [None] * (2 * A.WINDOW + 1)
    valid = [r["ratios"] for r in study if r["ratios"]]
    for k in range(len(avg)):
        xs = [r[k] for r in valid if r[k] is not None]
        avg[k] = sum(xs) / len(xs) if xs else None

    dest = A.destinations(flights, ap_country, start, end)
    payload = dict(
        generated=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        start=start.isoformat(), end=end.isoformat(),
        countries={"ALL": "All tracked airports", **{c: n for c, n in COUNTRY_NAMES.items() if vol[c]}},
        series=series,
        events=[dict(date=e["date"].isoformat(), countries=e["countries"], title=e["title"],
                     category=e["category"]) for e in events],
        study=[dict(date=r["event"]["date"].isoformat(), countries=";".join(r["event"]["countries"]),
                    title=r["event"]["title"], stat=r["stat"], p=r["p"]) for r in study],
        study_avg=avg, window=A.WINDOW,
        anomalies=sorted([dict(date=d.isoformat(), country=COUNTRY_NAMES.get(c, "All"), x=x, b=b, z=round(z, 1))
                          for c in ["ALL"] + list(COUNTRY_NAMES) for d, x, b, z in A.anomalies(counts[c])
                          if start <= d <= end], key=lambda a: a["date"], reverse=True)[:40],
        dest=[[A.country_name(c), n] for c, n in dest.most_common(12)],
        routes=[[a, names.get(a, ""), b, names.get(b, ""), n] for (a, b), n in A.top_routes(flights, start, end, 20)],
        airports=len(AIRPORTS),
    )
    out = DATA / "dashboard.html"
    out.write_text(HTML.replace("/*DATA*/null", json.dumps(payload)))
    return out


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gulf Jet Tracker</title>
<style>
:root{color-scheme:light;--bg:#f6f6f4;--surface:#fcfcfb;--border:#e4e3df;--grid:#ecebe7;
 --text:#0b0b0b;--text2:#52514e;--muted:#8a8984;--s1:#2a78d6;--s1soft:#2a78d622;--hi:#eb6834;--ev:#6250d6}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#111110;--surface:#1a1a19;
 --border:#2c2c2a;--grid:#262624;--text:#f0efec;--text2:#c3c2b7;--muted:#8d8c86;--s1:#3987e5;--s1soft:#3987e533;--hi:#d95926;--ev:#9085e9}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#111110;--surface:#1a1a19;--border:#2c2c2a;--grid:#262624;
 --text:#f0efec;--text2:#c3c2b7;--muted:#8d8c86;--s1:#3987e5;--s1soft:#3987e533;--hi:#d95926;--ev:#9085e9}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,sans-serif}
main{max-width:1180px;margin:0 auto;padding:20px 16px 48px}h1{font-size:20px;margin:0}h2{font-size:15px;margin:0 0 10px}
.sub{color:var(--text2);font-size:13px}.row{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));margin:16px 0}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px;min-width:0}
.kpi .v{font-size:26px;font-weight:600;font-variant-numeric:tabular-nums}.kpi .l{color:var(--text2);font-size:12px}
.grid2{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(min(100%,520px),1fr));margin-top:12px}
select{font:inherit;padding:4px 8px;border-radius:6px;border:1px solid var(--border);background:var(--surface);color:var(--text)}
svg{display:block;width:100%;height:auto;overflow:visible}svg text{fill:var(--text2);font-size:11px}
.tip{position:fixed;pointer-events:none;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 10px;
 font-size:12px;box-shadow:0 4px 16px #0002;display:none;z-index:9;max-width:260px}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:5px 6px;border-bottom:1px solid var(--grid)}th{color:var(--text2);font-weight:500}td.n,th.n{text-align:right}
.scroll{max-height:340px;overflow:auto}.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--text2);margin:6px 0}
.sw{display:inline-block;width:14px;height:3px;vertical-align:middle;margin-right:5px;border-radius:2px}
.bar{height:8px;border-radius:0 4px 4px 0;background:var(--s1)}
</style></head><body><main>
<h1>Private jet departures — Middle East</h1>
<div class="sub" id="meta"></div>
<div class="row" id="kpis"></div>
<div class="card">
  <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;align-items:center">
    <h2 style="margin:0">Daily business-jet departures</h2>
    <select id="country" aria-label="Country"></select>
  </div>
  <div class="legend"><span><span class="sw" style="background:var(--s1)"></span>Departures</span>
   <span><span class="sw" style="background:var(--muted)"></span>28-day median baseline</span>
   <span><span class="sw" style="background:var(--ev)"></span>Event (events.csv)</span>
   <span><span class="sw" style="background:var(--hi);height:8px;width:8px;border-radius:50%"></span>Anomaly (|z| ≥ 2.5)</span></div>
  <div id="main"></div>
</div>
<div class="grid2">
  <div class="card"><h2>Average around events (departures ÷ baseline)</h2><div id="study"></div>
   <div class="sub">Day 0 = event date. 1.0 = normal. Gaps mean no events with data yet.</div></div>
  <div class="card"><h2>Where they go (destination country)</h2><div id="dest"></div></div>
</div>
<div class="grid2">
  <div class="card"><h2>Event results</h2><div class="scroll" id="evtab"></div></div>
  <div class="card"><h2>Anomalous days</h2><div class="scroll" id="anom"></div></div>
</div>
<div class="grid2"><div class="card"><h2>Top routes</h2><div class="scroll" id="routes"></div></div></div>
</main><div class="tip" id="tip"></div>
<script>
const D=/*DATA*/null;
const $=id=>document.getElementById(id), tip=$('tip');
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function showTip(e,html){tip.innerHTML=html;tip.style.display='block';
 const x=Math.min(e.clientX+14,innerWidth-tip.offsetWidth-8),y=Math.min(e.clientY+14,innerHeight-tip.offsetHeight-8);
 tip.style.left=x+'px';tip.style.top=y+'px'}
const hideTip=()=>tip.style.display='none';
const NS='http://www.w3.org/2000/svg';
function el(t,a,p){const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);p&&p.appendChild(e);return e}
const fmt=n=>n==null?'–':Number(n).toLocaleString(undefined,{maximumFractionDigits:1});

$('meta').textContent=`${D.start} → ${D.end} · ${D.airports} tracked airports · generated ${D.generated}`;
const all=D.series.ALL.filter(r=>r[1]!=null), tot=all.reduce((s,r)=>s+r[1],0);
const last7=all.slice(-7), prev=all.slice(-35,-7);
const avg=a=>a.length?a.reduce((s,r)=>s+r[1],0)/a.length:null;
const chg=avg(prev)?(avg(last7)/avg(prev)-1)*100:null;
const kp=[[fmt(tot),'Departures in period'],[fmt(avg(all)),'Average per day'],
 [chg==null?'–':(chg>0?'+':'')+chg.toFixed(0)+'%','Last 7 days vs prior 4 weeks'],
 [(D.dest.find(r=>r[0]!='Unknown')||['–'])[0],'Top known destination']];
$('kpis').innerHTML=kp.map(([v,l])=>`<div class="card kpi"><div class="v">${v}</div><div class="l">${l}</div></div>`).join('');

const sel=$('country');for(const[c,n]of Object.entries(D.countries))sel.add(new Option(n,c));
sel.onchange=()=>drawMain(sel.value);

function drawMain(c){
 const host=$('main');host.innerHTML='';const rows=D.series[c];const W=1100,H=300,m={l:36,r:10,t:22,b:24};
 const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':'Daily departures line chart'},host);
 const n=rows.length,max=Math.max(4,...rows.map(r=>Math.max(r[1]??0,r[2]??0)))*1.1;
 const x=i=>m.l+(n<2?0:i*(W-m.l-m.r)/(n-1)),y=v=>H-m.b-v*(H-m.t-m.b)/max;
 for(let k=0;k<=4;k++){const v=max*k/4,yy=y(v);el('line',{x1:m.l,x2:W-m.r,y1:yy,y2:yy,stroke:'var(--grid)'},svg);
  el('text',{x:m.l-6,y:yy+4,'text-anchor':'end'},svg).textContent=Math.round(v)}
 const step=Math.max(1,Math.ceil(n/8));rows.forEach((r,i)=>{if(i%step==0)el('text',{x:x(i),y:H-6,'text-anchor':'middle'},svg).textContent=r[0].slice(5)});
 const idx=Object.fromEntries(rows.map((r,i)=>[r[0],i]));
 D.events.filter(e=>idx[e.date]!=null&&(c=='ALL'||e.countries.includes(c)||e.countries.includes('ALL'))).forEach(e=>{
  const xx=x(idx[e.date]);el('line',{x1:xx,x2:xx,y1:m.t-8,y2:H-m.b,stroke:'var(--ev)','stroke-width':1.5,'stroke-dasharray':'3 3'},svg);
  el('text',{x:xx+3,y:m.t-10,style:'fill:var(--ev)'},svg).textContent=e.title.slice(0,22)});
 const path=k=>{let d='',pen=false;rows.forEach((r,i)=>{if(r[k]==null){pen=false;return}d+=(pen?'L':'M')+x(i)+' '+y(r[k]);pen=true});return d};
 el('path',{d:path(2),fill:'none',stroke:'var(--muted)','stroke-width':1.5,'stroke-dasharray':'5 4'},svg);
 el('path',{d:path(1),fill:'none',stroke:'var(--s1)','stroke-width':2,'stroke-linejoin':'round'},svg);
 const anom=new Set(D.anomalies.filter(a=>a.country==(D.countries[c]=='All tracked airports'?'All':D.countries[c])).map(a=>a.date));
 rows.forEach((r,i)=>{if(r[1]!=null&&anom.has(r[0]))el('circle',{cx:x(i),cy:y(r[1]),r:4.5,fill:'var(--hi)',stroke:'var(--surface)','stroke-width':2},svg)});
 const cross=el('line',{y1:m.t,y2:H-m.b,stroke:'var(--muted)',visibility:'hidden'},svg);
 const dot=el('circle',{r:4,fill:'var(--s1)',stroke:'var(--surface)','stroke-width':2,visibility:'hidden'},svg);
 const hit=el('rect',{x:m.l,y:0,width:W-m.l-m.r,height:H,fill:'transparent'},svg);
 hit.onmousemove=e=>{const b=svg.getBoundingClientRect(),px=(e.clientX-b.left)*W/b.width;
  const i=Math.max(0,Math.min(n-1,Math.round((px-m.l)/((W-m.l-m.r)/Math.max(1,n-1)))));const r=rows[i];
  cross.setAttribute('x1',x(i));cross.setAttribute('x2',x(i));cross.setAttribute('visibility','visible');
  if(r[1]!=null){dot.setAttribute('cx',x(i));dot.setAttribute('cy',y(r[1]));dot.setAttribute('visibility','visible')}else dot.setAttribute('visibility','hidden');
  const evs=D.events.filter(e=>e.date==r[0]).map(e=>`<div style="color:var(--ev)">● ${esc(e.title)}</div>`).join('');
  showTip(e,`<b>${r[0]}</b><div>Departures: <b>${r[1]==null?'no data':r[1]}</b></div><div>Baseline: ${fmt(r[2])}</div>${evs}`)};
 hit.onmouseleave=()=>{cross.setAttribute('visibility','hidden');dot.setAttribute('visibility','hidden');hideTip()};
}
drawMain('ALL');

(function(){const host=$('study'),W=520,H=200,m={l:32,r:8,t:10,b:22},a=D.study_avg,n=a.length;
 const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':'Event study bar chart'},host);
 const max=Math.max(1.5,...a.filter(v=>v!=null))*1.1,bw=(W-m.l-m.r)/n,y=v=>H-m.b-v*(H-m.t-m.b)/max;
 [0,0.5,1,1.5].filter(v=>v<=max).forEach(v=>{el('line',{x1:m.l,x2:W-m.r,y1:y(v),y2:y(v),stroke:v==1?'var(--muted)':'var(--grid)','stroke-dasharray':v==1?'4 3':''},svg);
  el('text',{x:m.l-5,y:y(v)+4,'text-anchor':'end'},svg).textContent=v});
 a.forEach((v,i)=>{const off=i-D.window,xx=m.l+i*bw;el('text',{x:xx+bw/2,y:H-6,'text-anchor':'middle'},svg).textContent=off;
  if(v==null)return;const r=el('path',{d:`M${xx+2} ${y(0)}V${y(v)+4}q0 -4 4 -4h${bw-12}q4 0 4 4V${y(0)}Z`,fill:off==0?'var(--hi)':'var(--s1)'},svg);
  const h=el('rect',{x:xx,y:m.t,width:bw,height:H-m.t-m.b,fill:'transparent'},svg);
  h.onmousemove=e=>showTip(e,`Day ${off>0?'+':''}${off}<br>Avg ratio: <b>${v.toFixed(2)}</b>`);h.onmouseleave=hideTip});
})();

(function(){const t=D.dest.reduce((s,r)=>s+r[1],0)||1,mx=Math.max(1,...D.dest.map(r=>r[1]));
 $('dest').innerHTML='<table>'+D.dest.map(([c,n])=>`<tr><td style="width:34%">${esc(c)}</td><td><div class="bar" style="width:${100*n/mx}%"></div></td>
 <td class="n" style="width:70px">${n}</td><td class="n" style="width:56px">${(100*n/t).toFixed(1)}%</td></tr>`).join('')+'</table>'})();

$('evtab').innerHTML=D.study.length?'<table><tr><th>Date</th><th>Where</th><th>Event</th><th class="n">Ratio −3..+1</th><th class="n">p</th></tr>'+
 D.study.map(s=>`<tr><td>${s.date}</td><td>${esc(s.countries)}</td><td>${esc(s.title)}</td><td class="n">${s.stat==null?'no data':s.stat.toFixed(2)}</td>
 <td class="n">${s.p==null?'':s.p<0.001?'&lt;0.001':s.p.toFixed(3)}</td></tr>`).join('')+'</table>':'<div class="sub">Add rows to events.csv.</div>';
$('anom').innerHTML=D.anomalies.length?'<table><tr><th>Date</th><th>Country</th><th class="n">Departures</th><th class="n">Baseline</th><th class="n">z</th></tr>'+
 D.anomalies.map(a=>`<tr><td>${a.date}</td><td>${esc(a.country)}</td><td class="n">${a.x}</td><td class="n">${fmt(a.b)}</td><td class="n">${a.z>0?'+':''}${a.z}</td></tr>`).join('')+'</table>':'<div class="sub">None yet (needs ≥14 days of history).</div>';
$('routes').innerHTML='<table><tr><th>From</th><th>To</th><th class="n">Flights</th></tr>'+D.routes.map(r=>`<tr><td><b>${r[0]}</b> <span class="sub">${esc(r[1])}</span></td><td><b>${r[2]}</b> <span class="sub">${esc(r[3])}</span></td><td class="n">${r[4]}</td></tr>`).join('')+'</table>';
</script></body></html>
"""
