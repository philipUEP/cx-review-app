"""Shared CX complaint vocabulary, prompt, and routing rules.

Sources: Chadia's two audit CSVs (generic + non-generic) plus her approved
additions from the Sep 22 verification feedback (data/vocab_additions.csv).
Used by both the full-backlog runner (classify_v2.py) and the sample
re-scorer (rescore_sample.py).
"""
import os
import json
import pandas as pd

DATA = os.path.join(os.path.dirname(__file__), "data")
_HERE = os.path.dirname(os.path.abspath(__file__))
# Taxonomy can live in three places, in order: an explicit override, the shared
# attachments dir on this host, or the copy vendored beside this file in the repo.
# Without the fallback a fresh clone dies on import.
_TAX_DIRS = (os.environ.get("CX_TAXONOMY_DIR"), "/opt/data/attachments",
             os.path.join(_HERE, "taxonomy"))


def _vocab(*names):
    for d in _TAX_DIRS:
        if not d:
            continue
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return p
    return os.path.join(_HERE, "taxonomy", names[0])


GEN = _vocab("Complaint Validation Audit  - Generic Set of Complaints Types.csv",
             "Complaint Validation Audit  - Generic Set of Complaints .csv")
NGEN = _vocab("Complaint Validation Audit  - Non generic Complaints Types.csv")
ADDITIONS = (os.environ.get("CX_VOCAB_ADDITIONS")
             or next((p for p in (os.path.join(DATA, "vocab_additions.csv"),
                                  os.path.join(_HERE, "taxonomy", "vocab_additions.csv"))
                      if os.path.exists(p)),
                     os.path.join(DATA, "vocab_additions.csv")))

def norm(s):
    if pd.isna(s):
        return ""
    return " ".join(str(s).strip().lower().split())

def title(s):
    return " ".join(w.capitalize() for w in str(s).split())

def load_vocab(path):
    df = pd.read_csv(path)
    out = {}
    for _, r in df.iterrows():
        n = norm(r.iloc[0])
        if not n or n in ("nan", "complaint types"):
            continue
        c = norm(r.iloc[1]) if pd.notna(r.iloc[1]) and norm(r.iloc[1]) != "nan" else ""
        out[n] = c
    return out

generic = load_vocab(GEN)
nongeneric = load_vocab(NGEN)
nongeneric = {k: v for k, v in nongeneric.items() if k not in generic}

# Chadia's approved additions (Sep 22 feedback)
if os.path.exists(ADDITIONS):
    for k, v in load_vocab(ADDITIONS).items():
        if k not in generic:
            nongeneric[k] = v

# module category fallback for blanks
_v = pd.read_csv(os.path.join(DATA, "vocab_resolution.csv"))
_v = _v[_v["std_name"].notna()]
_module_cat = _v.groupby("std_name")["resolved_category"].apply(
    lambda s: s.mode().iloc[0] if len(s) else "").to_dict()

# Ten codes in her list carry no category in her file and none in the legacy
# fallback, so they used to classify as '' and vanish from every category
# rollup (519 rows). Assigned explicitly, using the fewest categories that
# already exist in her taxonomy — no new category was invented.
CATEGORY_OVERRIDE = {
    "too short": "Size",
    "too tall": "Size",
    "too loose": "Size",
    "strap too long": "Size",
    "difficult to use": "Function",
    "not adjustable": "Function",
    "sticky": "Material",
    "pilling": "Material",
    "velcro damage": "Material",
    "poor insulation": "Quality",
}

cat_map = {}
for k, v in {**generic, **nongeneric}.items():
    cat_map[k] = title(v or CATEGORY_OVERRIDE.get(k) or _module_cat.get(k, ""))

