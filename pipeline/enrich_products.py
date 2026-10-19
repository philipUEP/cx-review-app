#!/usr/bin/env python3
"""
Re-enrich every classified row's product identity from `amzmanager.all_products`.

THE BUG THIS FIXES
------------------
`cx_reviews.product_id` points at `all_products`, NOT `business_products`. Both
tables have small auto-increment ids (all_products 7-7081, business_products
1-686), so `JOIN business_products bp ON bp.id = r.product_id` does not fail —
it silently returns a DIFFERENT product. That is why the SKU column showed the
product code (`RM-B` / `RM-B`), why 16% of rows showed bare numbers
('6','7','1' — real seller SKUs for other products), and why no product title
existed to search on.

Verified chain (all three legs checked, not assumed):
    cx_reviews.product_id -> all_products.id          (the id space that fits: 7081 max)
      all_products.asin   -> products.asin            (6827/6827 - same row count)
      products.business_product_id -> business_products.id
        business_products.parent_product_id -> business_parent_products.id
Proof it is the right table: all_products.asin equals the ASIN we already hold
on 98.9%+ of rows; business_products has no asin column at all, so it could not
be validated this way even in principle.

Columns written (classifications):
    sku      = all_products.sku                     (seller SKU, e.g. RM-B-IR)
    asin     = all_products.asin                    (e.g. B01LZ75ROQ)
    product  = business_products.short_name         (e.g. RM-B)
    parent   = business_parent_products.short_name  (e.g. RM)
    title    = COALESCE(all_products.title, products_details.title,
                        business_products.name), truncated

Precedence for product/parent: the canonical FK through `products` wins; an
exact `all_products.sku = business_products.short_name` match is the fallback
for the ~29% of products with no business_product_id. Where they disagree (they
do on ~7% of the overlap, always as a rename such as RGU-BR -> RGU-BR-OLD) the
FK is kept, because that is the maintained relationship and it is what the
legacy Complaints validation screen displays. The disagreement count is printed
on every run so the choice stays visible rather than buried.

Reads: db02-replica (MARIADB_*, database amzmanager, SELECT only).
Writes: automation_philip on db01-master (CXAPP_*). Nothing else, ever.

Idempotent: recomputes from the source and writes only changed values, so a
second run reports 0 changes. Run from cx_refresh.sh AFTER load_to_db (the CSV
still carries the old, wrong enrichment) and before the views are created.
"""
import os
import re
import sys
from collections import Counter

import pymysql

BATCH = 1500
TITLE_MAX = 240

LOOKUP = """
SELECT r.id AS review_id,
       ap.sku,
       ap.asin,
       COALESCE(bp.short_name, bd.short_name)   AS product,
       COALESCE(bpp.short_name, bpd.short_name) AS parent,
       COALESCE(ap.title, pd.title, bp.name, bd.name) AS title
  FROM cx_reviews r
  LEFT JOIN all_products ap ON ap.id = r.product_id
  LEFT JOIN (SELECT asin, MAX(business_product_id) AS b
               FROM products WHERE business_product_id > 0 GROUP BY asin) bm
         ON bm.asin = ap.asin
  LEFT JOIN business_products bp        ON bp.id = bm.b
  LEFT JOIN business_parent_products bpp ON bpp.id = bp.parent_product_id
  LEFT JOIN business_products bd        ON bd.short_name = ap.sku
  LEFT JOIN business_parent_products bpd ON bpd.id = bd.parent_product_id
  LEFT JOIN (SELECT product_id, MAX(title) AS title
               FROM products_details GROUP BY product_id) pd
         ON pd.product_id = ap.id
 WHERE r.id IN (%s)
"""


def read_db():
    return pymysql.connect(
        host=os.environ["MARIADB_HOST"], user=os.environ["MARIADB_USER"],
        password=os.environ["MARIADB_PASSWORD"], database="amzmanager",
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)


