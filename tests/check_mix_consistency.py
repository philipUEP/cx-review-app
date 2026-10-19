#!/usr/bin/env python3
"""Check the complaint-mix panel against the database, over HTTP.

Every number is re-derived from SQL here, independently of app.py:

  * the change column is a change in RATE PER DAY WITH DATA — each window's
    counts divided by COUNT(DISTINCT review_date) for that same filter — so a
    window that holds 61 days of data is not compared as if it held 80;
  * the comparison window comes from the TRIMMED, contiguous months the chart
    draws, so one stray old row cannot stretch it over a year;
  * the partial month is decided by the FILTERED max(review_date), not the corpus
    max: a filter whose data stops on the 16th must be dashed on the 16th;
  * every toggle's count equals the page its link opens;
  * below the riser's floor (20 reviews or 1% of the window) a row shows no
    percentage at all;
  * repeats of the same review text on the same SKU/date are disclosed, and the
    'distinct complaints' toggle removes exactly those rows;
  * data/v3_output.csv is not stale relative to v_ai_complaints.

Usage:

    APP=127.0.0.1:8107 USER=chadia PASS=fix tests/check_mix_consistency.py
"""
import base64
import csv
import html as htmllib
import inspect
import math
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta

import pymysql

BASE = os.environ.get("APP", "127.0.0.1:8107")
AUTH = base64.b64encode(
    f"{os.environ.get('USER', 'chadia')}:{os.environ.get('PASS', 'fix')}".encode()).decode()

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
    with urllib.request.urlopen(req, timeout=180) as r:
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
    if q.get("product"):
        w.append("(v.product LIKE %s OR v.title LIKE %s)")
        a += [f"%{q['product']}%"] * 2
    if q.get("family"):
        w.append("v.product_family=%s"); a.append(q["family"])
    if q.get("parent"):
        w.append("v.parent=%s"); a.append(q["parent"])
    if q.get("sku"):
        w.append("v.sku LIKE %s"); a.append(f"%{q['sku']}%")
    if q.get("complaint"):
        w.append("v.final_complaint=%s"); a.append(q["complaint"])
    if q.get("source"):
        w.append("v.source=%s"); a.append(q["source"])
    if q.get("country"):
        w.append("v.country=%s"); a.append(q["country"])
    if q.get("month"):
        w.append("DATE_FORMAT(v.review_date,'%%Y-%%m')=%s"); a.append(q["month"])
    if q.get("from"):
        w.append("v.review_date >= %s"); a.append(q["from"])
    if q.get("to"):
        w.append("v.review_date <= %s"); a.append(q["to"])
    if q.get("status") == "decided":
        w.append("v.human_validated=1")
    elif q.get("status") == "flagged":
        w.append("v.needs_review=1 AND v.human_validated=0")
    elif q.get("status") == "new":
        w.append("v.human_validated=0")
    if q.get("tx") == "prose":
        w.append("v.has_prose=1")
    elif q.get("tx") == "codeonly":
        w.append("v.has_prose=0")
    if q.get("dup") == "1":
        w.append("v.dup_rank=1")
    return ("WHERE " + " AND ".join(w)) if w else "", a


def count(cur, q):
    w, a = where(q)
    cur.execute(f"SELECT COUNT(*) n FROM v_ai_complaints v {w}", a)
    return int(cur.fetchone()["n"])


def _win(q, a_, b_):
    qq = {k: v for k, v in q.items() if k not in ("from", "to", "month")}
    qq["from"], qq["to"] = a_.isoformat(), b_.isoformat()
    return where(qq)


def dist(cur, q, a_, b_):
    w, a = _win(q, a_, b_)
    cur.execute(f"SELECT COALESCE(v.final_complaint,'(none)') c, COUNT(*) n "
                f"FROM v_ai_complaints v {w} GROUP BY c", a)
    return {r["c"]: int(r["n"]) for r in cur.fetchall()}


def days_with_data(cur, q, a_, b_):
    """The change column's denominator: distinct review dates, same filter."""
    w, a = _win(q, a_, b_)
    cur.execute(f"SELECT COUNT(DISTINCT v.review_date) d FROM v_ai_complaints v {w}", a)
    return int(cur.fetchone()["d"] or 0)


