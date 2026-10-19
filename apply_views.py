#!/usr/bin/env python3
"""
Create/replace the reporting views. Idempotent — run from cx_refresh.sh.

v_ai_complaints
  One row per review with BOTH labels: what the AI said (ai_complaint /
  ai_category) and the RESOLVED label (final_complaint / final_category), which
  is the team's correction when one exists and the AI's label otherwise. Before
  this the database carried a correction and an AI label side by side with no
  answer to "what is this row's complaint", so every KPI, chart and export
  described the AI's opinion after the team had already corrected it.
  A 'rejected' review (not a complaint) has final_complaint NULL: removed from
  complaint reporting, still visible in the table.

  final_complaint carries the TAXONOMY spelling, not whatever spelling the
  classifier happened to emit. The AI wrote 'Not As Advertised/misleading' while
  the approved code is 'Not as Advertised/Misleading'; the review app's picker is
  built from complaint_codes, so without this the first correction would split
  one code into two values in every pivot. The lookup is case-insensitive and
  main() refuses to publish if two spellings of one code survive.

  product_family groups SKUs that are the same product in a different market or
  size variant (see PRODUCT_FAMILY). A title search misses the German
  'Sitzkissen' rows; a parent only covers one market. The family is what a
  question like "returns on our seat cushions" needs.

  dup_rank / dup_group say whether this row repeats a comment already recorded on
  the same ORDER: rank 1 is the first row of its (order_id, MD5(text)) group,
  rank 2 repeats one. Rows without an order_id fall back to (SKU, date, MD5).
  Nothing is removed -- the page discloses the repeats and offers a 'distinct
  complaints' view.

v_ai_complaints_monthly
  The grain Chadia asks for: SKU x month x complaint, with the share of that
  SKU-month and how many rows a human validated. One table for a PowerBI model
  to point at instead of a review-grain view it would have to aggregate itself.

  The grain is enforced, not hoped for: title/asin/product are taken with MAX()
  and kept OUT of the GROUP BY, because title varies within a SKU (the same
  EC-MF SKU carries an English, German, French and Spanish title) and grouping
  by it split 478 (sku, month, complaint) groups into 2-4 rows each. main()
  asserts COUNT(*) == COUNT(DISTINCT sku, month, complaint) so it cannot split
  again silently.

  title_short comes from sku_titles: the MODAL title for the SKU (the one most
  rows carry), refreshed with the duplicates. MAX(title) was lexical, so sku 7
  showed its 4-row French listing instead of the 649-row English one.

  Process noise and unusable categories are excluded here: 'Review Deletion' is
  a rule outcome, not a complaint, and rows whose category is NEW / NO_TEXT /
  ERR were never classified. Both stay in v_ai_complaints (and in the export)
  so nothing is invisible.

  title_short is a readable label for the SKU column: some seller SKUs really are
  numeric ('6' = the magnetic screen door, '1' = the umbrella), so a pivot on
  `sku` alone reads '6'. title_short is the head of the title and is the same for
  every month of a SKU. `sku` is never rewritten -- it is the seller's key.
"""
import os

import pymysql


def _seg(n):
    """The n'th pipe-separated segment of the review text (1-based)."""
    return (f"SUBSTRING_INDEX(SUBSTRING_INDEX(c.review_text,'|',{n}),'|',-1)")


def _prose(n):
    """1 when segment n reads as something a person actually wrote.

    'Changed Mind|My needs changed' is a return-form picklist, not prose: those
    rows cannot be checked against a label because there is nothing to read.
    A segment counts as prose at >= 25 characters containing a space - the
    picklist values ('Not as Expected', 'Too big', 'My needs changed') are all
    shorter, and every real customer comment in this corpus is longer.

    The same rule is implemented once in Python (app.py:text_parts) for
    rendering chips; keep the two in step - this is what the filter counts.
    """
    s = _seg(n)
    return f"IF(CHAR_LENGTH(TRIM({s}))>=25 AND TRIM({s}) LIKE '% %',1,0)"


HAS_PROSE = f"""CASE
         WHEN c.review_text IS NULL OR TRIM(c.review_text)='' THEN 0
         WHEN LOCATE('|', c.review_text)=0
              THEN IF(CHAR_LENGTH(TRIM(c.review_text))>=8,1,0)
         ELSE GREATEST({','.join(_prose(n) for n in range(1, 7))})
       END"""


# Product family: the parents that carry the SAME product into different
# markets/variants. Everything else is 'Other products' — a family nobody has
# curated yet, which is a fact, not a guess about what a parent code means.
#
#   SC     seat cushion (US/UK core line)
#   SC-GI  seat cushion, gel/improved insert
#   SCBC   seat + back cushion combo
#   PSC    pet seat cushion
#   BSC    booster seat cushion
#
# WCC / WC / BC are related lines but are their own products (wheelchair,
# wedge, bed cushion) and are deliberately NOT folded in: a family that quietly
# swallows the neighbourhood is worse than no family at all.
PRODUCT_FAMILY = {
    "SC": "Seat cushions",
    "SC-GI": "Seat cushions",
    "SCBC": "Seat cushions",
    "PSC": "Seat cushions",
    "BSC": "Seat cushions",
}
FAMILY_ELSE = "Other products"

