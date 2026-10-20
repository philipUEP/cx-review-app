#!/usr/bin/env python3
"""
Build the two supporting screens for the complaints dashboard:

  site/codes.html    "New complaint codes"  - the proposed-code queue
  site/quality.html  "Quality"              - weekly agreement checks

Both pages are static: the data is queried here and inlined as JSON, so there is
no fetch and no server. Read-only by design - decisions are taken in the review
app (APP_URL/codes, /quality), which writes to automation_philip; a static page
cannot. Every "how to decide" line points at the app, never at the sheet, because
the sheet stopped being the queue when the app shipped.

Both carry the freshness banner from the `runs` table, like the reviews page: a
mirror that cannot say how old it is invites people to quote stale numbers.

Output: site/codes.html + site/quality.html
"""
import json
import os
import re
from datetime import date

import pymysql

import refresh_status

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, "site")

HREF = {"index": "index.html", "codes": "codes.html", "quality": "quality.html"}


def app_url():
    """Where decisions are made, or "" when there is nowhere yet.

    The old default (https://philip.amzmanager.net) served the nginx placeholder:
    the mirror offered a "Decide in the review app" link that led to a page which
    could not decide anything. Unset means "say so", never "invent a URL".
    """
    return (os.environ.get("CX_APP_URL") or "").strip().rstrip("/")


def app_link(path, label):
    """An anchor to the app, or an explicit note in its place."""
    url = app_url()
    if not url:
        return ('<span class="noapp" title="CX_APP_URL is not set for this build">'
                'review app not available at this address yet</span>')
    return f'<a href="{url}{path}" target="_blank" rel="noopener">{label}</a>'


def banner(st, run_errors=None):
    """Green only for a clean last run; amber for fresh-but-failed.

    refresh_status returns ok/bad, which paints a failed latest attempt green
    while the figures underneath come from an older good run.
    """
    html, level = refresh_status.banner_html(st, run_errors=run_errors)
    if level == "ok" and st.get("failed"):
        html = html.replace('class="syncbar ok"', 'class="syncbar warn"')
        level = "warn"
    return html, level