def month_bounds(m):
    y, mo = int(m[:4]), int(m[5:7])
    first = date(y, mo, 1)
    nxt = date(y + (mo == 12), (mo % 12) + 1, 1)
    return first, nxt - timedelta(days=1)


def trim_months(months):
    """Re-implementation of app._trim_months: contiguous run holding >= 99.5%."""
    if len(months) <= 3:
        return months
    total = sum(n for _, n in months)
    best, acc, start = None, 0, 0
    for i, (_, n) in enumerate(months):
        acc += n
        while acc - months[start][1] >= total * 0.995 and start < i:
            acc -= months[start][1]
            start += 1
        if acc >= total * 0.995 and (best is None or (i - start) < (best[1] - best[0])):
            best = (start, i)
    return months[best[0]:best[1] + 1] if best else months


def series(cur, q):
    """(months, plot, lo, hi) for the filtered set the chart counts."""
    w, a = where(q)
    ws = (w + " AND " if w else "WHERE ") + (
        "v.review_date IS NOT NULL AND v.final_complaint IS NOT NULL "
        "AND v.final_complaint <> 'Review Deletion'")
    cur.execute(f"SELECT DATE_FORMAT(v.review_date,'%%Y-%%m') m, COUNT(*) n "
                f"FROM v_ai_complaints v {ws} GROUP BY 1 ORDER BY 1", a)
    months = [(r["m"], int(r["n"])) for r in cur.fetchall() if r["m"]]
    cur.execute(f"SELECT MIN(v.review_date) l, MAX(v.review_date) h FROM v_ai_complaints v {ws}", a)
    r = cur.fetchone()
    return months, trim_months(months), r["l"], r["h"]


def window(q, plot, lo, hi):
    f = datetime.strptime(q["from"], "%Y-%m-%d").date() if q.get("from") else None
    t = datetime.strptime(q["to"], "%Y-%m-%d").date() if q.get("to") else None
    if (f or t) and lo and hi:
        f, t = (f or lo), (t or hi)
    elif q.get("month"):
        f, t = month_bounds(q["month"])
    else:
        keys = [m for m, _ in plot][-3:]
        if not keys:
            return None, None
        f = month_bounds(keys[0])[0]
        t = month_bounds(keys[-1])[1]
    if hi and t > hi:
        t = hi
    if f > t:
        f = t
    return f, t


def delta_expected(cv, pv, cd, pd):
    if not cd or not pd:
        return "—"
    if not pv:
        return "new" if cv else ""
    dpct = ((cv / cd) / (pv / pd) - 1) * 100.0
    if abs(dpct) < 1:
        return "±0%"
    return ("▲" if dpct > 0 else "▼") + f"{abs(dpct):.0f}%"


ROW_RE = re.compile(r'<td class="c" title="([^"]*)">.*?<td class="n">([\d,]+)'
                    r'(?:<div class="meta">(?:all-time · )?([\d,]+) in window</div>)?'
                    r'.*?<td style="width:78px">(.*?)</td>', re.S)


def mix_rows(html):
    """[(code, all_time, in_window, delta_cell_html)] from the mix table."""
    sec = html[html.find('<div class="mix">'):]
    out = []
    for m in ROW_RE.finditer(sec):
        out.append((htmllib.unescape(m.group(1)), int(m.group(2).replace(",", "")),
                    int(m.group(3).replace(",", "")) if m.group(3) else None, m.group(4)))
    return out


def delta_text(cell):
    m = re.search(r'class="dl [^"]*"[^>]*>([^<]*)<', cell)
    if m:
        return htmllib.unescape(m.group(1)).strip()
    m = re.search(r'n(?:&lt;|<)(\d+)', cell)
    if m and 'class="meta"' in cell:
        return "n<" + m.group(1)
    return ""


class Case:
    def __init__(self, path, q, expect_thin=None):
        self.path, self.q, self.expect_thin = path, q, expect_thin


