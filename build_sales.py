#!/usr/bin/env python3
"""Build sales_by_sku_month — the exposure denominator the complaint view never had.

WHY THIS EXISTS
---------------
Every number in this project is a count of complaint RECORDS. Her question —
"if we have an increased amount of RETURNS on our seat cushion I need to be able
to see during specific date ranges what people were complaining about" — cannot
be answered by a count: a launch, a promotion and a quality problem all produce
more records. It needs units and orders on the same key and the same grain as
the complaint rollup.

WHAT IT DOES
------------
Reads the seller's daily finance feed from the READ-ONLY replica
(`amzmanager.finance_by_products`, 640k rows, daily grain) and writes a
sku × month aggregate to `automation_philip.sales_by_sku_month`.

THE JOIN (proven, not assumed — see --explain)
    finance_by_products.product_id  ->  products.id   (NOT products.sku)
    products.sku                    ->  classifications.sku
`finance_by_products.product_id` carries no SKU of its own. `products` is
`all_products` (all_products is a view over products), so its `.sku` is the same
seller SKU that `classifications.sku` came from. Measured on 2026-10-19:
184/184 complaint SKUs match products.sku (100.00% of 22,228 records, including
the numeric SKUs 6/7/1/11); matching on fnsku instead covers 0.20%.

GRAIN
    (sku, month) — one row per seller SKU per calendar month, aggregated over
    ALL marketplaces (country_id) of the single shop that has finance rows.
    Shipping one shop's worth of rows is the only option: finance_by_products
    holds exactly one shop_id.
    The US-only columns exist because 95% of the complaints are US reviews; they
    let a reader check the worldwide denominator against a same-market one
    without a second table.

`refunds` is the SELLER'S OWN refund count for that SKU-month. It is a sanity
check on the same scale as the complaint counts, never a validation of them: a
refund and a complaint are different events (most refunds never produce a review).

READ-ONLY RULE
    The replica connection is SELECT-only (the account holds GRANT SELECT, SHOW
    VIEW). Every write in this file goes to automation_philip.

IDEMPOTENT: load into a `_new` table, then RENAME. A failed or interrupted run
leaves the previous good table in place, never a half-loaded one.

  --explain   print the join proof and the coverage report, write nothing
  --check     refresh into a temp table, compare to the live table, write nothing
              (exit 1 if the live table is stale)
"""
import argparse
import os
import sys
import time

import pymysql

HOSTS = {
    "replica": dict(
        host=os.environ.get("MARIADB_HOST"),
        port=int(os.environ.get("MARIADB_PORT") or 3306),
        user=os.environ.get("MARIADB_USER"),
        password=os.environ.get("MARIADB_PASSWORD"),
        database="amzmanager",
    ),
    "write": dict(
        host=os.environ.get("CXAPP_DB_HOST"),
        user=os.environ.get("CXAPP_DB_USER"),
        password=os.environ.get("CXAPP_DB_PASSWORD"),
        database=os.environ.get("CXAPP_DB_NAME"),
    ),
}

TABLE = "sales_by_sku_month"

DDL = f"""
CREATE TABLE {{t}} (
  sku           VARCHAR(60)  NOT NULL,
  month         CHAR(7)      NOT NULL COMMENT 'YYYY-MM, calendar month of the finance date',
  units         BIGINT       NOT NULL DEFAULT 0,
  orders        BIGINT       NOT NULL DEFAULT 0,
  refunds       BIGINT       NOT NULL DEFAULT 0 COMMENT 'seller-side refund count for this SKU-month',
  sales         DECIMAL(16,2) NOT NULL DEFAULT 0,
  units_us      BIGINT       NOT NULL DEFAULT 0,
  orders_us     BIGINT       NOT NULL DEFAULT 0,
  refunds_us    BIGINT       NOT NULL DEFAULT 0,
  sales_us      DECIMAL(16,2) NOT NULL DEFAULT 0,
  n_countries   INT          NOT NULL DEFAULT 0 COMMENT 'marketplaces contributing a row',
  PRIMARY KEY (sku, month),
  KEY idx_sales_month (month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='Exposure denominator: seller units/orders per SKU per month, from amzmanager.finance_by_products'
"""


def replica():
    c = pymysql.connect(charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, **HOSTS["replica"])
    return c


def writer():
    return pymysql.connect(charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
                           autocommit=True, **HOSTS["write"])


# --------------------------------------------------------------------- join
def us_country_id(cur):
    cur.execute("SELECT id FROM country WHERE short_name='us' LIMIT 1")
    r = cur.fetchone()
    if not r:
        raise SystemExit("no country row with short_name='us' — refusing to guess the US id")
    return int(r["id"])