# Reported as a complaint, but it is the rule's process verdict (duplicate /
# empty review), not something a customer complained about.
PROCESS_NOISE = "Review Deletion"
# Categories that mean "not classified"; they cannot report a complaint.
BAD_CATEGORIES = ("NEW", "NO_TEXT", "ERR")

# Duplicate key: the SAME customer comment on the SAME order.
#
# This used to be keyed on (SKU, review_date, MD5(text)) with no order check, and
# that merged distinct customers who typed the same short phrase on the same day:
# 887 of 892 groups spanned two or more order_ids, only 5 groups (196 rows) were
# single-order, and the worst cases were 'Did not match the sizing required'
# (7 rows across 106 orders) and 'Incidentally ordered too many' (2 rows across
# 694 orders). The footer called those "a review already counted", and the
# 'distinct complaints' toggle removed 1,532 real records while leaving ~2,711
# real duplicates in. Keyed on the order, true duplication is 2,050 groups /
# 2,897 excess rows and the toggle removes exactly those.
#
# Picklist-only rows are NOT excluded any more. The old rationale ("'Too big' on
# the same SKU and date is a hundred different orders") was right about SKU+date
# and wrong about order: the same text twice on ONE order is a repeated record
# whichever feed wrote it.
#
# The ~40 rows with no order_id fall back to (SKU, review_date, MD5(text)) —
# the old key, which is the best available for them and, on this corpus, groups
# nothing (0 groups, 0 rows).
#
# The groups live in a real table, not in a window function inside the view.
# A ROW_NUMBER()/COUNT() OVER (...) in the view forced every query against
# v_ai_complaints to materialise and re-sort all 22k rows: 0.4-0.6s per query,
# ~20s to render one page. Joined against an indexed table it is 0.06-0.10s with
# exactly the same numbers. refresh_duplicates() rebuilds it on every refresh and
# swaps it in atomically.
#
# Nothing is deduped: dup_rank/dup_group let the page DISCLOSE the repeats and
# offer a 'distinct complaints' view. Silently collapsing rows would change every
# count in the app without the reader being told.
DUP_TABLE = "review_duplicates"

# The exposure denominator, written by build_sales.py. apply_views does NOT
# create it (only SELECT is used here); main() refuses to publish the rate view
# when it is missing, because a rate view over an empty denominator would show
# NULL everywhere and look like "this data has no sales", which is a different
# statement from "the build has not run".
SALES_TABLE = "sales_by_sku_month"

# The volume floor for quoting a rate (R3/R12). Below 100 orders a ratio is
# arithmetic on a handful of transactions, not a level: SC-XL-B sold 2 US orders
# in 2026-07 and booked 54 refunds (2700%), and a 1-order SKU-month printed 200
# complaints per 100 orders. The views carry the flag; the app prints the
# denominator instead of the ratio; POWERBI.md filters on it.
RATE_FLOOR = 100

DUP_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS {t} (
  grp         VARCHAR(200) NOT NULL,
  h           CHAR(32)     NOT NULL,
  n           INT          NOT NULL,
  first_id    BIGINT       NOT NULL,
  review_date DATE         NULL,
  PRIMARY KEY (grp, h),
  KEY first_id (first_id),
  KEY review_date (review_date)
) ENGINE=InnoDB
"""

# The grouping key, as SQL over the classification row. 'o|<order_id>' when the
# order is known, 's|<sku>|<date>' for the 40 rows without one.
DUP_KEY = ("CASE WHEN c.order_id IS NOT NULL AND TRIM(c.order_id) <> '' "
           "THEN CONCAT('o|', TRIM(c.order_id)) "
           "ELSE CONCAT('s|', IFNULL(c.sku, ''), '|', IFNULL(c.review_date, '')) END")

# A short, readable product name for a SKU, for the places where the SKU column
# is the dimension a person pivots on. Some seller SKUs are numeric ('6', '1',
# '7', '11') and a pivot on `sku` alone reads '6', not 'Screen Door'.
#
# F10: it is the MODAL title for the SKU — the one the most rows carry — not
# MAX(title). MAX() is lexical, so sku 7 showed the 4-row French listing while
# the 649-row English title existed; a pivot on the label read French for an
# English product. The modal title is computed once per refresh into sku_titles
# and joined, so it is constant for every month of a SKU and costs nothing per
# query (a window function inside the view would re-sort 22k rows every time).
TITLE_TABLE = "sku_titles"
TITLE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS {t} (
  sku         VARCHAR(64)  NOT NULL,
  title       VARCHAR(500) NULL,
  title_short VARCHAR(64)  NULL,
  n           INT          NOT NULL,
  PRIMARY KEY (sku)
) ENGINE=InnoDB
"""
TITLE_SHORT_SQL = ("LEFT(TRIM(SUBSTRING_INDEX(SUBSTRING_INDEX(title, ' – ', 1), ' - ', 1)), 48)")


def family_sql(col="parent"):
    whens = " ".join(f"WHEN '{p}' THEN '{f}'" for p, f in PRODUCT_FAMILY.items())
    return f"CASE {col} {whens} ELSE '{FAMILY_ELSE}' END"


