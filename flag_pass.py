"""Confidence flag pass — ADDITIVE, does not touch the validated primary output.

For each classified backlog review, ask whether a SECOND complaint also clearly
applies. needs_review=True when the model offers one.

Why this and not a rewritten classifier: the primary classification in
v3_output.csv is validated (60.9% vs Chadia, 60.3% held-out). Changing its
prompt would invalidate that and force a full re-validation. This pass writes a
SEPARATE artefact, so the primary stays exactly as measured.

Calibration (measured on Chadia's 389, the same prompt):
  no second code offered -> 65.8% correct  (68% of reviews)
  second code offered    -> 46.8% correct  (32% of reviews)
so the flag identifies the unreliable third of the backlog for human review.

Bounded (--max) and resumable so it can run under the gateway cron.
"""
import os, sys, json, threading, time
import concurrent.futures
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import top2_eval as T
import cxvocab as V

DATA = T.DATA
SRC = os.path.join(DATA, "v3_output.csv")
OUT = os.path.join(DATA, "review_flags.csv")
FIELDS = ["review_id", "product", "v3_complaint", "primary", "second", "needs_review"]

lock = threading.Lock()
n = [0]
errs = [0]

def main():
    ap_max = int(os.environ.get("CX_MAX", "0"))
    df = pd.read_csv(SRC)
    df["text"] = df["text"].fillna("").astype(str)
    # rule-routed and no-text rows never need a model call
    cand = df[(df["text"].str.len() > 0) &
              (~df["category"].isin(["ERR", "NO_TEXT", "Review Deletion"]))].copy()
    done = set()
    if os.path.exists(OUT):
        done = set(pd.read_csv(OUT)["review_id"].astype(str))
    pending = cand[~cand["review_id"].astype(str).isin(done)]
    if ap_max and len(pending) > ap_max:
        pending = pending.head(ap_max)
    print(f"candidates {len(cand)}; already flagged {len(done)}; to run {len(pending)}", flush=True)

    if not os.path.exists(OUT):
        with open(OUT, "w", newline="") as f:
            import csv as _csv
            _csv.writer(f).writerow(FIELDS)

    import csv as _csv
    def work(r):
        try:
            first, second = T.one_call(r["text"], r["product"] or "")
        except Exception:
            first, second = "ERR", ""
        with lock:
            needs = 1 if (str(second).strip() and str(second).strip() != str(first).strip()) else 0
            with open(OUT, "a", newline="") as f:
                _csv.writer(f).writerow([r["review_id"], r["product"],
                                         r.get("complaint", ""), first, second, needs])
            n[0] += 1
            if first == "ERR":
                errs[0] += 1
            if n[0] % 100 == 0:
                print(f"  {n[0]}/{len(pending)}", flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(work, [r for _, r in pending.iterrows()]))

    print(f"flagged {n[0]} rows, {errs[0]} errors -> {OUT}", flush=True)
    if n[0] and errs[0] / n[0] > 0.20:
        print(f"ALERT: {errs[0]}/{n[0]} errored ({errs[0]/n[0]:.0%}) — check API/model.", flush=True)
        raise SystemExit(1)

    # BLANK-PREDICTION ABORT. A reasoning-capable model given too small a
    # max_tokens spends the whole budget in reasoning and returns empty content on
    # EVERY row, and the pass still writes a full, tidy, entirely blank file. That
    # happened here: 16,840 blank rows written and reported as success. Never let a
    # blank run be recorded as done — the ids would then be skipped forever.
    try:
        import pandas as _pd
        _f = _pd.read_csv(OUT)
        _blank = _f["primary"].fillna("").astype(str).str.strip() == ""
        if len(_f) and _blank.mean() > 0.5:
            raise SystemExit(
                f"ABORT: {int(_blank.sum())}/{len(_f)} primaries are BLANK in {OUT}. "
                f"Config error (model={T.C.MODEL}, max_tokens={T.C.MAXTOK}), not a result. "
                f"A reasoning model needs a much larger budget. Delete the output before re-running."
            )
    except SystemExit:
        raise
    except Exception as _e:
        print(f"WARN: could not verify flags output ({_e})", flush=True)

if __name__ == "__main__":
    main()