CSS = r"""
:root{--bg:#f8f9fa;--card:#fff;--ink:#111827;--mut:#6b7280;--line:#e5e7eb;--accent:#2563eb;--warn:#b45309;--bad:#b42318;--ok:#067647;--pink:#fdf2f8;--pinkb:#fbcfe8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif}
a{color:var(--accent);text-decoration:none}
.wrap{display:flex;min-height:100vh}
aside{width:208px;flex:0 0 208px;background:var(--card);border-right:1px solid var(--line);padding:24px 0;position:sticky;top:0;height:100vh;overflow:auto}
.brand{display:flex;align-items:center;gap:9px;font-weight:700;font-size:16px;letter-spacing:-.02em;padding:0 20px 18px;color:var(--ink)}
.brand img.mark{height:24px;width:auto;display:block;flex:none}
.brand span{display:block;font-size:10px;font-weight:600;color:var(--mut);letter-spacing:.09em;text-transform:uppercase;margin-top:3px}
.grp{padding:10px 20px 4px;font-size:10.5px;font-weight:700;color:#9ca3af;letter-spacing:.08em;text-transform:uppercase}
.nav{display:block;padding:8px 20px;color:#374151;font-size:13.5px;border-left:2px solid transparent}
.nav:hover{background:#fafbfc}
.nav.on{color:var(--accent);font-weight:650;background:#f5f8ff;border-left-color:var(--accent)}
.nav.off{color:#b6bcc6;cursor:default}
main{flex:1;min-width:0;padding:20px 24px 60px}
header.top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:16px}
h1{font-size:21px;margin:0;letter-spacing:-.02em}
.sub{color:var(--mut);font-size:12.5px;margin-top:3px}
.cards{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:16px}
.cards.four{grid-template-columns:repeat(4,1fr)}
.cards.q{grid-template-columns:2fr 1fr 1fr;margin-bottom:16px}
@media(max-width:1100px){.cards,.cards.q{grid-template-columns:repeat(3,1fr)}.cards.four{grid-template-columns:repeat(2,1fr)}}
@media(max-width:700px){.cards,.cards.four,.cards.q{grid-template-columns:1fr 1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:12px 14px}
.card .n{font-size:23px;font-weight:680;letter-spacing:-.02em;line-height:1.15}
.card .l{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin-top:3px}
.card .s{color:var(--mut);font-size:12px;margin-top:4px}
.card.hi{border-color:#bfdbfe;background:#f8fbff}
.card.live .n{font-size:34px;margin-top:6px}
.card.live .l{margin-top:0}
.panel{background:var(--card);border:1px solid var(--line);border-radius:9px;overflow:hidden;margin-bottom:16px}
.bar{display:flex;gap:14px;align-items:center;padding:9px 14px;border-bottom:1px solid var(--line);background:#fff;font-size:12.5px;color:#374151}
.bar .h{font-weight:650;font-size:13px}
.bar .r{margin-left:auto;color:var(--mut);font-size:12px;text-align:right}
.tblwrap{overflow:auto;max-height:calc(100vh - 300px);padding-bottom:10px}
table{width:100%;border-collapse:separate;border-spacing:0;font-size:12.5px;white-space:nowrap}
th{position:sticky;top:0;z-index:2;background:#fafafa;text-align:left;font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);font-weight:700;padding:8px 10px;border-bottom:1px solid var(--line)}
.filterrow th{position:sticky;top:31px;z-index:2;background:#fff;padding:4px 6px;border-bottom:1px solid var(--line);font-weight:400;text-transform:none;letter-spacing:0}
.filterrow input,.filterrow select{width:100%;min-width:62px;padding:4px 6px;border:1px solid var(--line);border-radius:5px;font:inherit;font-size:12px;background:#fff}
td{padding:8px 10px;border-bottom:1px solid #f3f4f6;vertical-align:top}
th.num,td.num{text-align:right}
tbody tr{cursor:pointer}
tbody tr:hover td{background:#f9fbff}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;color:#374151}
.tag{display:inline-block;padding:1px 7px;border-radius:999px;font-size:10.5px;font-weight:700;border:1px solid var(--line);background:#f6f6fa;color:var(--mut)}
.tag.blank{background:var(--pink);border-color:var(--pinkb);color:#9d174d}
.tag.rev{background:#fef3f2;border-color:#fecdca;color:var(--bad)}
.tag.new{background:#fff7ed;border-color:#fed7aa;color:var(--warn)}
.tag.ok{background:#ecfdf3;border-color:#abefc6;color:var(--ok)}
.foot{display:flex;align-items:center;gap:10px;padding:10px 12px;border-top:1px solid var(--line);background:#fafafa;font-size:12.5px;color:var(--mut)}
.wrapcell{white-space:normal;min-width:320px;max-width:640px;color:#374151}
.mini{display:inline-block;width:58px;height:5px;border-radius:3px;background:#eef1f5;margin-left:8px;vertical-align:middle;overflow:hidden}
.mini i{display:block;height:100%;background:#93b4f7}
tr.exrow{cursor:default}
tr.exrow td{background:#fbfcfe;border-bottom:1px solid var(--line);white-space:normal;padding:10px 14px}
.ex{white-space:pre-wrap;font-size:12.5px;line-height:1.6;color:#1f2937;max-width:940px}
.exmeta{color:var(--mut);font-size:11.5px;margin-top:6px}
.chart{padding:10px 14px 14px}
.chart svg{width:100%;height:auto;display:block}
.empty{padding:32px 20px;text-align:center;color:var(--mut);font-size:13px}
.empty b{display:block;color:#374151;font-size:14px;margin-bottom:4px;font-weight:650}
.pad{padding:14px}
.prose{margin:0 0 9px;font-size:13px;line-height:1.6;color:#374151;max-width:920px}
.prose:last-child{margin-bottom:0}
.prose.dim{color:var(--mut);font-size:12.5px}
.syncbar{padding:8px 12px;border-radius:7px;font-size:12.5px;margin-bottom:14px;border:1px solid;line-height:1.5}
.syncbar.ok{background:#f0fdf4;border-color:#bbf7d0;color:#166534}
.syncbar.warn{background:#fffbeb;border-color:#fde68a;color:#92400e}
.syncbar.bad{background:#fef2f2;border-color:#fecaca;color:#b42318}
.noapp{display:inline-block;font-weight:600;color:#92400e;font-size:12px}
.ro{font-size:11px;color:var(--mut);font-weight:650;text-transform:uppercase;letter-spacing:.05em}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;background:#f3f4f6;padding:1px 4px;border-radius:4px;color:#374151}
@media(max-width:760px){
 .wrap{display:block}
 aside{position:static;width:auto;flex:none;height:auto;display:flex;align-items:center;gap:2px;
   padding:8px 10px;border-right:0;border-bottom:1px solid var(--line);overflow-x:auto}
 .brand{padding:0 12px 0 4px;font-size:14px;white-space:nowrap}
 .brand span{display:none}
 .nav{padding:6px 10px;border-left:0;border-bottom:2px solid transparent;white-space:nowrap}
 .nav.on{border-left:0;border-bottom-color:var(--accent)}
 .grp{display:none}
 main{padding:16px 14px 48px}
}
"""

