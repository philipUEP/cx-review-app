"""Complaint-level classifier for the full unvalidated backlog.

Uses the shared vocabulary/prompt/rules (cxvocab.py) and bulk-pulled DB text
(data/reviews_text.csv), with API fallback. Applies Chadia's routing rules:
vocabulary additions, synonyms, and the Review Deletion rule.

Output: data/v3_output.csv
Run: set -a && . /opt/data/.env && set +a && uv run --with pandas python3 classify_v2.py
"""
import argparse
import csv
import json
import os
import threading
import time
import urllib.request
import concurrent.futures

import pandas as pd
from cxvocab import norm, title, cat_map, MENU_LINES, SYSTEM, canonical, rule_route

DATA = os.path.join(os.path.dirname(__file__), "data")
AMZ = "https://v2.amzmanager.net/api-v3/api.php"
DS = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-chat"
WORKERS = 4
# CX_OUT lets a menu/prompt change be re-classified into a SIDE file without
# clobbering data/v3_output.csv while the dashboards are being rebuilt from it
# (the runner skips ids already present, so it can never re-label them in place).
OUT = os.environ.get("CX_OUT") or os.path.join(DATA, "v3_output.csv")
FIELDS = ["review_id", "product", "parent", "sku", "asin", "order_id", "country",
          "date", "source", "text", "complaint", "category"]

# bulk-pulled review text from DB (API fallback for anything missing)
TEXT_FILE = os.path.join(DATA, "reviews_text.csv")
text_lookup = {}
if os.path.exists(TEXT_FILE):
    for _rid, _text in pd.read_csv(TEXT_FILE, usecols=["review_id", "text"]).itertuples(index=False):
        text_lookup[int(_rid)] = _text if isinstance(_text, str) else ""
print(f"vocab: {len(cat_map)} complaints; loaded {len(text_lookup)} texts from DB", flush=True)

def amz_token():
    p = os.path.join(os.path.dirname(__file__), ".amz_token")
    if os.path.exists(p):
        return open(p).read().strip()
    return os.environ["AMZ_TOKEN"]