def case(c):
    path, q = c.path, c.q
    print(f"\n== {path}")
    page = get(path)
    sec = page[page.find('<div class="mix">'):]
    conn = db(); cur = conn.cursor()

    n_sql = count(cur, q)
    m = re.search(r'([\d,]+) reviews? match these filters', sec)
    if not m:
        fail(f"{path}: no 'N reviews match these filters' line")
        conn.close(); return
    if int(m.group(1).replace(",", "")) != n_sql:
        fail(f"{path}: page says {m.group(1)} rows match, SQL says {n_sql:,}")
    else:
        ok(f"{path}: {n_sql:,} rows match = SQL")

    months, plot, lo, hi = series(cur, q)
    f, t = window(q, plot, lo, hi)
    if not f:
        fail(f"{path}: could not derive a window")
        conn.close(); return
    span = (t - f).days + 1
    pt = f - timedelta(days=1)
    pf = pt - timedelta(days=span - 1)
    curw, prior = dist(cur, q, f, t), dist(cur, q, pf, pt)
    cd, pd = days_with_data(cur, q, f, t), days_with_data(cur, q, pf, pt)
    cur_total, prior_total = sum(curw.values()), sum(prior.values())
    thin = bool(prior_total) and (prior_total < 100 or prior_total < 0.5 * cur_total)
    daygap = bool(prior_total) and pd and cd and pd < 0.9 * cd
    floor = max(20, int(math.ceil(0.01 * cur_total))) if cur_total else 20

    ok(f"{path}: window {f}..{t} ({cd} days with data) vs {pf}..{pt} ({pd} days with data); "
       f"cur {cur_total:,} prior {prior_total:,}; thin={thin} daygap={daygap}")
    if c.expect_thin is not None and thin != c.expect_thin:
        fail(f"{path}: expected thin={c.expect_thin}, SQL arithmetic says {thin}")

    # --- the window, and the days of data on each side, are printed ----------
    if prior_total:
        if f"comparing <b>{f.isoformat()}</b> to <b>{t.isoformat()}</b>" not in sec:
            fail(f"{path}: header does not name the window {f}..{t}")
        elif f"({cd:,} days with data) against the {pd:,} days with data before" not in sec:
            fail(f"{path}: header does not state the days with data ({cd} vs {pd})")
        else:
            ok(f"{path}: header states the window and both days-with-data counts")
    else:
        if f"period {f.isoformat()} to {t.isoformat()}" not in sec:
            fail(f"{path}: no prior rows, and the header does not name the period {f}..{t}")
        else:
            ok(f"{path}: header names the period {f}..{t} (no prior rows to compare)")

    # --- the change column ---------------------------------------------------
    rows = mix_rows(page)
    if not rows:
        fail(f"{path}: no mix rows parsed")
        conn.close(); return
    delta_rows = [(code, k, win, cell) for code, k, win, cell in rows
                  if win is not None and code not in ("(no complaint assigned)", "Review Deletion")]
    checked = 0
    percent_rows = 0
    for code, k, win, cell in delta_rows:
        if win != curw.get(code, 0):
            fail(f"{path}: {code} shows {win} in window, SQL says {curw.get(code, 0)}")
        want = delta_text(cell)
        cv, pv = curw.get(code, 0), prior.get(code, 0)
        if thin:
            if want != "—":
                fail(f"{path}: {code} shows '{want}' in a thin window; must be '—'")
            continue
        if not prior_total:
            if want != "—":
                fail(f"{path}: {code} shows '{want}' with no prior rows to compare against")
            continue
        if max(cv, pv) < floor:
            if not want.startswith("n<"):
                fail(f"{path}: {code} {cv} vs {pv} is below the {floor} floor but shows '{want}'")
            continue
        exp = delta_expected(cv, pv, cd, pd)
        checked += 1
        if want.startswith(("▲", "▼", "±")):
            percent_rows += 1
        if want != exp:
            fail(f"{path}: {code} delta '{want}' but rates {cv}/{cd} vs {pv}/{pd} = '{exp}'")
        # the CALENDAR-day percentage must not be what is printed when the two
        # windows hold different numbers of days (that was the +86%/ +41.6% bug)
        if pv and cd != pd:
            wrong = ("▲" if cv > pv else "▼") + f"{abs((cv - pv) * 100.0 / pv):.0f}%"
            if want == wrong:
                fail(f"{path}: {code} prints the calendar-day change {wrong}, not the per-day rate "
                     f"({cd} vs {pd} days)")
    ok(f"{path}: {checked} change cells match their per-day SQL rate "
       f"(floor {floor:,}; {percent_rows} print a percentage)")

    if thin:
        if "change per day" in sec.split('<div class="mixgrid">')[0]:
            fail(f"{path}: change header shown in a thin window")
        if re.search(r'class="dl (up|down)"', sec):
            fail(f"{path}: thin window still prints an arrow")
        if "no percentage is shown" not in sec:
            fail(f"{path}: thin window does not explain itself")
        ok(f"{path}: thin window renders '—' and explains why")
    else:
        if prior_total and "change per day with data" not in sec:
            fail(f"{path}: change column does not say it is per day with data")
        if daygap and "PER DAY WITH DATA" not in sec:
            fail(f"{path}: prior window holds {pd} days of data against {cd} here (the day gap "
                 f"guard) and the page does not say the percentages are per day with data")
        elif daygap:
            ok(f"{path}: day-gap guard fired ({pd} vs {cd} days with data) and is stated")
        elif not prior_total:
            if "nothing to compare" not in sec:
                fail(f"{path}: no prior rows at all, and the page does not say so")
            else:
                ok(f"{path}: no earlier rows — the page says there is nothing to compare")

    # --- series, partial month, filtered max --------------------------------
    svg = re.search(r'data-points="(\d+)" data-partial="(\d+)"', sec)
    pts = [(m.group(1), int(m.group(2).replace(",", ""))) for m in
           re.finditer(r"<title>(20\d\d-\d\d)(?: \(to the \d+\w\w\))?: ([\d,]+)</title>", sec)]
    if not svg:
        fail(f"{path}: no series parsed")
    else:
        if int(svg.group(1)) != len(plot):
            fail(f"{path}: chart has {svg.group(1)} points, trimmed SQL series has {len(plot)}")
        dbm = dict(months)
        for mm, nn in pts:
            if dbm.get(mm) != nn:
                fail(f"{path}: series {mm}={nn}, SQL says {dbm.get(mm)} (noise/undeclared gap)")
        want_part = 0
        if plot and hi and plot[-1][0] == f"{hi.year:04d}-{hi.month:02d}" \
                and hi.day < month_bounds(plot[-1][0])[1].day:
            want_part = hi.day
        got_part = int(svg.group(2))
        if got_part != want_part:
            fail(f"{path}: data-partial={got_part}, the FILTERED max {hi} says {want_part}")
        elif want_part and len(plot) > 1:
            if f"(to the {want_part}" not in sec:
                fail(f"{path}: partial month label '(to the {want_part}' missing")
            if "drawn dashed" not in sec:
                fail(f"{path}: partial month not explained")
            ok(f"{path}: last month dashed at the FILTERED end ({hi}), not the corpus end")
        elif want_part:
            ok(f"{path}: one month only ({hi} mid-month) — nothing to dash")
        else:
            ok(f"{path}: no partial month (this filter's data reaches {hi})")
        if hi and hi.isoformat() not in sec:
            fail(f"{path}: filtered data end {hi} not printed in the mix")
        elif hi:
            ok(f"{path}: filtered data end {hi} printed")

    # --- prose / validated disclosure ---------------------------------------
    if not re.search(r"of <b>[\d,]+</b> rows carry a customer comment", sec):
        fail(f"{path}: no 'x of y rows carry a customer comment' line")
    if not re.search(r"of <b>[\d,]+</b> rows are human-validated", sec):
        fail(f"{path}: no 'N of M rows human-validated' line")
    ok(f"{path}: prose and validated disclosures present")

    # --- duplicates are disclosed, never silently dropped --------------------
    wq, aq = where(q)
    cur.execute(f"SELECT COALESCE(SUM(v.dup_rank > 1),0) n FROM v_ai_complaints v {wq}", aq)
    dup = int(cur.fetchone()["n"] or 0)
    if q.get("dup") != "1":
        mm = re.search(r'<b>([\d,]+)</b> rows? repeat', sec)
        if dup and not mm:
            fail(f"{path}: {dup} rows repeat a review but the footer does not say so")
        elif dup and int(mm.group(1).replace(",", "")) != dup:
            fail(f"{path}: footer says {mm.group(1)} repeat, SQL says {dup}")
        elif dup:
            ok(f"{path}: {dup:,} repeat rows disclosed and counted (not removed)")
    else:
        mm = re.search(r'<b>([\d,]+)</b> rows? that repeat', sec)
        if not mm:
            fail(f"{path}: distinct view does not say how many rows it hides")
        else:
            ok(f"{path}: distinct view names the {mm.group(1)} rows it hides")

    # --- toggles: every count equals the page its link opens -----------------
    tog = re.search(r'<div class="txtog">(.*?)</div>', sec, re.S)
    if not tog:
        fail(f"{path}: no toggle strip")
    else:
        links = re.findall(r'<a href="([^"]+)"[^>]*>([^<]+)</a>', tog.group(1))
        bad = 0
        for href, label in links:
            href = htmllib.unescape(href)
            tok = re.findall(r"([\d,]+)$", label.strip())
            if not tok:
                fail(f"{path}: toggle '{label}' carries no count")
                bad += 1
                continue
            shown = int(tok[0].replace(",", ""))
            target = get(href)
            tsec = target[target.find('<div class="mix">'):]
            if "Nothing matches those filters" in tsec:
                got = 0
            else:
                m2 = re.search(r'([\d,]+) reviews? match these filters', tsec)
                got = int(m2.group(1).replace(",", "")) if m2 else None
            if got != shown:
                fail(f"{path}: toggle '{label.strip()}' shows {shown:,} but {href} returns "
                     f"{'nothing' if got is None else format(got, ',')}")
                bad += 1
        if not bad:
            ok(f"{path}: all {len(links)} toggle counts = the page each link returns")

    conn.close()