SHELL = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ · UEP</title>
<style>__CSS__</style></head><body>
<div class="wrap">
<aside>
  <div class="brand"><img class="mark" src="assets/logo-mark-light.png" alt="UEP" width="84" height="24"><span>Complaint validation</span></div>
__NAV__
</aside>
__BODY__
</div>
<script>
const D=__DATA__;
__JS__
</script></body></html>"""

NAV_ITEMS = [
    ("link", "Reviews", "index"),
    ("link", "New codes", "codes"),
    ("link", "Quality", "quality"),
]


def nav(active):
    out = []
    for item in NAV_ITEMS:
        if item[0] == "grp":
            out.append(f'  <div class="grp">{item[1]}</div>')
            continue
        _, label, key = item
        if key is None:
            out.append(f'  <a class="nav off">{label}</a>')
        elif key == active:
            out.append(f'  <a class="nav on" href="#">{label}</a>')
        else:
            out.append(f'  <a class="nav" href="./{HREF[key]}">{label}</a>')
    return "\n".join(out)


# ---------------------------------------------------------------- page 1: codes

CODES_BODY = r"""<main>
  <header class="top">
    <div>
      <h1>New complaint codes</h1>
      <div class="sub"><span class="ro">read-only</span> · codes the AI proposed that the vocabulary did not
        cover · generated __GEN__ · decisions are taken in the
        __APPCODES__</div>
    </div>
  </header>
  <div class="cards four" id="cards"></div>

  <div class="panel">
    <div class="bar">
      <span id="sumline"></span>
      <span class="r"><span class="ro">Read-only</span> · decide in the
      __APPCODES2__:
      <code>approve</code> adds the code to the vocabulary, <code>deny</code> remaps its reviews to the code you meant.</span>
    </div>
    <div class="tblwrap">
      <table>
        <thead>
          <tr>
            <th>Code</th><th class="num">Reviews</th><th>Example review</th>
            <th>Status</th><th>Decided</th>
          </tr>
          <tr class="filterrow">
            <th><input id="f-q" placeholder="filter code"></th>
            <th><input id="f-n" placeholder="min reviews"></th>
            <th><input id="f-ex" placeholder="filter review text"></th>
            <th><select id="f-st">
              <option value="">All</option>
              <option value="pending">Pending</option>
              <option value="approved">Approved</option>
              <option value="denied">Denied</option>
            </select></th>
            <th></th>
          </tr>
        </thead>
        <tbody id="tb"></tbody>
      </table>
    </div>
    <div class="foot">
      <span id="pinfo"></span>
      <span style="margin-left:auto">Click a row to read the full review text</span>
    </div>
  </div>
