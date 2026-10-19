#!/usr/bin/env python3
"""Bring automation_philip's ops tables up to the schema the ops scripts expect.

Two columns exist only because of review findings, and both are additive so this
can run on every refresh tick:

  corrections.source          ENUM('sheet','app') NOT NULL DEFAULT 'app'
      Which surface made the decision. The review app is the declared only review
      surface (P9); cx_refresh.sh still pulls the retired Google sheet, so a
      decision can exist on both sides. Without a source stamp the sync cannot
      tell "the sheet is the newer decision" from "the sheet is stale and about to
      overwrite the app".

      DEFAULT 'app' is deliberate: any writer that does not name a source is the
      app (app.py is the only other INSERT), and the sheet path always stamps
      'sheet' explicitly. Defaulting to 'sheet' would mark every in-app decision as
      a sheet decision and silently re-open the overwrite this column exists to
      prevent.

  classifications.prompt_version VARCHAR(40) NULL
  classifications.prompt_hash    CHAR(12)  NULL
      Provenance for the label itself (P10b). runs.prompt_version records what the
      *run* claimed; nothing tied an individual row to the prompt that produced
      it, so "which prompt made this label" was unanswerable after a menu change.
      The hash is the first 12 hex chars of sha256(SYSTEM + menu) from cxvocab.py,
      so a version label reused with an edited menu still shows up as a difference.

Idempotent: each column is added only when information_schema says it is absent.
Safe to run while the app is up (instant ADD COLUMN on MySQL 8 / MariaDB).
Exit 0 when the schema is already current.
"""
import os
import sys

import pymysql


def cols(cur, table):
    cur.execute("""SELECT COLUMN_NAME FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s""", (table,))
    return {r[0] for r in cur.fetchall()}


def indexes(cur, table):
    cur.execute("""SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s""", (table,))
    return {r[0] for r in cur.fetchall()}


def add_column(cur, table, name, ddl):
    if name in cols(cur, table):
        print(f"  {table}.{name}: present")
        return False
    cur.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    print(f"  {table}.{name}: ADDED")
    return True


def add_index(cur, table, name, ddl):
    if name in indexes(cur, table):
        print(f"  {table} index {name}: present")
        return False
    cur.execute(f"ALTER TABLE {table} ADD INDEX {name} {ddl}")
    print(f"  {table} index {name}: ADDED")
    return True


def main():
    conn = pymysql.connect(
        host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
        password=os.environ["CXAPP_DB_PASSWORD"], database=os.environ["CXAPP_DB_NAME"],
        charset="utf8mb4", autocommit=True)
    cur = conn.cursor()
    cur.execute("SELECT DATABASE()")
    print(f"schema: {cur.fetchone()[0]}")

    changed = False
    changed |= add_column(
        cur, "corrections", "source",
        "`source` ENUM('sheet','app') NOT NULL DEFAULT 'app' AFTER `note`")
    # The guard reads the latest correction per review ordered by created_at;
    # without this index that is a filesort per row on a growing table.
    changed |= add_index(cur, "corrections", "idx_review_created", "(`review_id`,`created_at`)")

    changed |= add_column(cur, "classifications", "prompt_version",
                          "`prompt_version` VARCHAR(40) DEFAULT NULL AFTER `run_id`")
    changed |= add_column(cur, "classifications", "prompt_hash",
                          "`prompt_hash` CHAR(12) DEFAULT NULL AFTER `prompt_version`")
    changed |= add_index(cur, "classifications", "idx_prompt_version", "(`prompt_version`)")

    # Report the stamp distribution so a refresh log always shows provenance.
    cur.execute("SELECT source, COUNT(*) FROM corrections GROUP BY source")
    print("  corrections by source:", dict(cur.fetchall()) or "(empty)")
    cur.execute("""SELECT COALESCE(prompt_version,'(unstamped)') v, COUNT(*)
                     FROM classifications GROUP BY v ORDER BY 2 DESC""")
    print("  classifications by prompt_version:", dict(cur.fetchall()))
    conn.close()
    print("schema: " + ("updated" if changed else "already current"))


if __name__ == "__main__":
    sys.exit(main())
