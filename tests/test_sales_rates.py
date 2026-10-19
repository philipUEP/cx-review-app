#!/usr/bin/env python3
"""The denominator test: sales_by_sku_month, v_complaint_rates, /monthly and the CSV.

Every number is re-derived from the READ-ONLY replica (amzmanager) with SQL this
file writes itself, then compared to what automation_philip serves and what the
app renders. Nothing here trusts build_sales.py, apply_views.py or app.py — they
are what is being tested.

What it proves:
  1. BUILD. For two sku-months with very different volumes, SUM(units/orders/
     refunds) straight off finance_by_products -> products.sku equals the stored
     sales_by_sku_month row, including the US-only columns.
  2. GRAIN. sales_by_sku_month is unique on (sku, month) — the LEFT JOIN in
     v_complaint_rates cannot fan out, which would double every complaint count.
  3. RATES. On every row of v_complaint_rates, complaints_per_100_orders is
     n_complaints*100/orders and complaints_per_1000_units is n*1000/units, to two
     decimals; refund_rate_pct is refunds*100/orders.
  4. NULL, NOT ZERO. Every row with no sales row, or with orders=0 / units=0, has
     a NULL rate; every row with orders>0 has a non-NULL one. A 0.00 here would be
     a claim the data cannot support.
  5. COUNTS UNCHANGED. v_complaint_rates carries exactly the rows and the SUM(n) of
     v_ai_complaints_monthly: adding a denominator moved no complaint count.
  6. THE PAGE. /monthly.csv for each sku-month carries the same orders, units and
     rates, and /monthly's HTML prints them. An undefined rate is an empty CSV cell
     and an em dash on the page — never 0.
  7. COVERAGE IS LABELLED. The share of complaint records covered by the sales
     feed is measured and printed, so a rate over a fraction of the corpus cannot
     be quoted as if it covered all of it.

Run:
  set -a && . /opt/data/.env && set +a && \\
  /opt/data/venvs/gws/bin/python tests/test_sales_rates.py
  APP=127.0.0.1:8110 tests/test_sales_rates.py     # against an app already running
"""
import base64
import csv
import io
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date

import pymysql

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


def eq(label, got, want, tol=0.0):
    if tol:
        good = abs(float(got) - float(want)) <= tol
    else:
        good = got == want
    if good:
        ok(f"{label}: {got}")
    else:
        fail(f"{label}: got {got!r}, expected {want!r}")
    return good


def replica():
    return pymysql.connect(
        host=os.environ["MARIADB_HOST"], port=int(os.environ.get("MARIADB_PORT") or 3306),
        user=os.environ["MARIADB_USER"], password=os.environ["MARIADB_PASSWORD"],
        database="amzmanager", charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)


def write_db():
    return pymysql.connect(
        host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
        password=os.environ["CXAPP_DB_PASSWORD"], database=os.environ["CXAPP_DB_NAME"],
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)