def check_riser(path):
    """The 'biggest increase' sentence must use the same rates as the column."""
    print(f"\n== riser {path}")
    page = get(path)
    sec = page[page.find('<div class="mix">'):]
    m = re.search(r"Biggest increases? by review count against the ([\d,]+) days with data before:"
                  r"(.*?)\.</p>", sec, re.S)
    if not m:
        fail("riser: no 'Biggest increases … days with data before' sentence")
        return
    txt = htmllib.unescape(re.sub(r"<[^>]+>", "", m.group(2)))
    if "Too Thin" in txt:
        fail(f"riser names the noise code: '{txt[:160]}'")
    parts = re.findall(r"([A-Za-z][^+·]*?) \+([\d,]+) \(([+-]?\d+%|new)\)", txt)
    if not parts:
        fail(f"riser: could not parse '{txt[:160]}'")
        return
    counts = [int(p[1].replace(",", "")) for p in parts]
    if counts != sorted(counts, reverse=True):
        fail(f"riser: not ranked by review count: {counts}")
    else:
        ok(f"riser: ranked by review count {counts} · {txt.strip()[:110]}")


def check_units():
    """sample_stats() must keep code-only rows out of the agreement denominator."""
    print("\n== quality sample arithmetic")
    sys.path.insert(0, REPO)
    try:
        import app
    except Exception as e:                                    # noqa: BLE001
        fail(f"could not import app for the unit check: {e}")
        return
    rows = [
        {"your_call": "Too big", "ai_complaint": "Too big", "has_prose": 1, "txt": "x" * 30},
        {"your_call": "Too big", "ai_complaint": "Too big", "has_prose": 1, "txt": "x" * 30},
        {"your_call": "Too big", "ai_complaint": "Too small", "has_prose": 0, "txt": "Too big"},
    ]
    acc, cor, rej, n, skipped = app.sample_stats(rows)
    if (acc, cor, n, skipped) != (2, 0, 2, 1):
        fail(f"sample_stats: expected (2,0,2,1), got ({acc},{cor},{n},{skipped}) — a code-only "
             f"row is inside the agreement denominator")
    else:
        ok("sample_stats: code-only rows leave the denominator and are counted (skipped=1)")
    if "has_prose=1" not in inspect.getsource(app.ensure_sample):
        fail("ensure_sample: the sample pool does not filter has_prose=1")
    else:
        ok("ensure_sample draws only from rows with a customer comment")


