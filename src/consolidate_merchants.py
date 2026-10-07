#!/usr/bin/env python3
"""Consolidate purchase descriptions into unified merchants.

Same merchant, different location formatting (store numbers, city/state
suffixes, street addresses, truncated display names) collapses to one
merchant_key. Reads data/cleaned_transactions.csv (or rebuilds it from the
raw CSVs in statements/) and writes a unified transactions CSV plus a
merchant summary.

Usage (from the project root):
    python src/consolidate_merchants.py
    python src/consolidate_merchants.py --input data/cleaned_transactions.csv \\
        --output data/unified_transactions.csv --summary-out data/merchant_summary.csv
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}

COUNTRIES = {"USA", "US", "PHL", "JPN", "GBR"}

CITY_WORDS = {
    "IRVINE", "REDMOND", "BELLEVUE", "SEATTLE", "SPOKANE",
    "LONG", "BEACH", "COSTA", "MESA", "LOS", "ANGELES", "SANTA", "ANA",
    "SAN", "DIEGO", "FRANCISCO", "JOSE", "TUSTIN", "GARDEN", "GROVE",
    "WESTMINSTER", "ARTESIA", "CERRITOS", "DOWNEY", "NORWALK",
    "BELLFLOWER", "ANAHEIM", "ORANGE", "FULLERTON", "HUNTINGTON",
    "FOUNTAIN", "VALLEY", "NEWPORT", "ALISO", "VIEJO", "LAGUNA",
    "NIGUEL", "MISSION", "LAKE", "FOREST", "CARSON", "TORRANCE",
    "PASADENA", "BURBANK", "GLENDALE", "CULVER", "CITY", "HOLLYWOOD",
    "CHICAGO", "OAKLAND", "SEDRO", "WOOLLEY", "SEATAC",
    "NEW", "YORK", "BOSTON", "AUSTIN", "HOUSTON",
    "TOKYO", "HARAJYUKU", "SHIBUYA", "SHINJUKU", "GINZA", "OSAKA",
    "KYOTO", "MANILA", "MAKATI", "PASAY", "LONDON", "LEWES",
}

STREET_TOKENS = {
    "AVE", "AVENUE", "ST", "STREET", "BLVD", "BOULEVARD", "RD", "ROAD",
    "DR", "DRIVE", "WAY", "HWY", "HIGHWAY", "LN", "LANE", "PL", "PLAZA",
    "CT", "COURT", "CIR", "TER", "PKWY", "PARKWAY", "STE", "SUITE",
    "FLR", "FLOOR", "BLDG", "BUILDING", "UNIT", "RM", "LEVEL", "BLK",
}

# (merchant_key, fragments matched as whole words / prefixes)
CHAINS = [
    ("trader joe s", ("TRADER JOE",)),
    ("heytea", ("HEYTEA",)),
    ("sunright tea studio", ("SUNRIGHT",)),
    ("boiling point", ("BOILING POINT",)),
    ("85c bakery cafe", ("85C BAKERY", "85C",)),
    ("ms cm foodhall", ("FOODHALL",)),
    ("7 leaves cafe", ("7 LEAVES",)),
    ("sharetea", ("SHARETEA",)),
    ("3cat", ("3CAT",)),
    ("starbucks", ("STARBUCKS",)),
    ("shell", ("SHELL",)),
    ("in-n-out", ("IN-N-OUT", "IN N OUT")),
    ("clipper", ("CLIPPER",)),
    ("boba guys", ("BOBA GUYS",)),
    ("omomo tea shoppe", ("OMOMO",)),
    ("cha for tea", ("CHA FOR TEA",)),
    ("taco bell", ("TACO BELL",)),
    ("albertsons", ("ALBERTSONS",)),
    ("target", ("TARGET",)),
    ("sams fuel", ("SAMS FUEL",)),
    ("lawson", ("LAWSON",)),
    ("family mart", ("FAMILY MART",)),
    ("seaside donuts", ("SEASIDE DONUTS",)),
    ("dutch bros", ("DUTCH BROS",)),
    ("damo", ("DAMO",)),
    ("cava", ("CAVA",)),
    ("mcdonalds", ("MCDONALD'S", "MCDONALDS", "MCDO")),
    ("regal", ("REGAL",)),
    ("7-eleven", ("7-ELEVEN", "7 ELEVEN")),
    ("h mart", ("H MART", "HMART")),
    ("motel 6", ("MOTEL 6",)),
    ("k-top hair salon", ("K-TOP",)),
    ("uniqlo", ("UNIQLO",)),
    ("frontier", ("FRONTIER",)),
]

PROCESSOR_RE = re.compile(
    r"^(SQ\s*\*|TST\*|PY\s*\*|PYL\*|SNACK\*|SP\s+|G\*\s*|OTTER\s*\*\s*"
    r"|CTLP\s*\*\s*|RPS\s*\*|PAR\s*\*|FSP\s*\*|PP\s*\*|CKE\s*\*|IC\s*\*|UEP\s*\*)\s*"
)
# Same markers mid-string, e.g. TXPYL* in
# "AMERICAN CAMPUS COMM 866-729-5327 TXPYL*AMERICAN CAMPUS COMM".
MID_PROCESSOR_RE = re.compile(
    r"(SQ\s*\*|TST\*|PY\s*\*|PYL\*|SNACK\*|G\*\s*|OTTER\s*\*\s*"
    r"|CTLP\s*\*\s*|RPS\s*\*|PAR\s*\*|FSP\s*\*|PP\s*\*|CKE\s*\*|IC\s*\*|UEP\s*\*)"
)

LONG_DIGITS_RE = re.compile(r"\d{4,}")
STORE_NO_RE = re.compile(r"#\s*\d+")
PHONE_RE = re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b")


def _is_city_token(token: str) -> bool:
    if token in CITY_WORDS:
        return True
    if len(token) >= 3:
        return any(w.startswith(token) for w in CITY_WORDS)
    return False


def _split_glued(token: str) -> list[str]:
    """Split tokens like BEACHCA, FRANCISCO94104, COSTAMESA, SHELL10007989006."""
    if token in CITY_WORDS or token in STATES or token in COUNTRIES:
        return [token]
    m = re.match(r"^([A-Z]{3,})(\d{2,})$", token)
    if m:
        return [m.group(1), m.group(2)]
    for st in STATES:
        if token.endswith(st) and len(token) > len(st) + 2:
            prefix = token[: -len(st)]
            if prefix in CITY_WORDS or _is_city_token(prefix):
                return [prefix, st]
    for city in CITY_WORDS:
        if len(city) >= 4 and token.startswith(city) and len(token) > len(city):
            rest = token[len(city):]
            if rest in CITY_WORDS or rest in STATES or _is_city_token(rest):
                return [city, rest]
    return [token]


def _chain_match(text: str) -> str | None:
    for key, frags in CHAINS:
        for frag in frags:
            if text == frag or text.startswith(frag + " ") or text.startswith(frag + "-"):
                return key
            if re.search(r"(?:^|\W)" + re.escape(frag) + r"(?:\W|$)", text):
                return key
    return None


def normalize_merchant(description: str) -> tuple[str, str]:
    """Return (merchant_key, merchant_display) for one raw description."""
    text = re.sub(r"\s+", " ", (description or "").upper()).strip()
    if not text:
        return ("unknown", "UNKNOWN")
    text = PROCESSOR_RE.sub("", text)

    chained = _chain_match(text)
    if chained:
        return (chained, chained.upper())

    # Split letter/digit glue so store numbers and zips become own tokens.
    text = re.sub(r"([A-Z])(\d{4,})", r"\1 \2", text)
    text = re.sub(r"(\d)([A-Z]{2,})", r"\1 \2", text)
    text = PHONE_RE.sub(" ", text)
    text = STORE_NO_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()

    chained = _chain_match(text)
    if chained:
        return (chained, chained.upper())

    tokens: list[str] = []
    for raw in text.split(" "):
        if raw.strip() == "-":
            tokens.append("-")
            continue
        for part in _split_glued(raw.strip("-.,")):
            if part:
                tokens.append(part)

    def _is_number_noise(tok: str, idx: int) -> bool:
        if idx == 0 or not re.search(r"\d", tok):
            return False
        if tok.isdigit():
            return int(tok) >= 100 or idx >= 2
        return idx >= 1

    # Cut at the address / confirmation number: first noisy number token.
    # Keeps an alpha prefix like SHELL in SHELL10007989006 or GOAT in
    # GOAT454#80772, but drops fragments like RXV6 or 01313R.
    # Index 0 is never cut, so 7 LEAVES, 85C, and MOTEL 6 survive.
    for i, tok in enumerate(tokens):
        if _is_number_noise(tok, i):
            prefix = re.split(r"\d", tok, maxsplit=1)[0].strip("-")
            tokens = tokens[:i] + ([prefix] if len(prefix) >= 4 else [])
            break

    # Drop leading file/store ids, keep names like 7 LEAVES / 85C.
    while tokens and tokens[0].isdigit() and len(tokens[0]) >= 4:
        tokens.pop(0)

    # Cut at street suffixes (DR MARTENS at index 0 survives the i>=2 guard
    # via the address-number cut above, so this only trims real addresses).
    for i, tok in enumerate(tokens):
        if i >= 2 and tok in STREET_TOKENS:
            tokens = tokens[:i]
            break

    # Branch split: "BOILING POINT- IRVINE" -> "BOILING POINT".
    # Only on hyphens with adjacent whitespace, so IN-N-OUT survives.
    joined = " ".join(tokens)
    parts = re.split(r"\s+-\s*|\s*-\s+", joined)
    if len(parts) > 1 and len(parts[0]) >= 3:
        joined = parts[0]
        tokens = joined.split(" ")

    # Hyphen-glued branch: "FRESH-IRVINE" -> "FRESH". Exact city word
    # only, so RENT-A-CAR survives (CAR is merely a prefix of CARSON).
    if len(tokens) >= 2:
        for j, tok in enumerate(tokens):
            if "-" in tok[1:] and tok != "-":
                head, _, tail = tok.rpartition("-")
                if len(head) >= 2 and tail in CITY_WORDS:
                    tokens[j] = head

    # Glued processor repeat: "TXPYL*AMERICAN" inside
    # "AMERICAN CAMPUS COMM 866-729-5327 TXPYL*AMERICAN CAMPUS COMM".
    for i, tok in enumerate(tokens):
        if i == 0:
            continue
        m = MID_PROCESSOR_RE.search(tok)
        if m and m.start() >= 2:
            tokens = tokens[:i]
            break

    # Strip trailing location tokens: country, state, toast AA suffix,
    # zip/phone leftovers, stall numbers, city names (truncation tolerant).
    while tokens:
        last = tokens[-1]
        if last in COUNTRIES or last in STATES or last == "AA":
            tokens.pop()
            continue
        if last.isdigit():
            tokens.pop()
            continue
        if _is_city_token(last):
            tokens.pop()
            continue
        break
    while len(tokens) >= 2 and tokens[-1] == tokens[-2]:
        tokens.pop()

    core = " ".join(tokens).strip(" -")
    core = re.sub(r"\s+", " ", core.replace("/", " ")).strip(" -")
    m = MID_PROCESSOR_RE.search(core)
    if m and m.start() > 0:
        core = core[: m.start()].strip(" -")
    if not core:
        core = re.sub(r"\s+", " ", text).strip(" -") or "UNKNOWN"
    key = re.sub(r"[^a-z0-9\-\* ]", "", core.lower())
    key = re.sub(r"\s+", " ", key).strip() or "unknown"
    return (key, core)


def consolidate(df: pd.DataFrame) -> pd.DataFrame:
    keys, displays = zip(*(normalize_merchant(d) for d in df["description"]))
    out = df.copy()
    out["merchant_key"] = list(keys)
    out["merchant_display"] = list(displays)
    return out


def merchant_summary(unified: pd.DataFrame) -> pd.DataFrame:
    summary = (
        unified.groupby(["merchant_key", "merchant_display"])
        .agg(
            transactions=("amount", "size"),
            spend_total=("amount", "sum"),
            descriptions=("description", "nunique"),
            first_seen=("txn_date", "min"),
            last_seen=("txn_date", "max"),
        )
        .reset_index()
        .sort_values("spend_total", ascending=False)
    )
    summary["spend_total"] = summary["spend_total"].round(2)
    return summary


def load_input(path: Path) -> pd.DataFrame:
    if path.exists():
        return pd.read_csv(path)
    from analyze_purchases import load_and_clean

    return load_and_clean(Path("statements"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="data/cleaned_transactions.csv")
    parser.add_argument("--output", default="data/unified_transactions.csv")
    parser.add_argument("--summary-out", default="data/merchant_summary.csv")
    args = parser.parse_args()

    input_path = Path(args.input)
    df = load_input(input_path)
    unified = consolidate(df)
    unified.to_csv(args.output, index=False)

    summary = merchant_summary(unified)
    if args.summary_out:
        summary.to_csv(args.summary_out, index=False)

    n_desc = int(df["description"].nunique())
    n_merch = int(unified["merchant_key"].nunique())
    print(f"{len(unified)} rows, {n_desc} descriptions -> {n_merch} merchants")
    print(f"wrote {args.output} and {args.summary_out}")
    collapsed = summary[summary["descriptions"] > 1].head(15)
    for row in collapsed.itertuples():
        print(f"{row.merchant_key}: {row.descriptions} variants, "
              f"{row.transactions} txns, {row.spend_total:.2f}")


if __name__ == "__main__":
    main()