def join_proof(rc, wc):
    """Measure the join instead of asserting it. Printed by --explain and on failure."""
    r = rc.cursor()
    us = us_country_id(r)
    r.execute("""SELECT COUNT(*) n, COUNT(DISTINCT product_id) pids,
                        COUNT(DISTINCT country_id) countries, COUNT(DISTINCT shop_id) shops
                   FROM finance_by_products""")
    f = r.fetchone()
    r.execute("""SELECT COUNT(*) n FROM finance_by_products f
                  JOIN products p ON p.id = f.product_id""")
    via_products_id = int(r.fetchone()["n"])
    r.execute("""SELECT COUNT(*) n FROM finance_by_products f
                  JOIN products p ON p.sku = f.product_id""")  # nonsense key, for the record
    out = {
        "finance_rows": int(f["n"]), "finance_products": int(f["pids"]),
        "finance_countries": int(f["countries"]), "finance_shops": int(f["shops"]),
        "us_country_id": us, "rows_joined_via_products_id": via_products_id,
    }
    # product_id -> products.id -> sku; how many distinct SKUs, and is any SKU
    # sold in more than one marketplace row (so a SKU total ≠ a single market)?
    r.execute("""SELECT COUNT(DISTINCT p.sku) skus,
                        SUM(p.sku IS NULL OR p.sku='') blank_sku
                   FROM finance_by_products f JOIN products p ON p.id = f.product_id""")
    q = r.fetchone()
    out["skus_in_finance"] = int(q["skus"] or 0)
    out["rows_with_blank_sku"] = int(q["blank_sku"] or 0)

    w = wc.cursor()
    w.execute("SELECT COUNT(DISTINCT sku) skus, COUNT(*) recs FROM classifications")
    c = w.fetchone()
    out["complaint_skus"], out["complaint_records"] = int(c["skus"]), int(c["recs"])
    r.execute("SELECT DISTINCT sku FROM products WHERE sku IS NOT NULL AND sku<>''")
    prod_skus = {x["sku"] for x in r.fetchall()}
    w.execute("SELECT sku, COUNT(*) n FROM classifications GROUP BY sku")
    rows = w.fetchall()
    matched = [x for x in rows if x["sku"] in prod_skus]
    out["complaint_skus_matched"] = len({x["sku"] for x in matched})
    out["complaint_records_matched"] = sum(int(x["n"]) for x in matched)
    out["pct_records_matched"] = (100.0 * out["complaint_records_matched"]
                                  / max(out["complaint_records"], 1))
    out["top_unmatched"] = sorted(
        ((x["sku"], int(x["n"])) for x in rows if x["sku"] not in prod_skus),
        key=lambda t: -t[1])[:10]
    return out


def extract(rc):
    """The aggregate. One row per (sku, month), all marketplaces of the one shop."""
    r = rc.cursor()
    us = us_country_id(r)
    r.execute(f"""
        SELECT p.sku                                    AS sku,
               DATE_FORMAT(f.date, '%Y-%m')           AS month,
               SUM(f.units)                             AS units,
               SUM(f.orders)                            AS orders,
               SUM(f.refunds)                           AS refunds,
               SUM(f.sales)                             AS sales,
               SUM(CASE WHEN f.country_id = {us} THEN f.units   ELSE 0 END) AS units_us,
               SUM(CASE WHEN f.country_id = {us} THEN f.orders  ELSE 0 END) AS orders_us,
               SUM(CASE WHEN f.country_id = {us} THEN f.refunds ELSE 0 END) AS refunds_us,
               SUM(CASE WHEN f.country_id = {us} THEN f.sales   ELSE 0 END) AS sales_us,
               COUNT(DISTINCT f.country_id)             AS n_countries
          FROM finance_by_products f
          JOIN products p ON p.id = f.product_id
         WHERE f.date IS NOT NULL
           AND p.sku IS NOT NULL AND p.sku <> ''
         GROUP BY p.sku, DATE_FORMAT(f.date, '%Y-%m')
    """)
    return r.fetchall()


def load(wc, rows, target=TABLE):
    cur = wc.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {target}_new")
    cur.execute(DDL.format(t=f"{target}_new"))
    cols = ["sku", "month", "units", "orders", "refunds", "sales",
            "units_us", "orders_us", "refunds_us", "sales_us", "n_countries"]
    sql = (f"INSERT INTO {target}_new ({','.join(cols)}) VALUES "
           f"({','.join(['%s'] * len(cols))})")
    batch = 5000
    for i in range(0, len(rows), batch):
        cur.executemany(sql, [[r[c] for c in cols] for r in rows[i:i + batch]])
    cur.execute(f"SELECT COUNT(*) n, COALESCE(SUM(orders),0) o, COALESCE(SUM(units),0) u "
                f"FROM {target}_new")
    got = cur.fetchone()
    n, o, u = int(got["n"]), int(got["o"]), int(got["u"])
    if n != len(rows):
        raise SystemExit(f"load mismatch: {n:,} rows written, {len(rows):,} extracted")
    return int(n), int(o), int(u)


