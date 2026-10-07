#!/usr/bin/env python3
"""Clean Discover, Apple Card, and transaction-download CSVs into one spend table.

Source keys come from the file name, not from a guessed issuer:
discover, apple_card, txn_download.
Payment text in the download (CAPITAL ONE MOBILE PYMT) is issuer evidence only.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pandas as pd

SOURCE_FILES = {
    "discover": "Discover-AllAvailable-20261005.csv",
    "apple_card": "Apple Card Transactions Apr 01 2026 - Oct 05 2026.csv",
    "txn_download": "2026-10-05_transaction_download.csv",
}

COLUMNS = [
    "source",
    "txn_date",
    "posted_date",
    "description",
    "category_raw",
    "category",
    "amount",
    "kind",
    "include_in_spend",
]

TAXONOMY = (
    "dining",
    "groceries",
    "gas",
    "shopping",
    "travel",
    "transportation",
    "services",
    "medical",
    "other",
    "payments",
)

# Issuer category -> taxonomy. Mixed buckets are refined from the description.
CATEGORY_MAP = {
    "restaurants": "dining",
    "dining": "dining",
    "supermarkets": "groceries",
    "grocery": "groceries",
    "warehouse clubs": "groceries",
    "gasoline": "gas",
    "gas": "gas",
    "gas/automotive": "gas",
    "merchandise": "shopping",
    "shopping": "shopping",
    "department stores": "shopping",
    "installment": "shopping",
    "travel/ entertainment": "other",
    "other travel": "travel",
    "airfare": "travel",
    "lodging": "travel",
    "entertainment": "other",
    "services": "services",
    "other services": "services",
    "government services": "services",
    "internet": "services",
    "automotive": "transportation",
    "transportation": "transportation",
    "medical services": "medical",
    "medical": "medical",
    "health care": "medical",
    "fees": "other",
    "other": "other",
    "payments and credits": "payments",
    "awards and rebate credits": "payments",
    "payment/credit": "payments",
    "payment": "payments",
}

TRANSPORT_KEYS = (
    "LIME",
    "ORCA",
    "VENTRA",
    "FASTRAK",
    "ETOLL",
    "CLIPPER",
    "SUICA",
    "MONORAIL",
    "TRANSIT",
    "TRANSPORTATION",
    "PARKING",
    "LADOT",
    "METER",
    "BUS TICKET",
)
TRAVEL_KEYS = (
    "AIRLINE",
    "AIRLINES",
    "AVIS",
    "TOWNEPLACE",
    "MOTEL",
    "HOTEL",
    "EXPEDIA",
    "FRONTIER",
    "DISNEYLAND TICKET",
    "COT*HTL",
    "COT*FLT",
)
REFUND_MERCHANTS = (
    ("UNIQLO", "shopping"),
    ("LEGO", "shopping"),
    ("MITSUWA", "groceries"),
)

_ENDING_IN = re.compile(r"(?:APPLE PAY\s+)?ENDING IN\s+\d{4}", re.IGNORECASE)
_MASKED_PAN = re.compile(r"X{4,}[\dX]*", re.IGNORECASE)
_LONG_DIGITS = re.compile(r"\d{13,}")
_CENTS = Decimal("0.01")


def parse_date(text: str) -> str:
    raw = text.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date: {text!r}")


def parse_money(text: str) -> Decimal:
    raw = text.strip().replace("$", "").replace(",", "")
    if not raw:
        raise ValueError("empty amount")
    negative = False
    if raw.startswith("(") and raw.endswith(")"):
        negative = True
        raw = raw[1:-1]
    value = Decimal(raw)
    if negative:
        value = -value
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


def parse_money_optional(text: str | None) -> Decimal | None:
    if text is None or not str(text).strip():
        return None
    return parse_money(str(text))


def money_float(value: Decimal) -> float:
    return float(format(value.quantize(_CENTS, rounding=ROUND_HALF_UP), "f"))


def sum_amount(series) -> float:
    total = Decimal("0")
    for value in series:
        total += Decimal(f"{value:.2f}")
    return money_float(total)


def clean_description(text: str) -> str:
    """Drop card-ending phrases, masked PANs, and long digit runs. Merchant text stays."""
    cleaned = _ENDING_IN.sub(" ", text or "")
    cleaned = _MASKED_PAN.sub(" ", cleaned)
    cleaned = _LONG_DIGITS.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return cleaned


def map_category(category_raw: str, description: str, unknown: list[str]) -> str:
    key = (category_raw or "").strip().lower()
    if key not in CATEGORY_MAP:
        unknown.append(category_raw)
        base = "other"
    else:
        base = CATEGORY_MAP[key]
    if base == "payments":
        return base
    upper = (description or "").upper()
    if "CAR WASH" in upper:
        return "services"
    if any(token in upper for token in TRANSPORT_KEYS) and base in {
        "other",
        "travel",
        "gas",
        "services",
        "transportation",
    }:
        return "transportation"
    if any(token in upper for token in TRAVEL_KEYS) and base in {"other", "travel", "gas"}:
        return "travel"
    if any(token in upper for token in ("HAIR", "SALON", "SUPERCUTS")) and base == "other":
        return "services"
    return base


def refund_category(description: str, fallback: str) -> str:
    upper = description.upper()
    for token, category in REFUND_MERCHANTS:
        if token in upper:
            return category
    if fallback == "payments":
        return "other"
    return fallback


def _record(
    source: str,
    txn_date: str,
    posted_date: str,
    description: str,
    category_raw: str,
    category: str,
    amount: Decimal,
    kind: str,
    include: bool,
) -> dict:
    if category not in TAXONOMY:
        raise ValueError(f"category {category!r} is outside the taxonomy")
    if include and amount < 0:
        raise ValueError(f"negative amount marked as spend: {description!r} {amount}")
    if include and category == "payments":
        raise ValueError(f"payment category marked as spend: {description!r}")
    if include and kind not in {"purchase", "installment", "fee"}:
        raise ValueError(f"kind {kind} marked as spend: {description!r}")
    return {
        "source": source,
        "txn_date": txn_date,
        "posted_date": posted_date,
        "description": clean_description(description),
        "category_raw": (category_raw or "").strip(),
        "category": category,
        "amount": money_float(amount),
        "kind": kind,
        "include_in_spend": include,
    }


def _read_dicts(path: Path) -> tuple[list[dict[str, str]], int]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ValueError(f"{path.name} has no header") from exc
        rows = []
        blank = 0
        for raw in reader:
            if not any(cell.strip() for cell in raw):
                blank += 1
                continue
            if len(raw) != len(header):
                raise ValueError(f"{path.name} has a row with {len(raw)} fields, expected {len(header)}")
            rows.append(dict(zip(header, raw)))
    return rows, blank


def _load_discover(path: Path, unknown: list[str]) -> list[dict]:
    rows, blank = _read_dicts(path)
    if blank:
        raise ValueError(f"{path.name} has a blank data row")
    records = []
    for row in rows:
        description = row["Description"]
        category_raw = row["Category"]
        amount = parse_money(row["Amount"])
        upper = description.upper()
        category = map_category(category_raw, description, unknown)
        if category_raw == "Fees" or "RETURNED CHECK CHARGE" in upper:
            kind, include, signed = "fee", True, amount
        elif category_raw == "Awards and Rebate Credits" or "CASHBACK BONUS" in upper:
            # Statement credit. Not a payment the cardholder sent, and not a merchant refund.
            kind, include, signed, category = "other", False, amount, "payments"
        elif "RETURNED INTERNET PMT" in upper or "INTERNET PAYMENT" in upper:
            # Returned payments are positive in the Discover file. They undo a payment.
            kind, include, signed, category = "payment", False, amount, "payments"
        elif category_raw == "Payments and Credits" and amount < 0:
            kind = "refund"
            include = False
            signed = amount
            category = refund_category(description, category)
        elif category_raw == "Payments and Credits":
            kind, include, signed, category = "other", False, amount, "payments"
        elif amount < 0:
            kind = "refund"
            include = False
            signed = amount
            category = refund_category(description, category)
        else:
            kind, include, signed = "purchase", True, amount
        records.append(
            _record(
                "discover",
                parse_date(row["Trans. Date"]),
                parse_date(row["Post Date"]),
                description,
                category_raw,
                category,
                signed,
                kind,
                include,
            )
        )
    return records


def _load_apple(path: Path, unknown: list[str]) -> tuple[list[dict], dict]:
    rows, blank = _read_dicts(path)
    if blank:
        raise ValueError(f"{path.name} has a blank data row")
    records = []
    type_counts: dict[str, int] = {}
    for row in rows:
        description = row["Description"]
        category_raw = row["Category"]
        txn_type = row["Type"].strip()
        type_counts[txn_type] = type_counts.get(txn_type, 0) + 1
        amount = parse_money(row["Amount (USD)"])
        upper = description.upper()
        category = map_category(category_raw, description, unknown)
        if txn_type == "Payment" or category_raw == "Payment":
            kind, include, category = "payment", False, "payments"
        elif txn_type == "Installment":
            kind, include = "installment", True
            if category == "payments":
                category = "shopping"
        elif amount < 0 or "CREDIT ADJUSTMENT" in upper:
            kind, include = "refund", False
            # "TRANSIT" in the offer name is not a transit purchase.
            if "CREDIT ADJUSTMENT" in upper or category == "payments":
                category = "other"
        else:
            # Type Other includes the Apple Store charge. It is a purchase, not a payment.
            kind, include = "purchase", True
        records.append(
            _record(
                "apple_card",
                parse_date(row["Transaction Date"]),
                parse_date(row["Clearing Date"]),
                description,
                category_raw,
                category,
                amount,
                kind,
                include,
            )
        )
    installment_amounts = [
        Decimal(f"{row['amount']:.2f}") for row in records if row["kind"] == "installment"
    ]
    installment_sum = sum(installment_amounts, Decimal("0"))
    purchase_amounts = {
        Decimal(f"{row['amount']:.2f}")
        for row in records
        if row["kind"] == "purchase"
    }
    original_present = installment_sum in purchase_amounts and installment_sum != 0
    evidence = {
        "type_counts": type_counts,
        "installment_count": len(installment_amounts),
        "installment_total": money_float(installment_sum),
        "installment_amounts": sorted({money_float(value) for value in installment_amounts}),
        "original_purchase_present": original_present,
        "apple_store_purchases": sum(
            1
            for row in records
            if row["kind"] == "purchase" and "APPLE ONLINE STORE" in row["description"].upper()
        ),
    }
    return records, evidence


def _load_txn(path: Path, unknown: list[str]) -> tuple[list[dict], int]:
    rows, blank = _read_dicts(path)
    records = []
    for row in rows:
        description = row["Description"]
        category_raw = row["Category"]
        debit = parse_money_optional(row["Debit"])
        credit = parse_money_optional(row["Credit"])
        if debit is not None and credit is not None:
            raise ValueError(f"row has both debit and credit: {description!r}")
        if debit is None and credit is None:
            raise ValueError(f"row has neither debit nor credit: {description!r}")
        category = map_category(category_raw, description, unknown)
        upper = description.upper()
        payment_text = "PYMT" in upper or "PAYMENT" in upper
        if credit is not None and (category_raw == "Payment/Credit" or payment_text):
            kind, include, amount, category = "payment", False, -credit, "payments"
        elif credit is not None:
            kind, include, amount = "refund", False, -credit
            category = refund_category(description, category)
        else:
            assert debit is not None
            if category == "payments":
                raise ValueError(f"debit row in a payment category: {description!r}")
            kind, include, amount = "purchase", True, debit
        records.append(
            _record(
                "txn_download",
                parse_date(row["Transaction Date"]),
                parse_date(row["Posted Date"]),
                description,
                category_raw,
                category,
                amount,
                kind,
                include,
            )
        )
    return records, blank


def load_and_clean(data_dir) -> pd.DataFrame:
    """Load the three source CSVs and return one cleaned transaction table."""
    data_dir = Path(data_dir)
    unknown: list[str] = []
    frames = []
    raw_counts = {}
    blank_skipped = {}
    apple_evidence = {}
    for source, name in SOURCE_FILES.items():
        path = data_dir / name
        if not path.is_file():
            raise FileNotFoundError(path)
        if source == "discover":
            records = _load_discover(path, unknown)
            blank_skipped[source] = 0
        elif source == "apple_card":
            records, apple_evidence = _load_apple(path, unknown)
            blank_skipped[source] = 0
        else:
            records, blank = _load_txn(path, unknown)
            blank_skipped[source] = blank
        raw_counts[source] = len(records)
        frames.extend(records)
    df = pd.DataFrame(frames, columns=COLUMNS)
    if len(df) != sum(raw_counts.values()):
        raise RuntimeError("cleaned row count does not match parsed source rows")
    df = df.sort_values(
        ["txn_date", "source", "description", "amount"], kind="mergesort"
    ).reset_index(drop=True)
    df.attrs["row_counts_raw"] = raw_counts
    df.attrs["blank_lines_skipped"] = blank_skipped
    df.attrs["unknown_categories"] = sorted(set(unknown))
    df.attrs["unknown_category_count"] = len(unknown)
    df.attrs["apple_evidence"] = apple_evidence
    return df


def _kind_total(df: pd.DataFrame, kind: str, absolute: bool = False) -> tuple[int, float]:
    subset = df[df["kind"] == kind]
    if subset.empty:
        return 0, 0.0
    total = Decimal("0")
    for value in subset["amount"]:
        amount = Decimal(f"{value:.2f}")
        total += abs(amount) if absolute else amount
    return int(len(subset)), money_float(total)


def summarize(df: pd.DataFrame) -> dict:
    spend = df[df["include_in_spend"]]
    if (spend["amount"] < 0).any():
        raise RuntimeError("spend includes a negative amount")
    if (spend["category"] == "payments").any():
        raise RuntimeError("spend includes a payment category")

    raw_counts = dict(df.attrs.get("row_counts_raw") or {})
    clean_counts = {source: int((df["source"] == source).sum()) for source in SOURCE_FILES}
    if not raw_counts:
        raw_counts = dict(clean_counts)
    raw_counts["total"] = int(sum(raw_counts[source] for source in SOURCE_FILES))
    clean_counts["total"] = int(len(df))
    if raw_counts != clean_counts:
        raise RuntimeError(f"row count mismatch: raw {raw_counts} clean {clean_counts}")

    payment_count, payment_net = _kind_total(df, "payment", absolute=False)
    refund_count, refund_signed = _kind_total(df, "refund", absolute=False)
    cashback = df[(df["kind"] == "other") & (df["category"] == "payments")]
    fee_count, fee_total = _kind_total(df, "fee", absolute=False)
    installment_count, installment_total = _kind_total(df, "installment", absolute=False)

    by_year = (
        spend.groupby(spend["txn_date"].str.slice(0, 4))["amount"].apply(sum_amount).sort_index()
    )
    by_month = (
        spend.groupby(spend["txn_date"].str.slice(0, 7))["amount"].apply(sum_amount).sort_index()
    )
    by_category = (
        spend.groupby("category")["amount"].apply(sum_amount).sort_values(ascending=False)
    )
    by_source = spend.groupby("source")["amount"].apply(sum_amount)
    top_months = by_month.sort_values(ascending=False).head(5)

    apple = df.attrs.get("apple_evidence") or {}
    travel_mix = (
        df[df["category_raw"] == "Travel/ Entertainment"]
        .groupby("category")
        .size()
        .sort_index()
    )
    reversals = df[
        (df["kind"] == "payment")
        & (df["amount"] > 0)
        & (df["source"] == "discover")
    ]
    notes = [
        "Source is the file, not an inferred issuer: discover, apple_card, txn_download. "
        "txn_download payment text such as CAPITAL ONE MOBILE PYMT is issuer evidence only.",
        "Spend is the sum of amount where include_in_spend is true. "
        "Refunds are excluded from that sum and from spend_by_category, not netted in. "
        "gross_spend is that same sum.",
        "Discover payments match INTERNET PAYMENT, REPRESENTED INTERNET PAYMENT, "
        "and RETURNED INTERNET PMT. Other negative Payments and Credits rows are merchant "
        f"refunds (UNIQLO, LEGO, MITSUWA). Refund count is {refund_count}.",
        "Discover RETURNED INTERNET PMT rows are payment reversals. They stay in the table "
        f"as kind=payment with a positive amount ({len(reversals)} rows, "
        f"{sum_amount(reversals['amount']) if len(reversals) else 0:.2f}). "
        "payment_total_absolute is the absolute value of net payment amounts, so reversals offset.",
        f"Cashback bonus rows (Awards and Rebate Credits) are statement credits, not payments "
        f"and not merchant refunds: {len(cashback)} rows, {sum_amount(cashback['amount']) if len(cashback) else 0:.2f} signed. "
        "They are kind=other, category=payments, include_in_spend false.",
        f"Fees (returned check charges) are real charges and are included in spend: "
        f"{fee_count} rows, {fee_total:.2f}, category=other, kind=fee.",
        "Apple Card installments are included in spend. "
        f"This export has {apple.get('installment_count', installment_count)} installment rows "
        f"totaling {apple.get('installment_total', installment_total):.2f} "
        f"(amounts {apple.get('installment_amounts', [])}). "
        "No purchase row equals that installment total, so the original financed purchase is not "
        "in this file and the monthly charges are not double-counted. "
        f"APPLE ONLINE STORE purchase rows kept separately: {apple.get('apple_store_purchases', 0)}.",
        "Apple Type Other with a positive amount is a purchase. "
        "The negative Mastercard transit credit adjustment is a refund so it does not inflate spend.",
        "txn_download credits in Payment/Credit are payments. Credits in any other category are refunds. "
        "Debits are purchases. Empty debit or credit cells are ignored; a row must have one of the two.",
        "Categories are normalized to dining, groceries, gas, shopping, travel, transportation, "
        "services, medical, other, and payments. Discover 'Travel/ Entertainment' is mixed: "
        + ", ".join(f"{category} {int(count)}" for category, count in travel_mix.items())
        + ". Gas/Automotive parking and transit descriptions are transportation. "
        f"Unfamiliar raw categories mapped to other: {df.attrs.get('unknown_category_count', 0)} "
        f"({df.attrs.get('unknown_categories', [])}).",
        "Warehouse clubs (Sam's Club) map to groceries. Government services map to services. "
        "Entertainment has no taxonomy bucket, so movies and similar charges map to other.",
        "Descriptions drop 'ENDING IN ####', Apple Pay card suffixes, masked card tokens, "
        "and digit runs of 13 or more. The card-number column is not written.",
        "The transaction download has a trailing blank line. "
        f"Blank lines skipped: {df.attrs.get('blank_lines_skipped', {})}. "
        "That line is not a transaction and is not in row_counts_raw. No parsed transaction was dropped.",
        f"Apple Type counts: {apple.get('type_counts', {})}.",
    ]
    return {
        "row_counts_raw": raw_counts,
        "row_counts_clean": clean_counts,
        "excluded_payment_count": payment_count,
        "payment_total_absolute": abs(payment_net),
        "refund_count": refund_count,
        "refund_total": abs(refund_signed),
        "gross_spend": sum_amount(spend["amount"]) if len(spend) else 0.0,
        "date_min": str(df["txn_date"].min()) if len(df) else None,
        "date_max": str(df["txn_date"].max()) if len(df) else None,
        "spend_by_year": {str(key): float(value) for key, value in by_year.items()},
        "spend_by_month": {str(key): float(value) for key, value in by_month.items()},
        "most_expensive_months": [
            {"month": str(month), "amount": float(amount)} for month, amount in top_months.items()
        ],
        "spend_by_category": {str(key): float(value) for key, value in by_category.items()},
        "spend_by_source": {
            source: float(by_source[source]) if source in by_source.index else 0.0
            for source in SOURCE_FILES
        },
        "spend_by_year_month": {str(key): float(value) for key, value in by_month.items()},
        "notes": notes,
    }


def write_outputs(df: pd.DataFrame, out_dir) -> dict:
    """Write cleaned_transactions.csv and summary.json. Return the summary dict."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize(df)
    frame = df.loc[:, COLUMNS].copy()
    frame["include_in_spend"] = frame["include_in_spend"].map(lambda value: "true" if value else "false")
    frame["amount"] = frame["amount"].map(lambda value: f"{value:.2f}")
    csv_path = out_dir / "cleaned_transactions.csv"
    frame.to_csv(csv_path, index=False)
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    _reject_card_numbers(csv_path.read_text(encoding="utf-8"))
    return summary