VIEWS = [
    ("v_ai_complaints", f"""
CREATE OR REPLACE VIEW v_ai_complaints AS
SELECT c.review_id, c.review_date, c.sku, c.asin, c.parent, c.product, c.title,
       {family_sql('c.parent')}            AS product_family,
       c.country, c.source, c.order_id, c.review_text,
       c.complaint                          AS ai_complaint,
       c.category                           AS ai_category,
       c.complaint,
       c.category,
       c.needs_review, c.is_new_code, c.run_id, c.second_complaint,
       {HAS_PROSE}                          AS has_prose,
       CASE WHEN dup.first_id IS NULL OR dup.first_id = c.review_id
            THEN 1 ELSE 2 END              AS dup_rank,
       COALESCE(dup.n, 1)                   AS dup_group,
       CASE WHEN k.review_id IS NULL THEN 0 ELSE 1 END AS human_validated,
       k.new_complaint                      AS human_complaint,
       k.decision                           AS human_decision,
       CASE WHEN k.decision = 'rejected' THEN NULL
            ELSE COALESCE(kc.code, NULLIF(k.new_complaint, ''), c.complaint) END
                                            AS final_complaint,
       CASE WHEN k.decision = 'rejected' THEN NULL
            ELSE COALESCE(kc.category, c.category) END AS final_category
  FROM classifications c
  LEFT JOIN {DUP_TABLE} dup
         ON dup.grp = CASE WHEN c.order_id IS NOT NULL AND TRIM(c.order_id) <> ''
                           THEN CONCAT('o|', TRIM(c.order_id))
                           ELSE CONCAT('s|', IFNULL(c.sku, ''), '|', IFNULL(c.review_date, '')) END
        AND dup.h = MD5(TRIM(c.review_text))
  LEFT JOIN (SELECT review_id, MAX(id) AS last_id
               FROM corrections GROUP BY review_id) last ON last.review_id = c.review_id
  LEFT JOIN corrections k ON k.id = last.last_id
  -- Case-insensitive on purpose: the taxonomy spelling wins. One row per
  -- lower(code) so a duplicate cannot multiply this view's row count.
  LEFT JOIN (SELECT LOWER(code) lc, MIN(code) code, MAX(category) category
               FROM complaint_codes GROUP BY LOWER(code)) kc
         ON kc.lc = LOWER(COALESCE(NULLIF(k.new_complaint, ''), c.complaint))
"""),
    ("v_ai_complaints_monthly", f"""
CREATE OR REPLACE VIEW v_ai_complaints_monthly AS
SELECT v.sku,
       MAX(v.asin)                         AS asin,
       MAX(v.parent)                       AS parent,
       MAX(v.product)                      AS product,
       MAX(v.title)                        AS title,
       MAX(ts.title_short)                 AS title_short,
       MAX({family_sql('v.parent')})        AS product_family,
       DATE_FORMAT(v.review_date, '%Y-%m') AS month,
       CONCAT(YEAR(v.review_date), '-Q', QUARTER(v.review_date)) AS quarter,
       v.final_complaint                    AS complaint,
       MAX(v.final_category)               AS category,
       COUNT(*)                            AS n,
       ROUND(COUNT(*) * 100.0 /
             SUM(COUNT(*)) OVER (PARTITION BY v.sku, DATE_FORMAT(v.review_date, '%Y-%m')), 2)
                                          AS pct_of_sku_month,
       SUM(v.human_validated)              AS n_human_validated,
       -- Repeats are disclosed at this grain too (F6): the rollup counts them in
       -- n, and this column is the only way to see how many of the n are the same
       -- comment recorded twice on one order.
       COALESCE(SUM(v.dup_rank > 1), 0)     AS n_repeat
  FROM v_ai_complaints v
  -- Modal title per SKU, computed once per refresh (F10). MAX() over titles was
  -- lexical: sku 7 displayed its 4-row French listing instead of the 649-row
  -- English one. The join is 1:1 on sku, so the grain cannot split.
  LEFT JOIN {TITLE_TABLE} ts ON ts.sku = v.sku
 WHERE v.final_complaint IS NOT NULL
   AND v.review_date IS NOT NULL
   AND v.final_complaint <> '{PROCESS_NOISE}'
   AND (v.final_category IS NULL OR v.final_category NOT IN ({','.join(repr(c) for c in BAD_CATEGORIES)}))
 -- grain: sku x month x complaint, nothing else. title/asin/product/parent vary
 -- within a SKU and MUST stay out of this list (they are MAX()ed above).
 GROUP BY v.sku, DATE_FORMAT(v.review_date, '%Y-%m'),
          CONCAT(YEAR(v.review_date), '-Q', QUARTER(v.review_date)),
          v.final_complaint
"""),
    # The reviews the rule dropped as process noise. They are excluded from
    # complaint reporting, so nothing can see them unless they are listed: a
    # deleted complaint is invisible, which is the worst failure mode for a
    # reporting dataset.
    ("v_rule_deletions", """
CREATE OR REPLACE VIEW v_rule_deletions AS
SELECT review_id, review_date, sku, product, source,
       CHAR_LENGTH(COALESCE(review_text,'')) AS text_len,
       LEFT(COALESCE(review_text,''), 300)   AS review_text
  FROM classifications
 WHERE complaint = 'Review Deletion'
"""),
    # THE RATE VIEW. Same rows and the same complaint counts as
    # v_ai_complaints_monthly (it is that view LEFT JOINed on a primary key), plus
    # the exposure denominator and the ratios.
    #
    # The denominator is automation_philip.sales_by_sku_month: the seller's own
    # units/orders/refunds per SKU per month, built by build_sales.py from
    # amzmanager.finance_by_products (product_id -> products.id -> products.sku,
    # the same seller SKU classifications.sku came from).
    #
    # NULL means "not computable", never zero: NULLIF(denominator,0) turns both a
    # missing sales row and a real zero-order month into NULL. Showing 0.00 would
    # claim "no complaints per order" when the truth is "no orders to divide by",
    # and dividing without NULLIF is a division by zero the moment a SKU-month has
    # finance rows and no orders (561 such SKU-months exist today).
    #
    # n_complaints is the count for THIS complaint code, so complaints_per_100_orders
    # on a row is that code's rate. The SKU-month's all-code rate is the sum of the
    # numerators over the one denominator, not an average of these rows — the app's
    # mix panel does exactly that sum.
    ("v_complaint_rates", f"""
CREATE OR REPLACE VIEW v_complaint_rates AS
SELECT m.sku,
       m.asin,
       m.parent,
       m.product,
       m.title,
       m.title_short,
       m.product_family,
       m.month,
       m.quarter,
       m.complaint,
       m.category,
       m.n                                  AS n_complaints,
       m.pct_of_sku_month,
       m.n_human_validated,
       m.n_repeat,
       s.orders,
       s.units,
       s.refunds,
       ROUND(m.n * 100.0  / NULLIF(s.orders, 0), 2) AS complaints_per_100_orders,
       ROUND(m.n * 1000.0 / NULLIF(s.units, 0), 2)  AS complaints_per_1000_units,
       -- QUOTABILITY (R12). A ratio whose denominator is a handful of orders is
       -- arithmetic, not information: SC-XL-B sold 2 US orders in 2026-07 and
       -- booked 54 refunds, so an unguarded ratio printed 2700% beside rows that
       -- are ordinary levels. A rate is quoted only at >= 100 orders; below that
       -- the flags say so and the app prints the denominator instead.
       CASE WHEN s.orders >= {RATE_FLOOR} THEN 1 ELSE 0 END AS rate_quotable,
       CASE WHEN s.orders_us >= {RATE_FLOOR} THEN 1 ELSE 0 END AS refund_rate_quotable,
       ROUND(s.refunds * 100.0 / NULLIF(s.orders, 0), 2) AS refund_rate_pct,
       -- The feed only populates refunds for US and CA (US 8.4%, CA 9.5%,
       -- every EU marketplace ~0.2% in 2026), so refund_rate_pct divides a
       -- mostly-US numerator by a WORLDWIDE denominator: it is a series, not a
       -- level. This is the same-market figure and the one to quote as a rate.
       -- NULL below the floor: 54/2 is not a 2700% return rate, and a pivot that
       -- averages it would report a number nobody measured.
       CASE WHEN s.orders_us >= {RATE_FLOOR}
            THEN ROUND(s.refunds_us * 100.0 / NULLIF(s.orders_us, 0), 2)
            ELSE NULL END                         AS refund_rate_us_pct,
       s.orders_us,
       s.units_us,
       s.refunds_us,
       CASE WHEN s.sku IS NULL THEN 0 ELSE 1 END    AS has_sales
  FROM v_ai_complaints_monthly m
  LEFT JOIN {SALES_TABLE} s
         ON s.sku = m.sku AND s.month = m.month
"""),
    # THE PER-SOURCE RATE (R4). The all-source rate is a composition of feeds:
    # ncx stops on 2026-07-16 and amazon on 2026-08-29, so a month-over-month
    # change in the all-source figure partly measures which feeds were running.
    # fba is the only feed present every month, and it is the only series a
    # month-over-month comparison may use. Same grain as v_complaint_rates plus
    # `source`; the denominator is still the SKU-month's whole order count, because
    # every order could have produced a complaint from any feed.
    ("v_complaint_rates_by_source", f"""
CREATE OR REPLACE VIEW v_complaint_rates_by_source AS
SELECT m.sku,
       m.parent,
       m.product,
       m.product_family,
       m.month,
       m.source,
       m.complaint,
       m.category,
       m.n                                  AS n_complaints,
       s.orders,
       s.units,
       s.refunds_us,
       s.orders_us,
       ROUND(m.n * 100.0 / NULLIF(s.orders, 0), 2) AS complaints_per_100_orders,
       CASE WHEN s.orders >= {RATE_FLOOR} THEN 1 ELSE 0 END AS rate_quotable,
       CASE WHEN s.sku IS NULL THEN 0 ELSE 1 END    AS has_sales
  FROM (SELECT v.sku,
               MAX(v.parent)                       AS parent,
               MAX(v.product)                      AS product,
               MAX({family_sql('v.parent')})        AS product_family,
               DATE_FORMAT(v.review_date, '%Y-%m') AS month,
               v.source,
               v.final_complaint                   AS complaint,
               MAX(v.final_category)               AS category,
               COUNT(*)                            AS n
          FROM v_ai_complaints v
         WHERE v.final_complaint IS NOT NULL
           AND v.review_date IS NOT NULL
           AND v.final_complaint <> '{PROCESS_NOISE}'
           AND (v.final_category IS NULL OR v.final_category NOT IN ({','.join(repr(c) for c in BAD_CATEGORIES)}))
         GROUP BY v.sku, DATE_FORMAT(v.review_date, '%Y-%m'), v.source, v.final_complaint) m
  LEFT JOIN {SALES_TABLE} s
         ON s.sku = m.sku AND s.month = m.month
"""),
    # THE PIVOT GRAIN (R1). One row per SKU-month with the all-code numerator and
    # the denominator on the same row. v_complaint_rates repeats orders/units on
    # every complaint-code row, so SUM(orders) over a pivot multiplies the
    # denominator by the number of codes (28x on one SKU-month; seat cushions
    # 2026-07 summed to 552,965 orders against a true 32,848). This view is the
    # safe thing to pivot and the backing table for /monthly_sku.csv.
    ("v_sku_month_rates", f"""
CREATE OR REPLACE VIEW v_sku_month_rates AS
SELECT m.sku,
       m.asin,
       m.parent,
       m.product,
       m.title,
       m.title_short,
       m.product_family,
       m.month,
       m.quarter,
       m.n_complaints,
       m.n_codes,
       m.n_human_validated,
       m.n_repeat,
       s.orders,
       s.units,
       s.refunds,
       s.refunds_us,
       s.orders_us,
       s.units_us,
       ROUND(m.n_complaints * 100.0  / NULLIF(s.orders, 0), 2) AS complaints_per_100_orders,
       ROUND(m.n_complaints * 1000.0 / NULLIF(s.units, 0), 2)  AS complaints_per_1000_units,
       CASE WHEN s.orders >= {RATE_FLOOR} THEN 1 ELSE 0 END    AS rate_quotable,
       CASE WHEN s.orders_us >= {RATE_FLOOR} THEN 1 ELSE 0 END AS refund_rate_quotable,
       CASE WHEN s.orders_us >= {RATE_FLOOR}
            THEN ROUND(s.refunds_us * 100.0 / NULLIF(s.orders_us, 0), 2)
            ELSE NULL END                                     AS refund_rate_us_pct,
       CASE WHEN s.sku IS NULL THEN 0 ELSE 1 END              AS has_sales
  FROM (SELECT sku, MAX(asin) asin, MAX(parent) parent, MAX(product) product,
               MAX(title) title, MAX(title_short) title_short,
               MAX(product_family) product_family, month, MAX(quarter) quarter,
               SUM(n_complaints) n_complaints, COUNT(*) n_codes,
               SUM(n_human_validated) n_human_validated, SUM(n_repeat) n_repeat
          FROM v_complaint_rates
         GROUP BY sku, month) m
  LEFT JOIN {SALES_TABLE} s
         ON s.sku = m.sku AND s.month = m.month
"""),
]


