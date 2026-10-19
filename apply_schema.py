#!/usr/bin/env python3
"""
Apply app_schema.sql to the provisioned database and verify it landed.

Idempotent (CREATE TABLE IF NOT EXISTS / CREATE OR REPLACE VIEW), so re-running is
safe. Refuses to run against anything that is not the database we were given.
"""
import os, re, sys
import pymysql

SQL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_schema.sql")
EXPECTED = "automation_philip"


def main():
    host = os.environ["CXAPP_DB_HOST"]
    user = os.environ["CXAPP_DB_USER"]
    pw   = os.environ["CXAPP_DB_PASSWORD"]
    db   = os.environ["CXAPP_DB_NAME"]

    if db != EXPECTED:
        sys.exit(f"ABORT: CXAPP_DB_NAME is {db!r}, expected {EXPECTED!r}. Wrong database.")
    if "replica" in host:
        # Ab: writes on the replica degrade replication.
        sys.exit(f"ABORT: {host} is a replica. Writes go to the master only.")

    print(f"applying schema to {db} on {host}")
    c = pymysql.connect(host=host, user=user, password=pw, database=db, charset="utf8mb4")
    cur = c.cursor()

    raw = open(SQL).read()
    # Strip comments the way the server does — `--` followed by whitespace runs to
    # end of line, INLINE TOO. Splitting first and stripping later is wrong: an
    # inline comment can contain a ';' ("-- the runner-up; a genuine second code"),
    # which cuts the statement in half mid-comment and the server reports a syntax
    # error at a line that looks perfectly valid.
    stripped = re.sub(r"(^|\s)--\s.*$", "", raw, flags=re.M)
    stmts = [s.strip() for s in stripped.split(";") if s.strip()]
    print(f"{len(stmts)} statements")
    for s in stmts:
        first = " ".join(s.split()[:3])
        cur.execute(s)
        print("  ok:", first[:50])
    c.commit()

    print("\n=== tables now present ===")
    cur.execute("SHOW TABLES")
    for (t,) in cur.fetchall():
        cur.execute(f"SELECT COUNT(*) FROM `{t}`")
        print(f"  {t:20s} rows={cur.fetchone()[0]}")
    c.close()
    print("\ndone")


if __name__ == "__main__":
    main()