</main>"""

CODES_JS = r"""
const S=D.summary, ROWS=D.rows, MAXN=Math.max(1,...ROWS.map(r=>r.n));

const cardDefs=[
  ["pending","Pending","awaiting a decision",true],
  ["approved","Approved","in the vocabulary — the AI can emit them",false],
  ["denied","Denied","kept out; their reviews were remapped",false],
  ["reviews_affected","Reviews affected",S.pending_rows.toLocaleString()+" sit on pending codes",false]
];
document.getElementById("cards").innerHTML=cardDefs.map(([k,l,s,hi])=>
 `<div class="card ${hi?'hi':''}"><div class="n">${(S[k]||0).toLocaleString()}</div><div class="l">${l}</div><div class="s">${s}</div></div>`).join("");
document.getElementById("sumline").innerHTML=
 `<b>${S.pending}</b> pending · <b>${S.approved}</b> approved · <b>${S.denied}</b> denied · `+
 `<b>${S.reviews_affected.toLocaleString()}</b> reviews affected`;

function esc(s){return (s==null?"":String(s)).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}
function trunc(s,n){s=s||"";return esc(s.length>n?s.slice(0,n-1)+"…":s)}
function pill(st){
  if(st==="approved") return '<span class="tag ok">approved</span>';
  if(st==="denied") return '<span class="tag">denied</span>';
  return '<span class="tag new">pending</span>';
}

let VIEW=ROWS.slice();

function render(){
  const tb=document.getElementById("tb");
  if(!VIEW.length){
    tb.innerHTML='<tr><td colspan="5"><div class="empty"><b>Nothing matches those filters.</b>Clear a filter to see the queue again.</div></td></tr>';
  }else{
    tb.innerHTML=VIEW.map(r=>{
      const w=Math.max(3,Math.round(r.n/MAXN*100));
      const dec=r.at?`<span class="mono">${esc(r.at)}</span>${r.by?" · user #"+esc(r.by):""}`:"<span style=\"color:#9ca3af\">—</span>";
      return `<tr data-i="${r.i}">
        <td class="mono">${esc(r.code)}</td>
        <td class="num">${r.n.toLocaleString()}<span class="mini"><i style="width:${w}%"></i></span></td>
        <td class="wrapcell">${trunc(r.ex,160)}</td>
        <td>${pill(r.status)}</td>
        <td>${dec}</td></tr>
      <tr class="exrow" data-x="${r.i}" hidden><td colspan="5">
        <div class="ex">${esc(r.ex)||"(no example review recorded)"}</div>
        <div class="exmeta">${esc(r.code)} · ${r.n.toLocaleString()} reviews · ${esc(r.status)}${r.note?" · note: "+esc(r.note):""}${r.at?" · decided "+esc(r.at):""}</div>
      </td></tr>`;
    }).join("");
    tb.querySelectorAll("tr[data-i]").forEach(tr=>{
      tr.onclick=()=>{
        const x=tb.querySelector(`tr.exrow[data-x="${tr.dataset.i}"]`);
        if(x) x.hidden=!x.hidden;
      };
    });
  }
  document.getElementById("pinfo").textContent=`${VIEW.length.toLocaleString()} of ${ROWS.length} codes · sorted pending first, then by reviews affected`;
}