def check_monthly_grain(cur):
    """Fail the refresh if the SKU x month x complaint grain is split.

    A duplicate grain is invisible on screen (the pivot just lists the same
    complaint twice per SKU-month) and it silently halves every percentage, so
    this is an assertion, not a report.
    """
    cur.execute("""SELECT COUNT(*) AS rows_n,
                          COUNT(DISTINCT CONCAT_WS('~', IFNULL(sku,''),
                                                   IFNULL(month,''), IFNULL(complaint,''))) AS grain_n
                     FROM v_ai_complaints_monthly""")
    r = cur.fetchone()
    rows_n, grain_n = int(r[0]), int(r[1])
    if rows_n != grain_n:
        cur.execute("""SELECT sku, month, complaint, COUNT(*) c
                         FROM v_ai_complaints_monthly
                        GROUP BY sku, month, complaint
                       HAVING c > 1 ORDER BY c DESC LIMIT 5""")
        worst = "; ".join(f"{s}/{m}/{c} x{n}" for s, m, c, n in cur.fetchall())
        raise SystemExit(
            f"v_ai_complaints_monthly grain is split: {rows_n} rows for {grain_n} "
            f"(sku, month, complaint) groups. Worst: {worst or 'n/a'}")
    return rows_n, grain_n


def check_label_casing(cur):
    """Fail if one code is stored under two spellings.

    'Not As Advertised/misleading' vs 'Not as Advertised/Misleading' are one
    label; two spellings in a pivot are two labels and every share is halved.
    The view normalises through complaint_codes, so this must stay empty.
    """
    cur.execute("""SELECT LOWER(final_complaint) lc, COUNT(DISTINCT final_complaint) k,
                          GROUP_CONCAT(DISTINCT final_complaint SEPARATOR ' | ') spellings
                     FROM v_ai_complaints
                    WHERE final_complaint IS NOT NULL
                    GROUP BY LOWER(final_complaint) HAVING k > 1""")
    bad = cur.fetchall()
    if bad:
        raise SystemExit("final_complaint has two spellings for one code: "
                         + "; ".join(f"{b[2]}" for b in bad))