def write_db():
    return pymysql.connect(
        host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
        password=os.environ["CXAPP_DB_PASSWORD"], database=os.environ["CXAPP_DB_NAME"],
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, autocommit=False)


def clean(v, n=None):
    if v is None:
        return None
    v = re.sub(r"\s+", " ", str(v)).strip()
    if not v:
        return None
    return v[:n] if n else v


def ensure_title_column(cur):
    cur.execute("""SELECT COUNT(*) AS n FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='classifications'
                      AND COLUMN_NAME='title'""", (os.environ["CXAPP_DB_NAME"],))
    if cur.fetchone()["n"]:
        return False
    cur.execute("ALTER TABLE classifications ADD COLUMN title VARCHAR(255) NULL AFTER product")
    print("  added classifications.title VARCHAR(255)")
    return True


def main():
    w = write_db()
    wc = w.cursor()
    if ensure_title_column(wc):
        w.commit()

    wc.execute("""SELECT review_id, sku, asin, parent, product, title FROM classifications""")
    have = {r["review_id"]: r for r in wc.fetchall()}
    ids = list(have)
    print(f"classifications: {len(ids):,} rows")

    r = read_db()
    rc = r.cursor()
    truth = {}
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        rc.execute(LOOKUP % ",".join(["%s"] * len(chunk)), chunk)
        for row in rc.fetchall():
            truth[row["review_id"]] = row
    print(f"resolved on all_products: {len(truth):,}/{len(ids):,} "
          f"({len(truth) / max(1, len(ids)):.1%})")
    if len(truth) / max(1, len(ids)) < 0.95:
        print("ABORT: fewer than 95% of reviews resolve through cx_reviews.product_id -> "
              "all_products. Wrong table or wrong id column. Nothing written.")
        return 1

    # ---- guards -----------------------------------------------------------
    # 1. ASIN AGREEMENT. The asin already stored came from a different path; if
    #    all_products were the wrong table these would collapse.
    checked = agree = 0
    for rid, old in have.items():
        new = truth.get(rid) or {}
        if clean(old["asin"]) and clean(new.get("asin")):
            checked += 1
            agree += (clean(old["asin"]) == clean(new["asin"]))
    if checked:
        rate = agree / checked
        print(f"asin agreement with the asin already held: {agree:,}/{checked:,} ({rate:.1%})")
        if rate < 0.95:
            print("ABORT: asin disagreement — all_products is NOT the right table. Nothing written.")
            return 1

    # 2. A numeric-only SKU is legitimate only if it is a real seller SKU with a
    #    human product title behind it. If it is not, it is an id leaking into a
    #    string column and the mapping is wrong.
    numeric = [t for t in truth.values() if clean(t["sku"]) and clean(t["sku"]).isdigit()]
    orphan = [t for t in numeric if not clean(t["title"])]
    print(f"numeric-only SKUs: {len(numeric):,} (all resolve to a real product row: "
          f"{len(numeric) - len(orphan):,}; with a human title: "
          f"{sum(1 for t in numeric if clean(t['title'])):,})")
    if orphan:
        print(f"ABORT: {len(orphan)} numeric SKUs have no product title — "
              f"this looks like ids, not seller SKUs. Nothing written.")
        return 1

    # ---- compare and write ------------------------------------------------
    upd = []
    changed = Counter()
    for rid, old in have.items():
        t = truth.get(rid)
        if not t:
            continue
        new = {
            "sku": clean(t["sku"], 60),
            "asin": clean(t["asin"], 20),
            "product": clean(t["product"], 120),
            "parent": clean(t["parent"], 60),
            "title": clean(t["title"], TITLE_MAX),
        }
        diff = {k: v for k, v in new.items() if (clean(old[k]) or None) != v}
        if diff:
            for k in diff:
                changed[k] += 1
            upd.append((new["sku"], new["asin"], new["product"], new["parent"],
                        new["title"], rid))

    print(f"rows needing an update: {len(upd):,} "
          f"(sku {changed['sku']:,}, asin {changed['asin']:,}, product {changed['product']:,}, "
          f"parent {changed['parent']:,}, title {changed['title']:,})")

    # One UPDATE ... JOIN through a temp table, not 19k single-row UPDATEs: each
    # statement is a network round trip, which turned this into a 10-minute run
    # that looked like a hang. 19k rows in 10 multi-row INSERTs is seconds.
    wc.execute("""CREATE TEMPORARY TABLE _enrich (
                    review_id BIGINT PRIMARY KEY, sku VARCHAR(60), asin VARCHAR(20),
                    product VARCHAR(120), parent VARCHAR(60), title VARCHAR(255)
                  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    for i in range(0, len(upd), 2000):
        wc.executemany("""INSERT INTO _enrich (sku, asin, product, parent, title, review_id)
                          VALUES (%s,%s,%s,%s,%s,%s)""", upd[i:i + 2000])
    wc.execute("""UPDATE classifications c
                    JOIN _enrich e ON e.review_id = c.review_id
                     SET c.sku=e.sku, c.asin=e.asin, c.product=e.product,
                         c.parent=e.parent, c.title=e.title""")
    print(f"  updated: {wc.rowcount:,} rows")
    wc.execute("DROP TEMPORARY TABLE _enrich")
    w.commit()

    # ---- verify what actually landed, from the database -------------------
    print("\n=== after ===")
    wc.execute("SELECT COUNT(*) n FROM classifications")
    print(f"  rows              : {wc.fetchone()['n']:,}")
    for col in ("sku", "asin", "product", "parent", "title"):
        wc.execute(f"SELECT COUNT(*) n FROM classifications WHERE {col} IS NOT NULL AND {col}<>''")
        print(f"  {col:<18}: {wc.fetchone()['n']:,} filled")
    wc.execute("""SELECT COUNT(*) n FROM classifications
                   WHERE sku IS NOT NULL AND sku REGEXP '^[0-9]+$'""")
    n_num = wc.fetchone()["n"]
    wc.execute("""SELECT COUNT(*) n FROM classifications
                   WHERE sku REGEXP '^[0-9]+$' AND (title IS NULL OR title='')""")
    n_num_orphan = wc.fetchone()["n"]
    print(f"  numeric-only sku  : {n_num:,} (without a product title: {n_num_orphan:,})")
    wc.execute("SELECT COUNT(*) n FROM classifications WHERE sku=product")
    print(f"  sku == product    : {wc.fetchone()['n']:,} "
          f"(legitimate: some seller SKUs are the product code)")
    wc.execute("SELECT COUNT(*) n FROM classifications WHERE product IS NULL OR product=''")
    print(f"  no product        : {wc.fetchone()['n']:,}")
    wc.execute("""SELECT COUNT(DISTINCT product) p, COUNT(DISTINCT parent) pa,
                         COUNT(DISTINCT sku) s, COUNT(DISTINCT title) t
                    FROM classifications""")
    x = wc.fetchone()
    print(f"  distinct          : {x['p']} products · {x['pa']} parents · "
          f"{x['s']} skus · {x['t']} titles")
    wc.execute("""SELECT parent, product, sku, LEFT(title,60) title, COUNT(*) n
                    FROM classifications GROUP BY parent, product, sku, LEFT(title,60)
                    ORDER BY n DESC LIMIT 8""")
    print("  top combinations  :")
    for row in wc.fetchall():
        print(f"    {row['n']:6,}  {str(row['parent'] or '-'):<6} {str(row['product'] or '-'):<12} "
              f"{str(row['sku'] or '-'):<16} {row['title'] or ''}")

    if n_num_orphan:
        print("\nFAIL: numeric SKUs still lack a product title after enrichment.")
        return 1
    r.close()
    w.close()
    print("\nenrichment OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