# Labels withheld from the model's menu.
#
# The catch-alls absorb specific cases: on Chadia's blind corrections the model
# emitted "Not As Expected" 33 times where she used it 10. REMOVING the catch-alls
# from the menu moved agreement 75.9% -> 83.2% (+7.3 pts, 26 rows fixed / 10 broken).
#
# 'Not as Advertised/Misleading' IS her approved rename target (Audit Suggestions
# row 3), so withholding it has a real taxonomy cost. Restoring it as a bare menu
# label over-fires (22 emissions where she uses 10: -2.7 pts, rule_ab_nad.py,
# 2026-10-18) because the model treats it as a general disappointment label — and
# because 46/220 reviews literally begin with the customer's selected return reason
# "Not as Expected|...". Restored WITH the discriminating rule below (RULE_NAD +
# NEG_NAD) it was measured on the same 220 rows, same run, twice:
#     82.3% base -> 85.0% (neg2, +4 rows)   and   82.3% base -> 85.5% (+7 rows)
# with 6 emissions (she uses the slot 10x) — no overshoot. Shipped 2026-10-18.
#
# Two adjacent changes were measured in the same runs and REJECTED: a tighter
# positive test ("the review must set shown-vs-arrived") scored 78.6%, and removing
# the 'Vague Complaint' line below (it names a withheld code — the source of the
# 1-2 leaks per run) cost 4-5 rows (83.6% vs 85.5%), so the line stays.
# Any label withheld here must not be reachable via SYNONYMS either, or the
# withheld code re-enters through post-processing (the original arm-D confound).
MENU_EXCLUDE = {"not as expected", "vague complaint",
                "received incorrect color/item/size"}
_common = sorted(title(c) for c in generic if c not in MENU_EXCLUDE)
_specific = sorted(title(c) for c in nongeneric if c not in MENU_EXCLUDE)
# 'Not As Advertised/Misleading' is not a row in her two CSVs: it exists only as
# the rename target her audit sheet approves (Audit Suggestions row 3). Un-hiding
# it from MENU_EXCLUDE is therefore not enough — the menu has to be given it.
if "not as advertised/misleading" not in MENU_EXCLUDE and \
        "Not As Advertised/Misleading" not in _common + _specific:
    _specific = sorted(_specific + ["Not As Advertised/Misleading"])
MENU_LINES = ("Common complaints (apply to all products):\n" + "; ".join(_common) +
              "\n\nProduct-specific complaints:\n" + "; ".join(_specific))

# Her approved rename target is not a CSV row, so it needs its category here or
# classify_v2 would file it as 'NEW' and the row would drop out of every rollup.
if "not as advertised/misleading" not in MENU_EXCLUDE:
    cat_map.setdefault("not as advertised/misleading", "Not as Described")

# The boundary rule for her approved rename target. Measured, not guessed: the label
# over-fires as a general disappointment label (22x vs her 10) and 46/220 reviews
# open with the "Not as Expected|..." return-reason tag, so a bare menu entry just
# string-matches that tag. These lines are what held emissions to 6x while turning
# the picture/description/wrong-item cases into hits (rule_ab_nad5.py).
RULE_NAD = (
    "Using 'Not As Advertised/Misleading':\n"
    "- Use it only when the ITEM ITSELF differs from what was pictured, described or ordered: "
    "the photo showed a different design, colour or pattern; the listing promised a feature the "
    "item does not have; or a different model, size or variant arrived than was ordered.\n"
    "- Never use it for an item that is exactly as described but disappointed the customer: "
    "too firm, too soft, does not fit, too big, too small, not needed, unwanted, cheap/flimsy, "
    "minimal output, or any other specific complaint. Those go to that specific complaint.\n"
    "- Ignore the words 'Not as Expected' and 'Changed Mind' inside the review text. Reviews often "
    "start with the customer's pre-selected return reason, pipe-separated (for example "
    "'Not as Expected|wrong size|no'). That is a dropdown tag, not the complaint: classify the "
    "free-text description that follows it, and never answer because of the tag itself.\n"
    "- If the item is as described and the only problem is that the customer changed their mind, "
    "choose 'Not Needed/unwanted'.\n"
)

NEG_NAD = (
    "- 'Not As Advertised/Misleading' must never absorb these labels: Too Firm, Too Soft, "
    "Doesnt Fit, Too Big, Too Small, Not Needed/unwanted, Cheap/flimsy, Minimal Output, "
    "Arrived Missing Item, or any damage complaint.\n"
    "- Missing parts -> Arrived Missing Item, even when the review calls it the wrong item.\n"
    "- A scratch or stain the product left -> the damage complaint, not a description mismatch.\n"
    "- 'Not the quality advertised', 'not as robust as expected', 'didn't look luxurious' -> "
    "Cheap/flimsy or Doesnt Like Shape/design: the item matched the listing, the customer's taste "
    "or standard did not.\n"
)