def check_duplicate_flag(cur):
    """Fail if the duplicate flag is not what it claims (F2).

    dup_rank > 1 may only mean "this comment is already recorded on the same
    ORDER". The old key (text + SKU + date) merged different customers who typed
    the same short phrase on the same day, so this asserts the order, not the
    text: every repeat row must carry an order_id, and no (text) may be flagged
    across two orders.
    """
    cur.execute("""SELECT COUNT(*) n FROM v_ai_complaints
                    WHERE dup_rank < 1 OR dup_rank > 2""")
    bad = int(cur.fetchone()[0])
    if bad:
        raise SystemExit(f"duplicate flag is wrong on {bad} row(s): dup_rank outside 1..2")
    # A repeated row without an order_id could only come from the SKU/date
    # fallback, which this corpus does not exercise; if it ever does, it must be
    # visible rather than silently counted as a repeat.
    cur.execute("""SELECT COUNT(*) n FROM v_ai_complaints
                    WHERE dup_rank > 1 AND (order_id IS NULL OR TRIM(order_id) = '')""")
    bad = int(cur.fetchone()[0])
    if bad:
        raise SystemExit(f"{bad} row(s) are flagged as repeats with no order_id — the "
                         f"duplicate key has fallen back to SKU+date, which merges customers")
    # The claim the footer makes, checked row by row: a repeat has a twin on the
    # SAME order with the same text. The old key's failure was merging rows whose
    # only common ground was SKU + date, so this is the assertion that matters.
    cur.execute("""SELECT COUNT(*) n FROM v_ai_complaints a
                    LEFT JOIN (SELECT order_id, MD5(TRIM(review_text)) h, COUNT(*) k
                                 FROM classifications
                                GROUP BY order_id, MD5(TRIM(review_text))) d
                      ON d.order_id = a.order_id
                     AND d.h = MD5(TRIM(a.review_text))
                    WHERE a.dup_rank > 1 AND (d.k IS NULL OR d.k < 2)""")
    bad = int(cur.fetchone()[0])
    if bad:
        raise SystemExit(f"{bad} row(s) are flagged as repeats with no twin on the same order")
    # And what the old key would have merged: (SKU, date, text) groups spanning two
    # or more orders. Reported, not asserted — it is the size of the defect this
    # key replaced.
    cur.execute("""SELECT COUNT(*) n FROM (
                     SELECT sku, review_date, MD5(TRIM(review_text)) h,
                            COUNT(DISTINCT order_id) o
                       FROM classifications
                      WHERE review_text IS NOT NULL AND TRIM(review_text) <> ''
                      GROUP BY sku, review_date, MD5(TRIM(review_text))
                     HAVING o > 1) x""")
    crossed = int(cur.fetchone()[0])
    cur.execute(f"""SELECT COUNT(*) n FROM (
                     SELECT g.grp, g.h, g.n,
                            COUNT(v.review_id) seen, SUM(v.dup_rank = 1) ones
                       FROM {DUP_TABLE} g
                       LEFT JOIN v_ai_complaints v
                         ON v.dup_rank >= 1
                        AND MD5(TRIM(v.review_text)) = g.h
                        AND (CASE WHEN v.order_id IS NOT NULL AND TRIM(v.order_id) <> ''
                                  THEN CONCAT('o|', TRIM(v.order_id))
                                  ELSE CONCAT('s|', IFNULL(v.sku, ''), '|',
                                              IFNULL(v.review_date, '')) END) = g.grp
                      GROUP BY g.grp, g.h, g.n
                     HAVING ones <> 1 OR seen <> g.n) x""")
    bad = int(cur.fetchone()[0])
    if bad:
        raise SystemExit(f"{bad} duplicate group(s) do not have exactly one rank-1 row")
    cur.execute("""SELECT COUNT(*) n, COALESCE(SUM(dup_rank > 1), 0) repeats
                     FROM v_ai_complaints""")
    n, repeats = cur.fetchone()
    return int(n), int(repeats), crossed