function apply(){
  const q=document.getElementById("f-q").value.trim().toLowerCase();
  const ex=document.getElementById("f-ex").value.trim().toLowerCase();
  const st=document.getElementById("f-st").value;
  const min=parseInt(document.getElementById("f-n").value,10);
  VIEW=ROWS.filter(r=>{
    if(q && !(r.code||"").toLowerCase().includes(q)) return false;
    if(ex && !(r.ex||"").toLowerCase().includes(ex)) return false;
    if(st && r.status!==st) return false;
    if(!isNaN(min) && r.n<min) return false;
    return true;
  });
  render();
}
["f-q","f-n","f-ex"].forEach(id=>document.getElementById(id).addEventListener("input",()=>{
  clearTimeout(window._t); window._t=setTimeout(apply,150);
}));
document.getElementById("f-st").addEventListener("change",apply);
render();
"""

# -------------------------------------------------------------- page 2: quality

QUALITY_BODY = r"""<main>
  <header class="top">
    <div>
      <h1>Quality</h1>
      <div class="sub"><span class="ro">read-only</span> · the weekly random check — the record the taper
        decision rests on · generated __GEN__ · the calls are recorded in the
        __APPQUALITY__</div>
    </div>
  </header>

  <div class="cards q">
    <div class="card live hi" id="live"></div>
    <div class="card" id="c-checks"></div>
    <div class="card" id="c-queue"></div>
  </div>

  <div class="panel">
    <div class="bar"><span class="h">Weekly agreement</span><span class="r" id="chartsub"></span></div>
    <div class="chart" id="chart"></div>
  </div>

  <div class="panel">
    <div class="bar"><span class="h">Weekly checks</span><span class="r" id="tablesub">newest first</span></div>
    <div class="tblwrap">
      <table>
        <thead><tr>
          <th>Week starting</th><th class="num">Sample</th><th>Agreement</th>
          <th>Checked by</th><th>Sample ref</th><th>Note</th>
        </tr></thead>
        <tbody id="tb"></tbody>
      </table>
    </div>
  </div>

  <div class="panel">
    <div class="bar"><span class="h">How this is measured</span></div>
    <div class="pad">
      <p class="prose"><b>Weekly random check.</b> Each week a random sample of 45 reviews is drawn from the whole
      classified corpus — not from the review queue — stratified by complaint category so small but important
      categories are not swamped by returns. The sample is seeded on the ISO week and persisted, so it is
      reproducible and does not re-draw on every refresh. The calls are recorded in the
      __APPQUALITY2__ — not in the sheet —
      and agreement is the share of calls that match the AI's code, with reviews marked
      <code>NOT A COMPLAINT</code> excluded from the denominator.</p>
      <p class="prose">This is the number to use for the taper: it is an unbiased estimate of how often the AI's code
      survives a human read. Two months of weekly checks at 45 rows each; after that the sample can shrink or the
      interval lengthen, and this page is the evidence for that decision.</p>
      <p class="prose dim"><b>Not the same number:</b> <i>queue accuracy</i> is computed from the decisions recorded on
      the review app, i.e. the row set the model itself flagged as uncertain. It reads lower than reality, moves
      with the backlog's composition, and its denominator excludes removed reviews — so it is a workload indicator,
      not the accuracy of the system. The two are shown separately and never averaged.</p>
    </div>
  </div>
</main>"""

QUALITY_JS = r"""
const L=D.live, W=D.weekly, CH=D.checks;
const APPQ=__APPQJSON__;   /* anchor, or the "not available yet" note */

function esc(s){return (s==null?"":String(s)).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}

/* the headline is the WEEKLY RANDOM CHECK, not the queue rollup */
const last = W.latest;
const pctTxt = last && last.pct!=null ? last.pct.toFixed(1)+"%" : "—";
document.getElementById("live").innerHTML=
  `<div class="l">Weekly random check${last?" — week of "+esc(last.week):""}</div>
   <div class="n">${pctTxt}</div>
   <div class="s">${last
     ? `${Number(last.sample).toLocaleString()} reviews checked by hand · the ${W.current_week} sample is called in the review app`
     : `No weekly check recorded yet. The ${W.sample_n}-row sample for ${esc(W.current_week)} is waiting in the ${APPQ} — call five rows and it lands here.`}</div>`;