SYSTEM = ("You are a customer-support specialist classifying a customer review into a fixed list of complaints.\n"
          "Select the ONE complaint that best matches the MAIN problem described in the review.\n"
          "House conventions (follow these):\n"
          "- If airflow, power or drying fails (no air blows, not powerful enough, does not dry), choose 'Minimal output'. If the heater itself does not heat, choose 'No Heat'.\n"
          "- Use 'Vague Complaint' only when the review describes no identifiable problem at all. Never use it when any specific complaint fits.\n"
          "- If the customer says the product feels cheap, flimsy or poorly made, choose 'Cheap/Flimsy'.\n"
          "- Otherwise choose the most specific complaint that applies.\n"
          "Only propose a NEW complaint when no existing complaint is even roughly applicable.\n"
          'Return JSON only: {"complaint": "<complaint>"}.\n'
          + RULE_NAD + NEG_NAD)

# Chadia's conventions, learned from her own 438 reviewed labels.
# Deterministic remaps (not prose rules) so the model cannot over-apply them.
# Evidence: she NEVER uses the left-hand complaints (0 uses in 389 reviews),
# so any output of them is a certain miss.
SYNONYMS = {
    "too long": "too big",
    "too narrow": "too small",
    "noisey": "noisy",
    "causes pain / discomfort over time": "uncomfortable",
    "weak performance / ineffective": "minimal output",
    # Direction REVERSED 2026-10-18 with the discriminating rule in place: the code is
    # her approved rename target, so an answer of it must survive post-processing as
    # itself. The old mapping (advertised -> not as expected) folded it into a WITHHELD
    # label, which both hid every emission from the count and re-leaked the catch-all.
    # The reverse mapping (expected -> advertised) stays gone: her corrections predate
    # the rename, and score_key() already treats the two strings as one slot.
    "not as advertised": "not as advertised/misleading",
    "doesnt work with door type": "doesnt fit",
    "unsupportive": "too soft",
    "received incorrect color/item/size": "not as expected",
}

# Two labels are the same taxonomy slot as far as scoring goes: her audit sheet
# renames one into the other, and her blind corrections predate the rename. Used
# by the measurement harness so a correct answer is not scored as a miss purely
# because the label string changed.
SAME_SLOT = {
    "not as expected": "not as advertised/misleading",
    "not as advertised": "not as advertised/misleading",
    "not as advertised/misleading": "not as advertised/misleading",
}

def score_key(complaint):
    k = norm(complaint)
    return SAME_SLOT.get(k, k)


# Denied codes -> the code she meant instead. Kept on disk (not in the DB) so the
# classifier can apply it without a database connection; sync_corrections.py owns
# the file. Missing/empty file = nothing denied.
DENIALS_FILE = os.path.join(DATA, "denied_codes.json")


def _load_denials():
    try:
        with open(DENIALS_FILE, encoding="utf-8") as fh:
            return {norm(k): norm(v) for k, v in json.load(fh).items()}
    except (OSError, ValueError):
        return {}


_DENIALS = _load_denials()

def canonical(complaint):
    k = norm(complaint)
    k = SYNONYMS.get(k, k)
    # A code Chadia DENIED is remapped at classification time, so "deny" governs
    # future rows as well as the ones already in the database. sync_corrections.py
    # writes this file when it applies a denial.
    return _DENIALS.get(k, k)

def rule_route_detail(text):
    """Rule-based routing for non-complaint reviews -> Review Deletion.

    Review Deletion is a process label (junk / duplicate review), not a
    text-classifiable complaint, so it is decided by rule.

    'duplicate' is only a junk marker in a SHORT review. A customer writing
    "I received duplicate items" or "ordered one, got a duplicate charge" is
    making a real complaint, and an unconditional substring test deleted it from
    reporting invisibly (317 rows carried the label). Long mentions therefore go
    to the model, which can read the sentence.
    """
    t = (text if isinstance(text, str) else "").strip()
    if len(t) <= 3:
        return "Review Deletion", "empty-text"
    if len(t) < 40 and "duplicate" in t.lower():
        return "Review Deletion", "duplicate-short"
    return None, None


def rule_route(text):
    return rule_route_detail(text)[0]