def check_monthly_titles(cur):
    """Fail if a SKU with a title has no short title for the SKU column."""
    cur.execute("""SELECT COUNT(*) n FROM v_ai_complaints_monthly
                    WHERE title IS NOT NULL AND TRIM(title) <> ''
                      AND (title_short IS NULL OR TRIM(title_short) = '')""")
    bad = int(cur.fetchone()[0])
    if bad:
        raise SystemExit(f"{bad} monthly row(s) carry a title but no title_short")
    return bad


def refresh_duplicates(cur):
    """Rebuild the repeated-comment groups into their table, atomically.

    The key is (order_id, MD5(text)) — the same comment recorded twice on the same
    order — with (sku, review_date, MD5(text)) only for the rows that carry no
    order_id. See the DUP_KEY comment for what the old text+SKU+date key merged.

    Builds into `review_duplicates_new`, then swaps it in with ONE RENAME, so a
    failed rebuild leaves the last good flag in place. Delete-then-insert under
    autocommit would have left the table empty on failure, and an empty table
    silently marks every row 'not a duplicate' — the failure mode that looks
    like data rather than like an error.
    """
    cur.execute(f"DROP TABLE IF EXISTS {DUP_TABLE}_new")
    cur.execute(DUP_TABLE_DDL.format(t=f"{DUP_TABLE}_new"))
    cur.execute(f"""INSERT INTO {DUP_TABLE}_new (grp, h, n, first_id, review_date)
                    SELECT {DUP_KEY} grp, MD5(TRIM(c.review_text)) h, COUNT(*),
                           MIN(c.review_id), MAX(c.review_date)
                     FROM classifications c
                    WHERE c.review_text IS NOT NULL AND TRIM(c.review_text) <> ''
                    GROUP BY grp, h
                   HAVING COUNT(*) > 1""")
    cur.execute(f"SELECT COUNT(*), COALESCE(SUM(n),0), COALESCE(SUM(n-1),0) FROM {DUP_TABLE}_new")
    groups, rows, excess = cur.fetchone()
    cur.execute(DUP_TABLE_DDL.format(t=DUP_TABLE))     # exists on the first run
    # A run that died between RENAME and DROP leaves _old behind, and the next
    # RENAME would then fail on a name that already exists.
    cur.execute(f"DROP TABLE IF EXISTS {DUP_TABLE}_old")
    cur.execute(f"RENAME TABLE {DUP_TABLE} TO {DUP_TABLE}_old, "
                f"{DUP_TABLE}_new TO {DUP_TABLE}")
    cur.execute(f"DROP TABLE {DUP_TABLE}_old")
    return int(groups), int(rows), int(excess)