def swap(wc):
    """Load-then-RENAME: a reader never sees a half-built table."""
    cur = wc.cursor()
    cur.execute("SHOW TABLES LIKE %s", (TABLE,))
    if not cur.fetchone():
        cur.execute(f"RENAME TABLE {TABLE}_new TO {TABLE}")
        return
    cur.execute(f"DROP TABLE IF EXISTS {TABLE}_old")
    cur.execute(f"RENAME TABLE {TABLE} TO {TABLE}_old, {TABLE}_new TO {TABLE}")
    cur.execute(f"DROP TABLE {TABLE}_old")


# ----------------------------------------------------------------- coverage
def coverage(wc):
    """What the denominator actually covers, measured on the live table."""
    cur = wc.cursor()
    cur.execute(f"""SELECT COUNT(*) rows_n, COUNT(DISTINCT sku) skus,
                           MIN(month) mn, MAX(month) mx,
                           SUM(orders) orders, SUM(units) units, SUM(refunds) refunds,
                           SUM(orders=0) zero_order_months
                      FROM {TABLE}""")
    t = cur.fetchone()
    cur.execute(f"""SELECT SUM(s.sku IS NOT NULL) covered_pairs, COUNT(*) pairs,
                           SUM(CASE WHEN s.sku IS NOT NULL THEN x.n ELSE 0 END) covered_recs,
                           SUM(x.n) recs
                      FROM (SELECT sku, month, SUM(n) n FROM v_ai_complaints_monthly
                             WHERE sku IS NOT NULL AND month IS NOT NULL
                             GROUP BY sku, month) x
                      LEFT JOIN {TABLE} s ON s.sku = x.sku AND s.month = x.month""")
    c = cur.fetchone()
    return {
        "rows": int(t["rows_n"] or 0), "skus": int(t["skus"] or 0),
        "min_month": t["mn"], "max_month": t["mx"],
        "orders": int(t["orders"] or 0), "units": int(t["units"] or 0),
        "refunds": int(t["refunds"] or 0),
        "zero_order_months": int(t["zero_order_months"] or 0),
        "pairs": int(c["pairs"] or 0), "covered_pairs": int(c["covered_pairs"] or 0),
        "recs": int(c["recs"] or 0), "covered_recs": int(c["covered_recs"] or 0),
    }


def print_report(proof, cov):
    print(f"join: finance_by_products.product_id -> products.id ({proof['rows_joined_via_products_id']:,}"
          f"/{proof['finance_rows']:,} rows resolve), products.sku -> classifications.sku")
    print(f"  complaint SKUs matched: {proof['complaint_skus_matched']:,}/{proof['complaint_skus']:,} "
          f"({100.0 * proof['complaint_skus_matched'] / max(proof['complaint_skus'], 1):.1f}%), "
          f"records {proof['complaint_records_matched']:,}/{proof['complaint_records']:,} "
          f"({proof['pct_records_matched']:.2f}%)")
    if proof["top_unmatched"]:
        print(f"  unmatched complaint SKUs: {proof['top_unmatched']}")
    print(f"{TABLE}: {cov['rows']:,} sku-months, {cov['skus']:,} SKUs, "
          f"{cov['min_month']} -> {cov['max_month']}, {cov['orders']:,} orders, "
          f"{cov['units']:,} units, {cov['refunds']:,} refunds")
    print(f"  complaint coverage: {cov['covered_pairs']:,}/{cov['pairs']:,} sku-months "
          f"({100.0 * cov['covered_pairs'] / max(cov['pairs'], 1):.2f}%), "
          f"{cov['covered_recs']:,}/{cov['recs']:,} records "
          f"({100.0 * cov['covered_recs'] / max(cov['recs'], 1):.2f}%)")
    print(f"  {cov['zero_order_months']:,} sku-months carry finance rows with 0 orders "
          f"(a real zero: the rate is NULL there, never 0 and never a division)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--explain", action="store_true", help="print the join proof + coverage, write nothing")
    ap.add_argument("--check", action="store_true", help="report whether the live table is current")
    a = ap.parse_args()

    t0 = time.time()
    rc = replica()
    wc = writer()
    try:
        proof = join_proof(rc, wc)
        if a.explain:
            print_report(proof, coverage(wc))
            return 0
        rows = extract(rc)
        n, o, u = load(wc, rows)
        if getattr(a, "check", False):
            cov = coverage(wc)
            wc.cursor().execute(f"DROP TABLE IF EXISTS {TABLE}_new")
            print_report(proof, cov)
            print("CHECK: extraction differs from the live table" if n != cov["rows"]
                  else f"CHECK: live table is current ({cov['rows']:,} sku-months)")
            return 0 if n == cov["rows"] else 1
        swap(wc)
        print_report(proof, coverage(wc))
        print(f"loaded {n:,} sku-months ({o:,} orders, {u:,} units) in {time.time() - t0:.1f}s")
        return 0
    finally:
        rc.close()
        wc.close()


if __name__ == "__main__":
    sys.exit(main())