document.getElementById("c-checks").innerHTML=
  `<div class="n">${CH.length.toLocaleString()}</div><div class="l">Weekly checks logged</div>
   <div class="s">${CH.length?`${W.sample_n} rows per week · taper record`:"two months of weekly checks is the plan"}</div>`;

document.getElementById("c-queue").innerHTML=
  `<div class="n">${L.pct==null?"—":L.pct.toFixed(1)+"%"}</div><div class="l">Queue accuracy</div>
   <div class="s">${L.n
     ? `${L.n.toLocaleString()} decisions on the AI's uncertain rows · ${L.accepted.toLocaleString()} agreed, ${L.corrected.toLocaleString()} corrected`
     : "no review decisions recorded yet"}</div>
   <div class="s" style="color:#9ca3af">workload indicator, not system accuracy</div>`;

function pill(v){
  if(v==null) return '<span class="tag">—</span>';
  if(v>=90) return `<span class="tag ok">${v.toFixed(1)}%</span>`;
  if(v>=80) return `<span class="tag new">${v.toFixed(1)}%</span>`;
  return `<span class="tag rev">${v.toFixed(1)}%</span>`;
}

function renderTable(){
  const tb=document.getElementById("tb");
  if(!CH.length){
    tb.innerHTML='<tr><td colspan="6"><div class="empty"><b>No weekly check recorded yet.</b>'+
      'The first row lands here after a sample is reviewed and logged in <code>quality_checks</code>.</div></td></tr>';
    return;
  }
  tb.innerHTML=CH.map(r=>`<tr>
    <td class="mono">${esc(r.week)}</td>
    <td class="num">${Number(r.sample).toLocaleString()}</td>
    <td>${pill(r.pct)}${r.pct!=null?'<span class="mini"><i style="width:'+Math.max(0,Math.min(100,r.pct))+'%"></i></span>':""}</td>
    <td>${r.by?"user #"+esc(r.by):"—"}</td>
    <td class="mono">${esc(r.ref)||"—"}</td>
    <td class="wrapcell">${esc(r.note)||"—"}</td></tr>`).join("");
}

/* inline SVG, no libraries: one point per week with a recorded percentage */
function chartSVG(rows){
  const W=820,H=230,padL=44,padR=18,padT=16,padB=34;
  const iw=W-padL-padR, ih=H-padT-padB, n=rows.length;
  const X=i=>padL + (n===1?iw/2:i*iw/(n-1));
  const Y=v=>padT + ih - (Math.max(0,Math.min(100,v))/100)*ih;
  let g="";
  [0,25,50,75,100].forEach(v=>{
    g+=`<line x1="${padL}" x2="${W-padR}" y1="${Y(v)}" y2="${Y(v)}" stroke="#eef0f3"/>`;
    g+=`<text x="${padL-8}" y="${Y(v)+3.5}" text-anchor="end" font-size="9.5" fill="#9ca3af">${v}%</text>`;
  });
  const pts=rows.map((r,i)=>[X(i),Y(r.pct)]);
  const line=pts.map(p=>p[0].toFixed(1)+","+p[1].toFixed(1)).join(" ");
  g+=`<polygon points="${padL},${Y(0)} ${line} ${W-padR},${Y(0)}" fill="#2563eb" opacity="0.06"/>`;
  g+=`<polyline points="${line}" fill="none" stroke="#2563eb" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  const step=Math.ceil(n/8);
  pts.forEach((p,i)=>{
    const r=rows[i], isLast=i===n-1;
    g+=`<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="${isLast?4:3}" fill="${isLast?"#2563eb":"#fff"}" stroke="#2563eb" stroke-width="2"><title>week of ${esc(r.week)}: ${r.pct.toFixed(1)}% of ${r.sample} reviews</title></circle>`;
    if(i%step===0||isLast){
      g+=`<text x="${p[0].toFixed(1)}" y="${H-12}" text-anchor="middle" font-size="9.5" fill="#9ca3af">${esc(r.week.slice(5))}</text>`;
    }
  });
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Agreement percentage by week" preserveAspectRatio="xMidYMid meet">${g}</svg>`;
}