def refresh_titles(cur):
    """Rebuild the MODAL title per SKU into its table, atomically (F10).

    Modal, not MAX(): MAX() is lexical, so sku 7 took its 4-row French listing
    over the 649-row English one. ROW_NUMBER() over (sku, title) ordered by the
    row count picks the title most rows actually carry; the tie-break on title
    keeps it deterministic.
    """
    cur.execute(f"DROP TABLE IF EXISTS {TITLE_TABLE}_new")
    cur.execute(TITLE_TABLE_DDL.format(t=f"{TITLE_TABLE}_new"))
    cur.execute(f"""INSERT INTO {TITLE_TABLE}_new (sku, title, title_short, n)
                    SELECT sku, title, {TITLE_SHORT_SQL}, n
                      FROM (SELECT sku, title, COUNT(*) n,
                                   ROW_NUMBER() OVER (PARTITION BY sku
                                                      ORDER BY COUNT(*) DESC, title) rn
                              FROM classifications
                             WHERE sku IS NOT NULL AND title IS NOT NULL AND TRIM(title) <> ''
                             GROUP BY sku, title) t
                     WHERE rn = 1""")
    cur.execute(f"SELECT COUNT(*) FROM {TITLE_TABLE}_new")
    n = int(cur.fetchone()[0])
    cur.execute(TITLE_TABLE_DDL.format(t=TITLE_TABLE))
    cur.execute(f"DROP TABLE IF EXISTS {TITLE_TABLE}_old")
    cur.execute(f"RENAME TABLE {TITLE_TABLE} TO {TITLE_TABLE}_old, "
                f"{TITLE_TABLE}_new TO {TITLE_TABLE}")
    cur.execute(f"DROP TABLE {TITLE_TABLE}_old")
    return n


