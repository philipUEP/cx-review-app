#!/usr/bin/env python3
"""Check the complaint-mix panel against the database, over HTTP.

Every number in the mix is re-derived from SQL here, independently of app.py:
the change column must compare two counts taken over the SAME window, the month
series must count the same rows the distribution does, a thin prior window must
show no percentage at all, and the partial month must be drawn/labelled as
partial. Usage:

    APP=127.0.0.1:8103 USER=chadia PASS=fix tests/check_mix_consistency.py
"""
import base64
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

import pymysql

BASE = os.environ.get("APP", "127.0.0.1:8103")
AUTH = base64.b64encode(
    f"{os.environ.get('USER', 'chadia')}:{os.environ.get('PASS', 'fix')}".encode()).decode()

FAILS = []
CHECKS = 0


def fail(msg):
    FAILS.append(msg)
    print("FAIL", msg)


def ok(msg):
    global CHECKS
    CHECKS += 1
    print("ok  ", msg)


def get(path):
    req = urllib.request.Request(f"http://{BASE}{path}", headers={"Authorization": f"Basic {AUTH}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8")


def db():
    return pymysql.connect(host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
                           password=os.environ["CXAPP_DB_PASSWORD"],
                           database=os.environ["CXAPP_DB_NAME"], charset="utf8mb4",
                           cursorclass=pymysql.cursors.DictCursor)


def where(q):
    """The same filter semantics as app.build_where, re-implemented from the URL."""
    w, a = [], []
    if q.get("q"):
        w.append("(v.review_text LIKE %s OR v.title LIKE %s OR v.sku LIKE %s OR v.product LIKE %s)")
        a += [f"%{q['q']}%"] * 4
    if q.get("family"):
        w.append("v.product_family=%s"); a.append(q["family"])
    if q.get("parent"):
        w.append("v.parent=%s"); a.append(q["parent"])
    if q.get("complaint"):
        w.append("v.final_complaint=%s"); a.append(q["complaint"])
    if q.get("month"):
        w.append("DATE_FORMAT(v.review_date,'%%Y-%%m')=%s"); a.append(q["month"])
    if q.get("tx") == "prose":
        w.append("v.has_prose=1")
    elif q.get("tx") == "codeonly":
        w.append("v.has_prose=0")
    if q.get("status") == "decided":
        w.append("v.human_validated=1")
    return ("WHERE " + " AND ".join(w)) if w else "", a


def dist(cur, q, extra_from=None, extra_to=None):
    w, a = where(q)
    if extra_from:
        w = (w + " AND " if w else "WHERE ") + "v.review_date BETWEEN %s AND %s"
        a = a + [extra_from, extra_to]
    cur.execute(f"SELECT COALESCE(v.final_complaint,'(none)') c, COUNT(*) n "
                f"FROM v_ai_complaints v {w} GROUP BY c", a)
    return {r["c"]: int(r["n"]) for r in cur.fetchall()}


def mix_rows(html):
    """[(code, all_time, in_window, delta_text, delta_title)] from the mix table."""
    sec = html[html.find('<div class="mix">'):]
    out = []
    for tr in re.findall(r"<tr>(.*?)</tr>", sec, re.S):
        m = re.search(r'<td class="c" title="([^"]*)">', tr)
        if not m:
            continue
        code = (m.group(1).replace("&amp;", "&").replace("&#x27;", "'")
                .replace("&gt;", ">").replace("&lt;", "<").replace("&quot;", '"'))
        n = re.search(r'<td class="n">([\d,]+)', tr)
        win = re.search(r'<div class="meta">(?:all-time · )?([\d,]+) in window</div>', tr)
        dl = re.search(r'<span class="dl (\w+)"([^>]*)>([^<]*)</span>', tr)
        out.append((code, int(n.group(1).replace(",", "")) if n else None,
                    int(win.group(1).replace(",", "")) if win else None,
                    dl.group(3) if dl else "", dl.group(2) if dl else ""))
    return out


def series(html):
    svg = re.search(r'aria-label="Complaints per month" data-points="(\d+)" data-partial="(\d+)"', html)
    pts = [(m.group(1), int(m.group(2))) for m in
           re.finditer(r"<title>(2026-\d\d|20\d\d-\d\d)(?: \(to the \d+\w\w\))?: ([\d,]+)</title>", html)]
    return svg, pts


def case(path, q, expect_thin=False):
    print(f"\n== {path}")
    html = get(path)
    sec = html[html.find('<div class="mix">'):]
    conn = db(); cur = conn.cursor()

    cur.execute("SELECT MAX(review_date) hi FROM v_ai_complaints")
    hi = cur.fetchone()["hi"]

    # window: same rules as the app (explicit dates, else last 3 months present)
    d = {k: (v[0] if isinstance(v, list) else v) for k, v in q.items()}
    f = g = None
    if d.get("from") or d.get("to"):
        f = datetime.strptime(d["from"], "%Y-%m-%d").date() if d.get("from") else None
        g = datetime.strptime(d["to"], "%Y-%m-%d").date() if d.get("to") else None
    if f and g:
        pass
    else:
        w, a = where(d)
        cur.execute(f"SELECT DATE_FORMAT(v.review_date,'%%Y-%%m') m FROM v_ai_complaints v "
                    f"{(w + ' AND ') if w else 'WHERE '}v.final_complaint IS NOT NULL "
                    f"AND v.final_complaint <> 'Review Deletion' GROUP BY 1 ORDER BY 1", a)
        ms = [r["m"] for r in cur.fetchall()]
        f = date(int(ms[-3][:4]), int(ms[-3][5:]), 1)
        g = date(int(ms[-1][:4]), int(ms[-1][5:]), 1)
        nxt = date(g.year + (g.month == 12), (g.month % 12) + 1, 1)
        g = nxt - timedelta(days=1)
    if g > hi:
        g = hi
    span = (g - f).days + 1
    pt = f - timedelta(days=1)
    pf = pt - timedelta(days=span - 1)

    curw = dist(cur, d, f.isoformat(), g.isoformat())
    prior = dist(cur, d, pf.isoformat(), pt.isoformat())
    ct, ptot = sum(curw.values()), sum(prior.values())
    thin = ptot < 100 or ptot < 0.5 * ct

    rows = [r for r in mix_rows(html) if r[1] is not None]
    if not rows:
        fail(f"{path}: no mix rows parsed")
        return
    ok(f"{path}: parsed {len(rows)} mix rows; window {f}..{g} prev {pf}..{pt} "
       f"(cur {ct}, prior {ptot}, thin={thin})")
    if thin != expect_thin:
        fail(f"{path}: expected thin={expect_thin}, app window arithmetic says {thin}")

    for code, alltime, win, dtxt, dtitle in rows:
        if code in ("(no complaint assigned)", "Review Deletion") or " other complaints" in code:
            continue
        if win is not None and win != curw.get(code, 0):
            fail(f"{path}: {code} shows {win} in window, SQL says {curw.get(code, 0)}")
        if expect_thin:
            if dtxt.strip() != "—":
                fail(f"{path}: {code} shows '{dtxt}' in a thin window; must be '—'")
            continue
        cv, pv = curw.get(code, 0), prior.get(code, 0)
        want = "±0%" if pv and abs((cv - pv) * 100.0 / pv) < 1 else (
            ("▲%.0f%%" % abs((cv - pv) * 100.0 / pv)) if pv and cv > pv else
            ("▼%.0f%%" % abs((cv - pv) * 100.0 / pv)) if pv else ("new" if cv else ""))
        if pv == 0 and cv == 0:
            want = ""
        if dtxt.strip() != want:
            fail(f"{path}: {code} delta '{dtxt}' but {cv} vs {pv} = '{want}'")
        # the same percentage computed against the ALL-TIME count must NOT appear
        if pv and alltime != cv:
            bad = (alltime - pv) * 100.0 / pv
            if dtxt.strip() == f"{'▲' if bad > 0 else '▼'}{abs(bad):.0f}%":
                fail(f"{path}: {code} delta is all-time ({alltime}) vs prior ({pv}), not window ({cv})")
    if not expect_thin:
        ok(f"{path}: every change cell matches its own windowed counts")

    hdr = re.search(r'complaint · reviews · share of matches([^<]*)', sec)
    hdr = hdr.group(1) if hdr else ""
    if expect_thin and "change vs" in hdr:
        fail(f"{path}: change column header shown in a thin window")
    if expect_thin and ("▲" in sec.split('<div class="mixgrid">')[0] or
                        re.search(r'class="dl (up|down)"', sec.split('<div class="mixgrid">')[0])):
        fail(f"{path}: thin window still prints an arrow in the table")
    if expect_thin:
        ok(f"{path}: thin window renders '—' everywhere and drops the change header")

    # series == distribution minus noise/NULL, month by month
    w, a = where(d)
    ws = (w + " AND " if w else "WHERE ") + ("v.review_date IS NOT NULL AND v.final_complaint IS NOT NULL "
                                            "AND v.final_complaint <> 'Review Deletion'")
    cur.execute(f"SELECT DATE_FORMAT(v.review_date,'%%Y-%%m') m, COUNT(*) n FROM v_ai_complaints v {ws} "
                f"GROUP BY 1 ORDER BY 1", a)
    dbm = {r["m"]: int(r["n"]) for r in cur.fetchall()}
    svg, pts = series(html)
    if not pts:
        fail(f"{path}: no series points parsed")
    for m, n in pts:
        if dbm.get(m) != n:
            fail(f"{path}: series {m}={n}, SQL says {dbm.get(m)} (1,186-style gap)")
    ok(f"{path}: series {[(m, n) for m, n in pts]} = SQL, noise excluded")

    if hi.day < 28:  # not applicable
        pass
    partial = int(svg.group(2)) if svg else 0
    if partial and not re.search(r"is drawn dashed", sec):
        fail(f"{path}: partial month not labelled")
    if partial and f"(to the {partial}" not in sec:
        fail(f"{path}: partial month label missing '(to the N')")
    if hi.isoformat() not in html:
        fail(f"{path}: real data end {hi} not printed on the page")
    else:
        ok(f"{path}: data end {hi} printed; partial_day={partial}")

    # prose + validated disclosure
    if not re.search(r"of <b>[\d,]+</b> rows carry a customer comment", sec):
        fail(f"{path}: no 'x of y rows carry a customer comment' line")
    else:
        ok(f"{path}: prose disclosure present")
    if not re.search(r"of <b>[\d,]+</b> rows are human-validated", sec):
        fail(f"{path}: no 'N of M rows human-validated' line")
    else:
        ok(f"{path}: validated disclosure present")
    conn.close()


def main():
    case("/reviews?family=Seat+cushions", {"family": "Seat cushions"})
    case("/reviews?family=Seat+cushions&from=2026-09-01&to=2026-10-19",
         {"family": "Seat cushions", "from": "2026-09-01", "to": "2026-10-19"})
    case("/reviews?family=Seat+cushions&from=2026-07-01&to=2026-09-30",
         {"family": "Seat cushions", "from": "2026-07-01", "to": "2026-09-30"}, expect_thin=True)
    case("/reviews?family=Seat+cushions&tx=prose", {"family": "Seat cushions", "tx": "prose"})

    # riser must be the largest ABSOLUTE change, not the largest percentage
    html = get("/reviews?family=Seat+cushions&from=2026-09-01&to=2026-10-19")
    sec = html[html.find('<div class="mix">'):]
    m = re.search(r"Biggest increases? by review count[^<]*(.*?)\.</p>", sec, re.S)
    txt = re.sub(r"<[^>]+>", "", m.group(1)) if m else ""
    if not m:
        fail("riser: no 'Biggest increases' sentence")
    elif "Not Needed/Unwanted +214" not in txt:
        fail(f"riser: expected Not Needed/Unwanted +214 first, got '{txt[:120]}'")
    elif "Too Thin" in txt:
        fail(f"riser: noise code named: '{txt[:160]}'")
    else:
        ok(f"riser ranked by reviews: {txt.strip()[:120]}")

    # R2: the exact page from the review must not print ▲10200%
    html = get("/reviews?family=Seat+cushions&from=2026-07-01&to=2026-09-30")
    if re.search(r"▲\d{3,}%", html):
        fail("thin page still shows a 3+ digit arrow")
    else:
        ok("thin page: no ▲NNN% anywhere")

    print(f"\n{CHECKS} checks passed, {len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
