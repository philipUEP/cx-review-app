#!/usr/bin/env python3
"""The denominator test: sales_by_sku_month, the rate views, /monthly and both CSVs.

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
     decimals.
  4. NULL, NOT ZERO, AND NOT BELOW THE FLOOR. Every row with no sales row or 0
     orders has a NULL rate; every row at or above the floor has one. A refund
     ratio below 100 US orders is NULL and flagged, so 54 refunds on 2 orders can
     never print 2700% beside ordinary rows (R12).
  5. COUNTS UNCHANGED. v_complaint_rates carries exactly the rows and the SUM(n) of
     v_ai_complaints_monthly. Same for the two new views.
  6. THE PAGE AND BOTH CSVs. /monthly prints orders and a US return rate;
     /monthly.csv carries a SKU-month's denominator ONCE (R1) so SUM(orders) is
     the true total; /monthly_sku.csv is one row per SKU-month and its sums equal
     the family denominator (R1).
  7. THE FAMILY DENOMINATOR IS NOT CONDITIONED ON THE COMPLAINT (R2). August's
     family denominator is 39,390 orders, not the 38,513 of the SKU-months that
     recorded a complaint, so the family rate is 0.53 and not 0.54; and a single
     code's rate is quoted over the family, with the conditional figure labelled.
  8. ZERO-ORDER MONTHS DO NOT LEAK INTO THE NUMERATOR (R3): /monthly?sku=S-Hum
     says "too few to quote a rate" instead of 200.00 per 100 orders.
  9. THE FEED IS PART OF THE RATE (R4): the per-feed table exists, fba is the only
     series present in every month, and the page states that a month-over-month
     comparison is valid only within one feed.

Run:
  set -a && . /opt/data/.env && set +a && \\
  /opt/data/venvs/gws/bin/python tests/test_sales_rates.py
  APP=127.0.0.1:8110 tests/test_sales_rates.py     # against an app already running
"""
import base64
import csv
import io
import os
import re
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
FLOOR = 100


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
        u = os.environ.get("APP_USER", "salestest")
        p = os.environ.get("APP_PASS", "salestest-pw")
        self._auth = base64.b64encode(f"{u}:{p}".encode()).decode()
        req = urllib.request.Request(f"http://{self.base}{path}",
                                     headers={"Authorization": f"Basic {self._auth}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read().decode("utf-8")

    def csv(self, path):
        raw = self.get(path).lstrip("\ufeff")
        return list(csv.DictReader(io.StringIO(raw)))


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


def hand_family(rc, skus, month):
    """The family denominator re-derived from the replica for a whole SKU set.

    This is the number the R2 fix turns on: it counts EVERY SKU the corpus
    attributes to the family, whether or not that SKU recorded a complaint in the
    month. Summing only the complaining SKUs' orders is the defect.
    """
    cur = rc.cursor()
    ph = ",".join(["%s"] * len(skus))
    cur.execute(f"""SELECT COALESCE(SUM(f.orders),0) orders, COALESCE(SUM(f.units),0) units,
                           COALESCE(SUM(CASE WHEN c.short_name='us' THEN f.refunds ELSE 0 END),0) refunds_us,
                           COALESCE(SUM(CASE WHEN c.short_name='us' THEN f.orders ELSE 0 END),0) orders_us
                      FROM finance_by_products f
                      JOIN products p ON p.id = f.product_id
                      LEFT JOIN country c ON c.id = f.country_id
                     WHERE p.sku IN ({ph}) AND DATE_FORMAT(f.date,'%%Y-%%m') = %s""",
                skus + [month])
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
    print("== 1/9 grain and join keys")
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
        print(f"\n== 2/9 {label}: {sku} / {month} — replica vs sales_by_sku_month")
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

        print(f"\n== 3/9 {label}: v_complaint_rates rates re-derived")
        cur.execute("""SELECT complaint, n_complaints, orders, units, refunds,
                              complaints_per_100_orders, complaints_per_1000_units,
                              rate_quotable
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
            if int(r["rate_quotable"]) != int(o >= FLOOR):
                fail(f"  {sku}/{month}/{r['complaint']}: rate_quotable={r['rate_quotable']} "
                     f"for {o} orders (floor {FLOOR})")
                bad += 1
        if not bad:
            ok(f"  {len(vr)} complaint-code rows: every rate = n/denominator, to 2dp; "
               f"quotable flags agree with the floor")

        print(f"\n== 6/9 {label}: /monthly, /monthly.csv and /monthly_sku.csv")
        with App() as app:
            rows = app.csv(f"/monthly.csv?sku={urllib.parse.quote(sku)}"
                           f"&month={urllib.parse.quote(month)}")
            if not rows:
                fail(f"{sku}/{month}: /monthly.csv returned no rows")
                continue
            eq(f"  CSV orders ({label})", int(rows[0]["orders"]), int(s["orders"]))
            eq(f"  CSV units ({label})", int(rows[0]["units"]), int(s["units"]))
            eq(f"  CSV record count unchanged ({label})",
               sum(int(r["n"]) for r in rows), sum(int(r["n_complaints"]) for r in vr))
            # R1: the denominator rides on ONE row per SKU-month.
            eq(f"  CSV carries the denominator exactly once ({label})",
               sum(int(r["exposure_row"]) for r in rows), 1)
            eq(f"  CSV SUM(orders) is not multiplied by the code count ({label})",
               sum(int(r["orders"]) for r in rows if r["orders"]), int(s["orders"]))
            top = next(r for r in rows if r["orders"])
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
            if "US return rate" not in html:
                fail(f"{sku}/{month}: /monthly has no 'US return rate' column (R7)")
            else:
                ok("  /monthly carries the US return rate column")
            sku_rows = app.csv(f"/monthly_sku.csv?sku={urllib.parse.quote(sku)}"
                               f"&month={urllib.parse.quote(month)}")
            if len(sku_rows) != 1:
                fail(f"{sku}/{month}: /monthly_sku.csv returned {len(sku_rows)} rows for one "
                     f"SKU-month — it must be exactly one (R1)")
            else:
                eq(f"  sku-month CSV orders ({label})", int(sku_rows[0]["orders"]),
                   int(s["orders"]))
                eq(f"  sku-month CSV complaint count ({label})",
                   int(sku_rows[0]["n_complaints"]), sum(int(r["n_complaints"]) for r in vr))

    # 4/5. NULL discipline, the floor, and unchanged counts
    print("\n== 4/9 NULL, never zero — and never a ratio below the volume floor")
    cur.execute("""SELECT SUM(orders > 0 AND complaints_per_100_orders IS NULL) a,
                          SUM((orders IS NULL OR orders = 0) AND complaints_per_100_orders IS NOT NULL) b,
                          SUM(units > 0 AND complaints_per_1000_units IS NULL) c,
                          SUM((units IS NULL OR units = 0) AND complaints_per_1000_units IS NOT NULL) d,
                          SUM(orders_us >= %s AND refund_rate_us_pct IS NULL) e,
                          SUM((orders_us IS NULL OR orders_us < %s) AND refund_rate_us_pct IS NOT NULL) f,
                          SUM(orders IS NULL) no_sales, SUM(orders = 0) zero_orders,
                          SUM(has_sales = 1) covered, COUNT(*) all_rows
                     FROM v_complaint_rates""", (FLOOR, FLOOR))
    n = cur.fetchone()
    eq("rows with a positive denominator and a NULL rate", int(n["a"] or 0), 0)
    eq("rows with no denominator but a rate", int(n["b"] or 0), 0)
    eq("units side: positive denominator and NULL rate", int(n["c"] or 0), 0)
    eq("units side: no denominator but a rate", int(n["d"] or 0), 0)
    eq("US-refund-rate: quotable denominator and NULL rate", int(n["e"] or 0), 0)
    eq("US-refund-rate: unquotable denominator but a rate", int(n["f"] or 0), 0)
    print(f"     {int(n['covered']):,}/{int(n['all_rows']):,} rows cover a sales row; "
          f"{int(n['no_sales']):,} have none, {int(n['zero_orders']):,} sold 0 orders")

    # R12: no impossible refund percentage survives the floor.
    cur.execute("""SELECT COUNT(*) n FROM v_complaint_rates
                    WHERE refund_rate_us_pct IS NOT NULL
                      AND (refund_rate_us_pct < 0 OR refund_rate_us_pct > 100)""")
    eq("no refund rate above 100% anywhere in the view (R12)", int(cur.fetchone()["n"]), 0)
    cur.execute("""SELECT orders_us, refunds_us, refund_rate_us_pct FROM v_complaint_rates
                    WHERE sku='SC-XL-B' AND month='2026-07' LIMIT 1""")
    bad_row = cur.fetchone()
    if bad_row:
        eq("SC-XL-B 2026-07: 54 refunds on 2 US orders has no rate (R12)",
           bad_row["refund_rate_us_pct"], None)
    else:
        fail("SC-XL-B 2026-07 is missing — the R12 anchor row moved")

    print("\n== 5/9 complaint counts unchanged by every join")
    cur.execute("""SELECT COUNT(*) c, COALESCE(SUM(n),0) s FROM v_ai_complaints_monthly""")
    a = cur.fetchone()
    for view in ("v_complaint_rates", "v_sku_month_rates", "v_complaint_rates_by_source"):
        col = "n_complaints"
        cur.execute(f"""SELECT SUM({col}) s FROM {view}""")
        got = int(cur.fetchone()["s"] or 0)
        eq(f"{view} carries the same records as the monthly grain", got, int(a["s"]))

    print("\n== 7/9 coverage, stated as a number the reader can quote")
    cur.execute("""SELECT SUM(s.sku IS NOT NULL) cov_pairs, COUNT(*) pairs,
                          SUM(CASE WHEN s.sku IS NOT NULL THEN x.n ELSE 0 END) cov_recs,
                          SUM(x.n) recs
                     FROM (SELECT sku, month, SUM(n_complaints) n FROM v_complaint_rates
                            WHERE sku IS NOT NULL AND month IS NOT NULL
                            GROUP BY sku, month) x
                     LEFT JOIN sales_by_sku_month s ON s.sku = x.sku AND s.month = x.month""")
    cv = cur.fetchone()
    pct_recs = 100.0 * int(cv["cov_recs"]) / max(int(cv["recs"]), 1)
    print(f"     {int(cv['cov_pairs']):,}/{int(cv['pairs']):,} complaint sku-months "
          f"({100.0 * int(cv['cov_pairs']) / max(int(cv['pairs']), 1):.2f}%) and "
          f"{int(cv['cov_recs']):,}/{int(cv['recs']):,} complaint records "
          f"({pct_recs:.2f}%) have a sales row")
    if pct_recs < 90:
        fail(f"sales coverage is only {pct_recs:.1f}% of complaint records — a rate over that "
             f"must be labelled partial wherever it is shown")
    else:
        ok(f"coverage {pct_recs:.2f}% of complaint records (>90%, and still labelled in the app)")

    # ---------------------------------------------------------------- 7. the family
    print("\n== 7b/9 seat cushions: the FAMILY denominator, not the selection's")
    cur.execute("""SELECT DISTINCT sku FROM v_ai_complaints_monthly
                    WHERE product_family='Seat cushions' AND sku IS NOT NULL""")
    skus = [r["sku"] for r in cur.fetchall()]
    ph = ",".join(["%s"] * len(skus))
    cur.execute(f"""SELECT month, SUM(n_complaints) recs,
                           SUM(CASE WHEN has_sales = 0 THEN n_complaints ELSE 0 END) recs_nosales
                      FROM v_sku_month_rates WHERE product_family='Seat cushions'
                     GROUP BY month ORDER BY month""")
    recs = {r["month"]: int(r["recs"]) for r in cur.fetchall()}
    # (a) the family denominator: every SKU of the family, every month
    cur.execute(f"""SELECT month, SUM(orders) o, SUM(refunds_us) rfu, SUM(orders_us) ou,
                           COUNT(*) n
                      FROM sales_by_sku_month WHERE sku IN ({ph}) GROUP BY month""", skus)
    fam = {r["month"]: r for r in cur.fetchall()}
    # (b) the SELECTION-conditioned denominator the old view used: only the
    # SKU-months that recorded a complaint. Named as such, never "all SKUs".
    cur.execute("""SELECT month, SUM(o) o, COUNT(*) n
                     FROM (SELECT sku, month, MAX(orders) o FROM v_complaint_rates
                            WHERE product_family='Seat cushions' GROUP BY sku, month) t
                    GROUP BY month""")
    cond = {r["month"]: r for r in cur.fetchall()}
    months = [m for m in sorted(fam) if m >= "2026-06"]
    print("     month      recs  family orders  rate   |  SKU-months with a complaint: "
          "orders  rate")
    for m in months:
        f_ = fam[m]
        c_ = cond.get(m, {"o": 0, "n": 0})
        fr = 100.0 * recs.get(m, 0) / int(f_["o"]) if int(f_["o"] or 0) else None
        cr = 100.0 * recs.get(m, 0) / int(c_["o"]) if int(c_["o"] or 0) else None
        print(f"     {m}  {recs.get(m, 0):>5,}  {int(f_['o']):>11,}  "
              f"{(f'{fr:.2f}' if fr else '—'):>6}  |  {int(c_['n']):>3}  "
              f"{int(c_['o']):>9,}  {(f'{cr:.2f}' if cr else '—'):>6}")
    # The two denominators must DIFFER, or this test cannot see the defect.
    aug_f, aug_c = int(fam["2026-08"]["o"]), int(cond["2026-08"]["o"])
    if aug_f == aug_c:
        fail("August's family and selection-conditioned denominators agree, so this check "
             "cannot prove which one the app uses")
    else:
        ok(f"August: family {aug_f:,} orders vs {aug_c:,} in SKU-months with a complaint — "
           f"{aug_f - aug_c:,} orders belong to SKUs that recorded nothing (R2)")
    # and the family number is the replica's, not the view's own arithmetic
    hf = hand_family(rc, skus, "2026-08")
    eq("August family orders re-derived from the replica", aug_f, int(hf["orders"]))
    eq("July family refunds_us re-derived from the replica",
       int(fam["2026-07"]["rfu"]), int(hand_family(rc, skus, "2026-07")["refunds_us"]))
    jul_rate = 100.0 * recs["2026-07"] / int(fam["2026-07"]["o"])
    aug_rate = 100.0 * recs["2026-08"] / aug_f
    eq("seat cushions 2026-07 rate on the FAMILY denominator", round(jul_rate, 2), 1.55, tol=0.005)
    eq("seat cushions 2026-08 rate on the FAMILY denominator", round(aug_rate, 2), 0.53, tol=0.005)
    ok(f"the selection-conditioned August rate would have read "
       f"{100.0 * recs['2026-08'] / aug_c:.2f} — which is how 'all SKUs' mislabelled 0.54")

    # ---------------------------------------------------------------- 8. zero orders
    print("\n== 8/9 zero-order months do not leak into the numerator (R3)")
    with App() as app:
        html = app.get("/monthly?sku=S-Hum")
        if "200.00" in html:
            fail("/monthly?sku=S-Hum still prints 200.00 complaint records per 100 orders")
        elif "too few to quote a rate" not in html:
            fail("/monthly?sku=S-Hum does not say 'too few to quote a rate'")
        else:
            ok("/monthly?sku=S-Hum says 'too few to quote a rate' and prints no rate for its "
               "1 order")
        cur.execute("""SELECT SUM(n_complaints) n FROM (
                         SELECT sku, month, SUM(n_complaints) n_complaints, MAX(orders) o
                           FROM v_complaint_rates WHERE sku='S-Hum' GROUP BY sku, month) t
                        WHERE o = 0""")
        z = int(cur.fetchone()["n"] or 0)
        if z and "sold 0 orders" not in html:
            fail(f"S-Hum has {z} record(s) in a 0-order SKU-month and the page does not say so")
        elif z:
            ok(f"S-Hum: the {z} record in the 0-order month is disclosed as excluded, not divided")

        # ------------------------------------------------------------ 9. the feed
        print("\n== 9/9 per-feed rate series (R4)")
        html = app.get("/reviews?family=Seat+cushions")
        for need in ("Complaints per 100 orders by feed",
                     "A month-over-month comparison is only valid within one feed"):
            if need not in html:
                fail(f"/reviews?family=Seat+cushions is missing: {need}")
            else:
                ok(f"present: {need}")
        # the fba-only series, re-derived: records per month over the family denominator
        cur.execute("""SELECT month, SUM(n_complaints) n FROM v_complaint_rates_by_source
                        WHERE product_family='Seat cushions' AND source='fba'
                          AND month >= '2026-06' GROUP BY month ORDER BY month""")
        fba = {r["month"]: int(r["n"]) for r in cur.fetchall()}
        cur.execute("""SELECT SUM(source='ncx') ncx, SUM(source='amazon') amazon, COUNT(*) n
                         FROM v_ai_complaints WHERE product_family='Seat cushions'
                          AND DATE_FORMAT(review_date,'%Y-%m')='2026-07'
                          AND final_complaint IS NOT NULL AND final_complaint <> 'Review Deletion'""")
        j = cur.fetchone()
        eq("July: records from ncx + amazon", int(j["ncx"] or 0) + int(j["amazon"] or 0), 200)
        eq("July: total records", int(j["n"]), 510)
        for m in ("2026-07", "2026-08", "2026-09"):
            want = 100.0 * fba[m] / int(fam[m]["o"])
            cell = f"{want:.2f}"
            if cell not in html:
                fail(f"the per-feed table does not print fba {m} = {cell} "
                     f"({fba[m]} records / {int(fam[m]['o']):,} orders)")
            else:
                ok(f"per-feed table prints fba {m} = {cell}")
        if fba["2026-09"] > fba["2026-07"]:
            ok(f"on fba, September ({fba['2026-09']}) exceeds July ({fba['2026-07']}) — the "
               f"all-source July peak is a feed-composition artifact")
        else:
            fail("the fba series does not peak in September — re-check the R4 finding")
        if "sales feed" in html and "feeds changed" in html:
            ok("the chart marks the months where the contributing feeds changed")


if __name__ == "__main__":
    sys.exit(main())