def check_views(cur):
    """The duplicate flag and the SKU titles must be what the pages rely on."""
    print("\n== view flags")
    cur.execute("""SELECT COUNT(*) n FROM v_ai_complaints
                    WHERE dup_rank < 1 OR (dup_group > 1 AND has_prose = 0)""")
    bad = int(cur.fetchone()["n"])
    if bad:
        fail(f"duplicate flag: {bad} row(s) wrong (picklist row in a group, or dup_rank < 1)")
    else:
        cur.execute("SELECT COALESCE(SUM(dup_rank > 1),0) n FROM v_ai_complaints")
        ok(f"duplicate flag: picklist rows never grouped; {int(cur.fetchone()['n']):,} repeats flagged")
    cur.execute("""SELECT COUNT(*) n FROM v_ai_complaints_monthly
                    WHERE title IS NOT NULL AND TRIM(title) <> ''
                      AND (title_short IS NULL OR TRIM(title_short) = '')""")
    bad = int(cur.fetchone()["n"])
    if bad:
        fail(f"title_short: {bad} monthly row(s) have a title but no short title")
    else:
        cur.execute("SELECT title_short FROM v_ai_complaints_monthly WHERE sku='6' LIMIT 1")
        ts = cur.fetchone()
        if not ts or "Screen Door" not in (ts["title_short"] or ""):
            fail(f"title_short for sku '6' does not name the screen door: {ts}")
        else:
            ok(f"title_short: sku '6' reads '{ts['title_short']}'")