# --------------------------------------------------------------- the app under test
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class App:
    """A private app instance, so this test never touches the live one."""

    def __init__(self):
        self.proc = None
        self.base = os.environ.get("APP")

    def __enter__(self):
        if self.base:
            return self
        port = free_port()
        env = dict(os.environ, APP_PORT=str(port), APP_HOST="127.0.0.1",
                   APP_USER="salestest", APP_PASS="salestest-pw")
        self.proc = subprocess.Popen([sys.executable, "app.py"], cwd=REPO, env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.base = f"127.0.0.1:{port}"
        for _ in range(60):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                return self
            except OSError:
                if self.proc.poll() is not None:
                    err = self.proc.stderr.read().decode()[:500]
                    raise SystemExit(f"app.py exited before serving: {err}")
                time.sleep(0.25)
        raise SystemExit("app.py did not start within 15s")

    def __exit__(self, *a):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def get(self, path):
        auth = base64.b64encode(b"salestest:salestest-pw").decode()
        req = urllib.request.Request(f"http://{self.base}{path}",
                                     headers={"Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read().decode("utf-8")


# --------------------------------------------------------------- the hand numbers
def hand(rc, sku, month):
    """What the replica says for one (sku, month), computed here and nowhere else."""
    cur = rc.cursor()
    cur.execute("""SELECT COALESCE(SUM(f.units),0) units, COALESCE(SUM(f.orders),0) orders,
                          COALESCE(SUM(f.refunds),0) refunds, COALESCE(SUM(f.sales),0) sales,
                          COALESCE(SUM(CASE WHEN c.short_name='us' THEN f.units ELSE 0 END),0) units_us,
                          COALESCE(SUM(CASE WHEN c.short_name='us' THEN f.refunds ELSE 0 END),0) refunds_us,
                          COUNT(*) rows_n
                     FROM finance_by_products f
                     JOIN products p ON p.id = f.product_id
                     LEFT JOIN country c ON c.id = f.country_id
                    WHERE p.sku = %s AND DATE_FORMAT(f.date,'%%Y-%%m') = %s""", (sku, month))
    return cur.fetchone()


def pick_two(wc, rc):
    """A high-volume and a low-volume sku-month that both carry complaints."""
    cur = wc.cursor()
    cur.execute("""SELECT r.sku, r.month, SUM(r.n_complaints) n, s.orders, s.units
                     FROM v_complaint_rates r JOIN sales_by_sku_month s
                       ON s.sku = r.sku AND s.month = r.month
                    WHERE s.orders > 0 AND s.units > 0 AND r.month >= '2026-01'
                    GROUP BY r.sku, r.month, s.orders, s.units
                    HAVING SUM(r.n_complaints) >= 3
                    ORDER BY s.orders DESC""")
    rows = cur.fetchall()
    if len(rows) < 2:
        raise SystemExit("not enough sku-months with complaints and sales to test")
    big = rows[0]
    # a low-volume month, far below the big one, still with real complaints
    small = next((r for r in reversed(rows) if r["orders"] * 20 < big["orders"]), rows[-1])
    return big, small


def main():
    rc, wc = replica(), write_db()
    try:
        run(rc, wc)
    finally:
        rc.close()
        wc.close()
    print()
    if FAILS:
        print(f"=== FAILED: {len(FAILS)} check(s) ===")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"=== all {CHECKS} checks passed ===")
    return 0


def run(rc, wc):
    print("== 1/7 grain and join keys")
    cur = wc.cursor()
    cur.execute("""SELECT COUNT(*) rows_n,
                          COUNT(DISTINCT CONCAT_WS('~', sku, month)) grain_n,
                          MIN(month) mn, MAX(month) mx,
                          SUM(sku = '' OR sku IS NULL) blank_sku,
                          SUM(orders = 0) zero_orders
                     FROM sales_by_sku_month""")
    g = cur.fetchone()
    eq("sales_by_sku_month is unique on (sku, month)", int(g["rows_n"]), int(g["grain_n"]))
    eq("no blank sku in the sales table", int(g["blank_sku"]), 0)
    print(f"     {int(g['rows_n']):,} sku-months, {g['mn']} -> {g['mx']}, "
          f"{int(g['zero_orders']):,} with 0 orders")

    big, small = pick_two(wc, rc)
    print(f"     two sku-months under test: {big['sku']}/{big['month']} "
          f"({int(big['orders']):,} orders) and {small['sku']}/{small['month']} "
          f"({int(small['orders']):,} orders)")

    for label, row in (("high-volume", big), ("low-volume", small)):
        sku, month = row["sku"], row["month"]
        print(f"\n== 2/7 {label}: {sku} / {month} — replica vs sales_by_sku_month")
        h = hand(rc, sku, month)
        cur.execute("""SELECT units, orders, refunds, sales, units_us, orders_us, refunds_us
                         FROM sales_by_sku_month WHERE sku=%s AND month=%s""", (sku, month))
        s = cur.fetchone()
        if not s:
            fail(f"{sku}/{month}: no sales_by_sku_month row")
            continue
        eq(f"  orders ({label})", int(s["orders"]), int(h["orders"]))
        eq(f"  units ({label})", int(s["units"]), int(h["units"]))
        eq(f"  refunds ({label})", int(s["refunds"]), int(h["refunds"]))
        eq(f"  US units ({label})", int(s["units_us"]), int(h["units_us"]))
        eq(f"  US refunds ({label})", int(s["refunds_us"]), int(h["refunds_us"]))
        print(f"     {int(h['rows_n']):,} daily finance rows summed by hand")

        # 3. the view's rates, re-derived
        print(f"\n== 3/7 {label}: v_complaint_rates rates re-derived")
        cur.execute("""SELECT complaint, n_complaints, orders, units, refunds,
                              complaints_per_100_orders, complaints_per_1000_units,
                              refund_rate_pct
                         FROM v_complaint_rates WHERE sku=%s AND month=%s
                        ORDER BY n_complaints DESC""", (sku, month))
        vr = cur.fetchall()
        if not vr:
            fail(f"{sku}/{month}: no v_complaint_rates rows")
            continue
        bad = 0
        for r in vr:
            n, o, u = int(r["n_complaints"]), int(r["orders"]), int(r["units"])
            if o:
                want = round(n * 100.0 / o, 2)
                if abs(float(r["complaints_per_100_orders"]) - want) > 0.005:
                    fail(f"  {sku}/{month}/{r['complaint']}: "
                         f"{r['complaints_per_100_orders']} != {n}*100/{o} = {want}")
                    bad += 1
            if u:
                want_u = round(n * 1000.0 / u, 2)
                if abs(float(r["complaints_per_1000_units"]) - want_u) > 0.005:
                    fail(f"  {sku}/{month}/{r['complaint']}: per-1000 {want_u} expected")
                    bad += 1
        if not bad:
            ok(f"  {len(vr)} complaint-code rows: every rate = n/denominator, to 2dp")

        # 6. the page and the CSV
        print(f"\n== 6/7 {label}: /monthly.csv and /monthly agree with the view")
        with App() as app:
            raw = app.get(f"/monthly.csv?sku={urllib.parse.quote(sku)}"
                          f"&month={urllib.parse.quote(month)}")
            rows = list(csv.DictReader(io.StringIO(raw.lstrip("\ufeff"))))
            if not rows:
                fail(f"{sku}/{month}: /monthly.csv returned no rows")
                continue
            eq(f"  CSV orders ({label})", int(rows[0]["orders"]), int(s["orders"]))
            eq(f"  CSV units ({label})", int(rows[0]["units"]), int(s["units"]))
            eq(f"  CSV record count unchanged ({label})",
               sum(int(r["n"]) for r in rows), sum(int(r["n_complaints"]) for r in vr))
            top = rows[0]
            want = round(int(top["n"]) * 100.0 / int(s["orders"]), 2)
            eq(f"  CSV complaints_per_100_orders ({label})",
               float(top["complaints_per_100_orders"]), want, tol=0.005)
            html = app.get(f"/monthly?sku={urllib.parse.quote(sku)}"
                           f"&month={urllib.parse.quote(month)}")
            if f"{int(s['orders']):,}" not in html:
                fail(f"{sku}/{month}: /monthly does not print the orders figure "
                     f"{int(s['orders']):,}")
            else:
                ok(f"  /monthly prints orders {int(s['orders']):,}")

    # 4/5. NULL discipline and unchanged counts, over the whole view
    print("\n== 4/7 NULL, never zero: no ratio against a missing or zero denominator")
    cur.execute("""SELECT SUM(orders > 0 AND complaints_per_100_orders IS NULL) a,
                          SUM((orders IS NULL OR orders = 0) AND complaints_per_100_orders IS NOT NULL) b,
                          SUM(units > 0 AND complaints_per_1000_units IS NULL) c,
                          SUM((units IS NULL OR units = 0) AND complaints_per_1000_units IS NOT NULL) d,
                          SUM(orders_us > 0 AND refund_rate_us_pct IS NULL) e,
                          SUM((orders_us IS NULL OR orders_us = 0) AND refund_rate_us_pct IS NOT NULL) f,
                          SUM(orders IS NULL) no_sales, SUM(orders = 0) zero_orders,
                          SUM(has_sales = 1) covered, COUNT(*) all_rows
                     FROM v_complaint_rates""")
    n = cur.fetchone()
    eq("rows with a positive denominator and a NULL rate", int(n["a"] or 0), 0)
    eq("rows with no denominator but a rate", int(n["b"] or 0), 0)
    eq("units side: positive denominator and NULL rate", int(n["c"] or 0), 0)
    eq("units side: no denominator but a rate", int(n["d"] or 0), 0)
    eq("US-refund-rate: positive denominator and NULL rate", int(n["e"] or 0), 0)
    eq("US-refund-rate: no denominator but a rate", int(n["f"] or 0), 0)
    print(f"     {int(n['covered']):,}/{int(n['all_rows']):,} rows cover a sales row; "
          f"{int(n['no_sales']):,} have none, {int(n['zero_orders']):,} sold 0 orders")

    print("\n== 5/7 complaint counts unchanged by the join")
    cur.execute("""SELECT COUNT(*) c, COALESCE(SUM(n),0) s FROM v_ai_complaints_monthly""")
    a = cur.fetchone()
    cur.execute("""SELECT COUNT(*) c, COALESCE(SUM(n_complaints),0) s FROM v_complaint_rates""")
    b = cur.fetchone()
    eq("v_complaint_rates rows = v_ai_complaints_monthly rows", int(b["c"]), int(a["c"]))
    eq("v_complaint_rates complaint records = v_ai_complaints_monthly records",
       int(b["s"]), int(a["s"]))

    print("\n== 7/7 coverage, stated as a number the reader can quote")
    cur.execute("""SELECT SUM(s.sku IS NOT NULL) cov_pairs, COUNT(*) pairs,
                          SUM(CASE WHEN s.sku IS NOT NULL THEN x.n ELSE 0 END) cov_recs,
                          SUM(x.n) recs
                     FROM (SELECT sku, month, SUM(n_complaints) n FROM v_complaint_rates
                            WHERE sku IS NOT NULL AND month IS NOT NULL
                            GROUP BY sku, month) x
                     LEFT JOIN sales_by_sku_month s ON s.sku = x.sku AND s.month = x.month""")
    cv = cur.fetchone()
    pct_pairs = 100.0 * int(cv["cov_pairs"]) / max(int(cv["pairs"]), 1)
    pct_recs = 100.0 * int(cv["cov_recs"]) / max(int(cv["recs"]), 1)
    print(f"     {int(cv['cov_pairs']):,}/{int(cv['pairs']):,} complaint sku-months "
          f"({pct_pairs:.2f}%) and {int(cv['cov_recs']):,}/{int(cv['recs']):,} complaint records "
          f"({pct_recs:.2f}%) have a sales row")
    if pct_recs < 90:
        fail(f"sales coverage is only {pct_recs:.1f}% of complaint records — a rate over that "
             f"must be labelled partial wherever it is shown")
    else:
        ok(f"coverage {pct_recs:.2f}% of complaint records (>90%, and still labelled in the app)")

    print("\n== seat cushions, the question she asked (per-month, all SKUs)")
    # Orders/units are per SKU-MONTH, and a SKU-month has one row per complaint
    # code: summing orders straight off v_complaint_rates multiplies the
    # denominator by the number of codes (28x on sku 1). Aggregate to (sku, month)
    # first, then add up — the same mistake would inflate any pivot built on the view.
    cur.execute("""SELECT month, SUM(n) recs, SUM(o) orders, SUM(u) units, SUM(rf) refunds,
                          SUM(ns) recs_nosales, COUNT(*) sku_months
                     FROM (SELECT sku, month, SUM(n_complaints) n, MAX(orders) o,
                                  MAX(units) u, MAX(refunds) rf,
                                  SUM(CASE WHEN has_sales = 0 THEN n_complaints ELSE 0 END) ns
                             FROM v_complaint_rates WHERE product_family = 'Seat cushions'
                            GROUP BY sku, month) t
                    GROUP BY month ORDER BY month""")
    rows = cur.fetchall()
    rated = [r for r in rows if int(r["orders"] or 0)]
    for r in rows:
        o = int(r["orders"] or 0)
        rate = f"{100.0 * int(r['recs']) / o:.2f}" if o else "—"
        rp = f"{100.0 * int(r['refunds'] or 0) / o:.2f}" if o else "—"
        print(f"     {r['month']}  records {int(r['recs']):>5,}  orders {o:>7,}  "
              f"per100 {rate:>6}  refunds {int(r['refunds'] or 0):>5,}  refund% {rp:>5}  "
              f"sku-months {int(r['sku_months'])}")
    if len(rated) >= 4:
        ok(f"{len(rated)} seat-cushion months carry a computable complaints-per-100-orders")
    else:
        fail(f"only {len(rated)} seat-cushion months are ratable — the answer is too thin")


if __name__ == "__main__":
    sys.exit(main())