def _reject_card_numbers(text: str) -> None:
    if re.search(r"ENDING IN\s+\d{4}", text, re.IGNORECASE):
        raise RuntimeError("cleaned output still contains an ENDING IN card suffix")
    if re.search(r"X{4,}\d", text, re.IGNORECASE):
        raise RuntimeError("cleaned output still contains a masked card number")


def print_summary(summary: dict) -> None:
    raw = summary["row_counts_raw"]
    print(
        f"rows {raw['total']} "
        f"(discover {raw['discover']}, apple_card {raw['apple_card']}, "
        f"txn_download {raw['txn_download']})"
    )
    print(f"dates {summary['date_min']} .. {summary['date_max']}")
    print(f"spend {summary['gross_spend']:.2f}")
    print(
        f"payments excluded {summary['excluded_payment_count']} "
        f"(net absolute {summary['payment_total_absolute']:.2f})"
    )
    print(
        f"refunds excluded {summary['refund_count']} "
        f"({summary['refund_total']:.2f})"
    )
    print("spend by year: " + ", ".join(
        f"{year} {amount:.2f}" for year, amount in summary["spend_by_year"].items()
    ))
    print("top months:")
    for row in summary["most_expensive_months"]:
        print(f"  {row['month']} {row['amount']:.2f}")
    print("spend by category: " + ", ".join(
        f"{category} {amount:.2f}" for category, amount in summary["spend_by_category"].items()
    ))
    print("spend by source: " + ", ".join(
        f"{source} {amount:.2f}" for source, amount in summary["spend_by_source"].items()
    ))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Clean purchase CSVs and summarize spend.")
    parser.add_argument("--data-dir", type=Path, default=Path("/Users/alex/Code/Purchases/statements"))
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    out_dir = args.out_dir if args.out_dir is not None else Path("/Users/alex/Code/Purchases/data")
    df = load_and_clean(args.data_dir)
    summary = write_outputs(df, out_dir)
    print_summary(summary)


if __name__ == "__main__":
    main()