def fetch_text(rid):
    for attempt in range(5):
        try:
            req = urllib.request.Request(f"{AMZ}?action=CXGetReview",
                data=json.dumps({"id": int(rid)}).encode(),
                headers={"Authorization": f"Bearer {amz_token()}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if attempt == 4:
                raise
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("fetch failed")

_rate_lock = threading.Lock()
_last_call = [0.0]

def throttle(min_interval=0.5):
    with _rate_lock:
        now = time.time()
        wait = _last_call[0] + min_interval - now
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()

def classify(text, product):
    throttle()
    user = MENU_LINES + f"\n\nProduct: {product}\nReview: {text}\n\nReturn the complaint."
    body = {"model": MODEL,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": user}],
            "temperature": 0, "max_tokens": 800,
            "response_format": {"type": "json_object"}}
    req = urllib.request.Request(DS, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.load(r)
    c = resp["choices"][0]["message"].get("content") or ""
    try:
        return json.loads(c).get("complaint", "")
    except Exception:
        return c.strip()[:100]

def process(r):
    rid = r["id"]
    product = r.get("shortname") or ""
    base = {"review_id": rid, "product": product, "parent": r.get("parent_shortname") or "",
            "sku": r.get("sku") or "", "asin": r.get("asin") or "",
            "order_id": r.get("order_id") or "", "country": r.get("country_shortname") or "",
            "date": r.get("date_publishing") or "", "source": r.get("type") or "",
            "text": "", "complaint": "", "category": ""}
    text = text_lookup.get(int(rid))
    if text is None:
        try:
            d = fetch_text(rid)
            text = ((d.get("title") or "") + " " + (d.get("body") or "")).strip()
        except Exception:
            base["category"] = "ERR"
            return base
    base["text"] = text
    if not text:
        base["category"] = "NO_TEXT"
        return base
    # rule-based routing first (Review Deletion is a process label, not a complaint)
    rr = rule_route(text)
    if rr:
        base["complaint"] = rr
        base["category"] = "Review Deletion"
        return base
    for attempt in range(3):
        try:
            cname = classify(text, product)
            ck = canonical(cname)
            if not ck:
                # Blank content = the model spent its whole budget reasoning and
                # returned nothing. Treat as a FAILURE so it retries and then shows
                # up as ERR: previously it fell through to category "NEW" with an
                # empty complaint, which hid 33 rows inside a clean-looking run.
                raise ValueError("blank classification (reasoning budget too small?)")
            if ck in cat_map:
                base["complaint"] = title(ck)
                base["category"] = cat_map[ck]
            else:
                base["complaint"] = cname
                base["category"] = "NEW"
            return base
        except Exception:
            time.sleep(2 * (attempt + 1))
    base["category"] = "ERR"
    return base

def backlog_path():
    p = os.environ.get("CX_BACKLOG", os.path.join(DATA, "api_reviews.json"))
    return p if os.path.isabs(p) else os.path.join(os.path.dirname(__file__), p)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20000)
    ap.add_argument("--max", type=int, default=0,
                    help="process at most this many reviews then exit cleanly (for bounded cron ticks)")
    args = ap.parse_args()
    revs = json.load(open(backlog_path()))
    unval = [r for r in revs if not r.get("complaints") and not r.get("has_complaint")]
    print(f"unvalidated backlog: {len(unval)}", flush=True)

    pilot = unval[:args.limit]
    done_ids = set()
    if os.path.exists(OUT):
        done_ids = set(pd.read_csv(OUT)["review_id"].astype(str))
    pending = [r for r in pilot if str(r["id"]) not in done_ids]
    todo = len(pending)
    if args.max and len(pending) > args.max:
        pending = pending[:args.max]  # bounded batch; the rest resumes next tick
    # Report the REAL backlog, not len(pilot)-len(pending): after truncation that
    # subtraction printed "already done 13,523" when 700 rows were actually done.
    print(f"already done {len(pilot)-todo}; to run {len(pending)} (of {todo} pending)", flush=True)

    if not os.path.exists(OUT) or os.path.getsize(OUT) == 0:
        with open(OUT, "w", newline="") as f:
            csv.writer(f).writerow(FIELDS)

    lock = threading.Lock()
    n = [0]
    errs = [0]
    def work(r):
        res = process(r)
        with lock:
            with open(OUT, "a", newline="") as f:
                csv.writer(f).writerow([res.get(k, "") for k in FIELDS])
            n[0] += 1
            if res.get("category") == "ERR":
                errs[0] += 1
            if n[0] % 100 == 0:
                print(f"  {n[0]}/{len(pending)}", flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(work, pending))

    df = pd.read_csv(OUT)
    ok = df[~df["category"].isin(["ERR", "NO_TEXT", "NEW"])]
    new = df[df["category"] == "NEW"]
    rule = df[df["category"] == "Review Deletion"]
    print(f"\nclassified {len(ok)}/{len(df)} (incl. {len(rule)} rule-routed); "
          f"proposed-new {len(new)}; skipped {len(df)-len(ok)-len(new)}", flush=True)
    print("top complaints:", ok["complaint"].value_counts().head(12).to_dict(), flush=True)
    print(f"wrote {OUT}", flush=True)

    # Loud health check. This runs unattended on a 30-minute cron, so a silent
    # total failure (expired token, DB down, wrong model, blank responses) would
    # otherwise poison the backlog a batch at a time while every tick reports OK.
    # Exit non-zero so the failure is recorded rather than absorbed.
    if n[0] and errs[0] / n[0] > 0.20:
        print(f"ALERT: {errs[0]}/{n[0]} rows errored this batch "
              f"({errs[0] / n[0]:.0%}) — check token/DB/model before trusting output.",
              flush=True)
        raise SystemExit(1)
    print(f"batch health: {n[0] - errs[0]}/{n[0]} ok, {errs[0]} errors", flush=True)

if __name__ == "__main__":
    main()