function renderChart(){
  const pts=CH.filter(r=>r.pct!=null).slice().sort((a,b)=>a.week<b.week?-1:1);
  const box=document.getElementById("chart"), sub=document.getElementById("chartsub");
  if(pts.length>=2){
    box.innerHTML=chartSVG(pts);
    sub.textContent=`${pts.length} weeks · ${pts[0].week} to ${pts[pts.length-1].week}`;
  }else if(pts.length===1){
    box.innerHTML=`<div class="empty"><b>One check logged.</b>Week of ${esc(pts[0].week)} came in at ${pts[0].pct.toFixed(1)}% on a sample of ${Number(pts[0].sample).toLocaleString()}. The trend line appears once a second week is recorded.</div>`;
    sub.textContent="1 week";
  }else{
    box.innerHTML='<div class="empty"><b>No trend yet.</b>The line appears after two weekly checks are logged — one week on its own is a number, not a trend.</div>';
    sub.textContent="awaiting the first check";
  }
}

renderTable();
renderChart();
document.getElementById("tablesub").textContent = CH.length ? `${CH.length} week${CH.length===1?"":"s"} · newest first` : "newest first";
"""


def connect():
    return pymysql.connect(
        host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
        password=os.environ["CXAPP_DB_PASSWORD"], database=os.environ["CXAPP_DB_NAME"],
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)


def write_page(path, html):
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {os.path.relpath(path, HERE)}: {len(html):,} bytes")


def codes_payload(cur):
    cur.execute("""SELECT code, n_rows, example_review, status, decided_by, decided_at, note
                   FROM proposed_codes""")
    rows = cur.fetchall()
    rows.sort(key=lambda r: (r["status"] != "pending", -int(r["n_rows"] or 0), (r["code"] or "").lower()))

    summary = {"pending": 0, "approved": 0, "denied": 0,
               "reviews_affected": 0, "pending_rows": 0}
    out = []
    for i, r in enumerate(rows):
        st = r["status"] or "pending"
        summary[st] = summary.get(st, 0) + 1
        summary["reviews_affected"] += int(r["n_rows"] or 0)
        if st == "pending":
            summary["pending_rows"] += int(r["n_rows"] or 0)
        out.append({
            "i": i,
            "code": r["code"],
            "n": int(r["n_rows"] or 0),
            "ex": r["example_review"] or "",
            "status": st,
            "by": r["decided_by"],
            "at": r["decided_at"].isoformat(sep=" ")[:16] if r["decided_at"] else "",
            "note": r["note"] or "",
        })
    return {"generated": date.today().isoformat(), "summary": summary, "rows": out}


def quality_payload(cur):
    # The taper record: weekly random samples only. The old auto rollup wrote
    # queue-derived rows into this table, so anything not tagged 'weekly-' is not
    # a sample and must not be plotted as one.
    cur.execute("""SELECT week_start, sample_n, agreement_pct, checked_by, sample_ref, note
                   FROM quality_checks WHERE sample_ref LIKE 'weekly-%%'
                   ORDER BY week_start DESC""")
    checks = [{
        "week": r["week_start"].isoformat(),
        "sample": int(r["sample_n"] or 0),
        "pct": float(r["agreement_pct"]) if r["agreement_pct"] is not None else None,
        "by": r["checked_by"],
        "ref": r["sample_ref"] or "",
        "note": r["note"] or "",
    } for r in cur.fetchall()]

    # Queue accuracy: agreement among the decisions taken on the model's
    # uncertain rows. Kept, labelled, and never mixed into the weekly number.
    cur.execute("""SELECT COUNT(*) n,
                          COALESCE(SUM(new_complaint IS NULL),0) accepted,
                          COALESCE(SUM(new_complaint IS NOT NULL),0) corrected
                   FROM corrections c
                   JOIN (SELECT review_id, MAX(id) mid FROM corrections
                         WHERE decision <> 'rejected' GROUP BY review_id) t ON t.mid = c.id""")
    live = cur.fetchone()
    cur.execute("SELECT COUNT(*) c FROM corrections WHERE decision='rejected'")
    rejected = int(cur.fetchone()["c"] or 0)

    n = int(live["n"] or 0)
    accepted = int(live["accepted"] or 0)
    corrected = int(live["corrected"] or 0)
    denom = accepted + corrected if (accepted + corrected) else n
    pct = round(accepted * 100.0 / denom, 1) if denom else None

    import datetime as _dt
    today = _dt.date.today()
    y, w, _ = today.isocalendar()
    return {
        "generated": date.today().isoformat(),
        "live": {"n": n, "accepted": accepted, "corrected": corrected,
                 "rejected": rejected, "pct": pct},
        "weekly": {"latest": checks[0] if checks else None, "count": len(checks),
                   "sample_n": 45, "current_week": f"{y}-W{w:02d}"},
        "checks": checks,
    }


PLACEHOLDER = re.compile(r"__[A-Z][A-Z0-9_]*__")


def render(shell, active, title, gen, body, data, js, banner=""):
    data_json = json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")
    html = (shell
            .replace("__CSS__", CSS)
            .replace("__TITLE__", title)
            .replace("__NAV__", nav(active))
            .replace("__BODY__", banner + body.replace("__GEN__", gen))
            .replace("__JS__", js)
            .replace("__DATA__", data_json))
    # One pass at the end, so a placeholder inside the inline JS is substituted
    # too (it was not, and the quality page shipped a literal __APP__ link).
    # App links resolve to an explicit "not available yet" note when CX_APP_URL
    # is unset: the mirror must never send anyone to a placeholder.
    html = (html
            .replace("__APPCODES__", app_link("/codes", "review app"))
            .replace("__APPCODES2__", app_link("/codes", "review app → New codes ↗"))
            .replace("__APPQUALITY__", app_link("/quality", "review app"))
            .replace("__APPQUALITY2__", app_link("/quality", "review app's Quality page"))
            .replace("__APPQJSON__", json.dumps(app_link("/quality", "review app")))
            .replace("__GEN__", gen))
    left = PLACEHOLDER.findall(html)
    if left:
        raise SystemExit(f"unsubstituted placeholder(s) in {title}: {sorted(set(left))}")
    return html


def main():
    conn = connect()
    cur = conn.cursor()

    codes = codes_payload(cur)
    q = quality_payload(cur)
    st = refresh_status.status(cur)
    banner_html, level = banner(st, run_errors=os.environ.get("CX_REFRESH_ERRORS") or None)
    print(f"refresh: last successful {st['last_ok'] or 'never'}"
          f"{' (STALE)' if not st['ok'] else ''}; banner={level}")

    os.makedirs(SITE, exist_ok=True)
    write_page(os.path.join(SITE, "codes.html"),
               render(SHELL, "codes", "New complaint codes", codes["generated"], CODES_BODY, codes,
                      CODES_JS, banner_html))
    write_page(os.path.join(SITE, "quality.html"),
               render(SHELL, "quality", "Quality", q["generated"], QUALITY_BODY, q, QUALITY_JS, banner_html))

    s = codes["summary"]
    print(f"codes: {len(codes['rows'])} rows · {s['pending']} pending / "
          f"{s['approved']} approved / {s['denied']} denied · "
          f"{s['reviews_affected']:,} reviews affected")
    print(f"quality: {len(q['checks'])} weekly checks · live agreement "
          f"{q['live']['pct'] if q['live']['pct'] is not None else 'n/a'} "
          f"(n={q['live']['n']}, accepted={q['live']['accepted']}, "
          f"corrected={q['live']['corrected']}, rejected={q['live']['rejected']})")
    conn.close()


if __name__ == "__main__":
    main()