def check_rate_nulls(cur):
    """Assert the rate view did not move a count and never fakes a zero.

    Three failures this exists to catch, all silent without it:
      * the LEFT JOIN fanning out (a SKU duplicated in the sales table) — every
        complaint count in the app would double;
      * a ratio computed against no denominator, shown as 0.00 "complaints per
        order" when the truth is "no orders";
      * a division by zero on a finance row with orders = 0.
    """
    cur.execute("""SELECT COUNT(*) c, COALESCE(SUM(n),0) s FROM v_ai_complaints_monthly""")
    a = cur.fetchone()
    cur.execute("""SELECT COUNT(*) c, COALESCE(SUM(n_complaints),0) s FROM v_complaint_rates""")
    b = cur.fetchone()
    if int(a[0]) != int(b[0]) or int(a[1]) != int(b[1]):
        raise SystemExit(f"rate view changed the complaint grain: v_ai_complaints_monthly "
                         f"{int(a[0]):,} rows / {int(a[1]):,} records vs "
                         f"v_complaint_rates {int(b[0]):,} / {int(b[1]):,}")
    cur.execute("""SELECT
        SUM(orders > 0  AND complaints_per_100_orders IS NULL)  bad_pos,
        SUM((orders IS NULL OR orders = 0) AND complaints_per_100_orders IS NOT NULL) bad_zero,
        SUM(units  > 0  AND complaints_per_1000_units IS NULL)  bad_pos_u,
        SUM((units  IS NULL OR units  = 0) AND complaints_per_1000_units IS NOT NULL) bad_zero_u,
        SUM(orders IS NULL) no_sales_rows,
        SUM(orders = 0)     zero_order_rows,
        SUM(has_sales = 1)  covered_rows,
        COUNT(*)            all_rows
      FROM v_complaint_rates""")
    r = cur.fetchone()
    bad = [int(x or 0) for x in r[:4]]
    if any(bad):
        raise SystemExit(f"rate view has ratios against no denominator: positives {bad[0]}, "
                         f"zeros {bad[1]}, units positives {bad[2]}, units zeros {bad[3]}")

    # R12: a refund ratio above the volume floor only, and never a value where the
    # flag says the denominator is too thin. 2700% beside ordinary rows is not a
    # level, it is two orders and 54 refunds.
    cur.execute("""SELECT SUM(refund_rate_us_pct IS NOT NULL AND refund_rate_quotable = 0) a,
                          SUM(refund_rate_us_pct < 0 OR refund_rate_us_pct > 100) b
                     FROM v_complaint_rates WHERE refund_rate_us_pct IS NOT NULL""")
    q = cur.fetchone()
    if int(q[0] or 0) or int(q[1] or 0):
        raise SystemExit(f"refund_rate_us_pct escapes the quotable floor: "
                         f"{int(q[0] or 0)} below-floor values, {int(q[1] or 0)} outside 0..100")

    # The per-source view must carry exactly the same records and the same grain as
    # the daily rollup with `source` added — otherwise a per-source rate and the
    # all-source rate do not add up, and the fba-only series is not the same measure.
    cur.execute("""SELECT COUNT(*) c, COALESCE(SUM(n_complaints),0) s,
                          COUNT(DISTINCT CONCAT_WS('~', IFNULL(sku,''), IFNULL(month,''),
                                                   IFNULL(source,''), IFNULL(complaint,''))) g
                     FROM v_complaint_rates_by_source""")
    src = cur.fetchone()
    if int(src[1]) != int(b[1]):
        raise SystemExit(f"per-source view holds {int(src[1]):,} records, the monthly grain "
                         f"{int(b[1]):,} — a source is being dropped or double-counted")
    if int(src[2]) != int(src[0]):
        raise SystemExit(f"per-source view grain is split: {int(src[0]):,} rows for "
                         f"{int(src[2]):,} (sku, month, source, complaint) groups")

    # The sku-month view is the pivot grain: one row per SKU-month, counts unchanged.
    cur.execute("""SELECT COUNT(*) c, COALESCE(SUM(n_complaints),0) s,
                          COUNT(DISTINCT CONCAT_WS('~', IFNULL(sku,''), IFNULL(month,''))) g
                     FROM v_sku_month_rates""")
    sm = cur.fetchone()
    if int(sm[1]) != int(b[1]):
        raise SystemExit(f"sku-month view holds {int(sm[1]):,} records, the complaint grain "
                         f"{int(b[1]):,}")
    if int(sm[2]) != int(sm[0]):
        raise SystemExit(f"sku-month view is not one row per SKU-month: {int(sm[0]):,} rows, "
                         f"{int(sm[2]):,} groups")
    return {k: int(r[i] or 0) for i, k in zip(
        (4, 5, 6, 7), ("no_sales_rows", "zero_order_rows", "covered_rows", "all_rows"))}


def main():
    conn = pymysql.connect(host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
                           password=os.environ["CXAPP_DB_PASSWORD"], database=os.environ["CXAPP_DB_NAME"],
                           charset="utf8mb4", autocommit=True)
    cur = conn.cursor()
    # The duplicate groups must be in place BEFORE the view that joins them, or
    # CREATE VIEW fails on a fresh database. Same for the modal SKU titles.
    groups, rows, excess = refresh_duplicates(cur)
    print(f"{DUP_TABLE}: {groups:,} group(s) covering {rows:,} row(s) — {excess:,} repeat "
          f"row(s), keyed on order_id + MD5(text)")
    nt = refresh_titles(cur)
    print(f"{TITLE_TABLE}: {nt:,} SKU(s) carry their MODAL title (not MAX, which was lexical)")
    # The rate view cannot be built without its denominator, and a refresh that
    # silently skipped it would leave the app showing an old one.
    cur.execute("SHOW TABLES LIKE %s", (SALES_TABLE,))
    if not cur.fetchone():
        raise SystemExit(f"{SALES_TABLE} is missing — run build_sales.py before apply_views.py "
                         f"(cx_refresh.sh does this in order)")
    for name, ddl in VIEWS:
        cur.execute(ddl)
        cur.execute(f"SELECT COUNT(*) FROM {name}")
        print(f"{name}: created/replaced ({cur.fetchone()[0]:,} rows)")
    check_label_casing(cur)
    rows_n, grain_n = check_monthly_grain(cur)
    print(f"grain ok: {rows_n:,} rows = {grain_n:,} (sku, month, complaint) groups")
    n, repeats, crossed = check_duplicate_flag(cur)
    print(f"duplicate flag ok: {repeats:,} of {n:,} row(s) repeat a comment already recorded "
          f"on the same order — disclosed, not removed. The old SKU+date key would have "
          f"merged {crossed:,} (sku, date, text) groups that span more than one order.")
    rates = check_rate_nulls(cur)
    print(f"rate view ok: {rates['all_rows']:,} rows, {rates['covered_rows']:,} carry a sales "
          f"denominator, {rates['no_sales_rows']:,} have no sales row (rate NULL), "
          f"{rates['zero_order_rows']:,} have 0 orders (rate NULL, never 0.00)")
    check_monthly_titles(cur)
    print("sku titles ok: every titled SKU also carries a short title")
    conn.close()


if __name__ == "__main__":
    main()