def check_csv(cur):
    """W9: a stale data/v3_output.csv must fail here, not age quietly."""
    print("\n== v3_output.csv")
    path = os.path.join(REPO, "data", "v3_output.csv")
    cur.execute("SELECT COUNT(*) n, COUNT(DISTINCT review_id) d FROM v_ai_complaints")
    r = cur.fetchone()
    n_db, n_ids = int(r["n"]), int(r["d"])
    if not os.path.exists(path):
        fail("data/v3_output.csv is missing")
        return
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if len(rows) != n_db:
        fail(f"data/v3_output.csv is STALE: {len(rows):,} rows vs {n_db:,} in v_ai_complaints "
             f"({n_db - len(rows):+,}) — run sync_v3_output.py")
        return
    ids = {(r.get("review_id") or "").strip() for r in rows}
    if len(ids) != n_ids:
        fail(f"data/v3_output.csv holds {len(ids):,} distinct ids, DB has {n_ids:,}")
    else:
        ok(f"data/v3_output.csv: {len(rows):,} rows = v_ai_complaints ({n_ids:,} ids)")


def main():
    conn = db(); cur = conn.cursor()
    check_views(cur)

    # default: three months, a prior window 19 days short of the data
    case(Case("/reviews", {}))
    case(Case("/reviews?family=Seat+cushions", {"family": "Seat cushions"}))
    # filters whose data STOPS mid-month (the corpus runs to 2026-10-19)
    case(Case("/reviews?source=ncx", {"source": "ncx"}))
    case(Case("/reviews?sku=SC-XL-B", {"sku": "SC-XL-B"}))
    # a month filter: equal windows of days with data on both sides
    case(Case("/reviews?family=Seat+cushions&month=2026-10",
              {"family": "Seat cushions", "month": "2026-10"}))
    # explicit dates, then a thin prior window
    case(Case("/reviews?family=Seat+cushions&from=2026-09-01&to=2026-10-19",
              {"family": "Seat cushions", "from": "2026-09-01", "to": "2026-10-19"}))
    case(Case("/reviews?family=Seat+cushions&from=2026-07-01&to=2026-09-30",
              {"family": "Seat cushions", "from": "2026-07-01", "to": "2026-09-30"},
              expect_thin=True))
    # a status filter: the toggle counts must carry the status through
    case(Case("/reviews?family=Seat+cushions&status=flagged",
              {"family": "Seat cushions", "status": "flagged"}))
    # the distinct-complaints view
    case(Case("/reviews?family=Seat+cushions&dup=1",
              {"family": "Seat cushions", "dup": "1"}))

    check_riser("/reviews?family=Seat+cushions")
    check_units()
    check_csv(cur)

    # the exact shape of W6: a small code must not print a big arrow
    page = get("/reviews?family=Seat+cushions&month=2026-10")
    small = [(c, k, delta_text(cell)) for c, k, win, cell in mix_rows(page)
             if win is not None and k < 20 and delta_text(cell).startswith(("▲", "▼"))]
    if small:
        fail(f"month=2026-10: sub-floor rows still print a percentage: {small[:3]}")
    else:
        ok("month=2026-10: no sub-floor row prints a percentage (n<20 instead of ▲500%)")

    conn.close()
    print(f"\n{CHECKS} checks passed, {len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
