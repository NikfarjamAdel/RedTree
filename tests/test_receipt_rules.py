"""ABN checks, sender addresses, date parsing and rule resolution."""
import json
from datetime import date

from receipt_rules import (
    abn_from_text,
    abn_is_valid,
    date_from_text,
    reparse_date,
    resolve,
    sender_address,
)


# --- ABN check digit ----------------------------------------------------
def test_valid_abn():
    assert abn_is_valid("51824753556")          # the ATO's own, public ABN


def test_scanning_misreads_fail_the_check_digit():
    # a 3 read as 8; a 5 read as 6
    assert not abn_is_valid("51824758556")
    assert not abn_is_valid("51824753656")


def test_abn_must_be_eleven_digits():
    assert not abn_is_valid("1234")
    assert not abn_is_valid("5182475355x")


def test_abn_found_in_text_with_spaces():
    assert abn_from_text("TAX INVOICE · ABN 51 824 753 556") == "51824753556"


def test_abn_label_without_number_gives_nothing():
    # the laundromat prints 'ABN' followed by other text
    assert abn_from_text("ABN\nPoint of Sale\nD3") is None


# --- Sender addresses -----------------------------------------------------
def test_sender_address_is_extracted_and_lower_cased():
    assert sender_address("Laundromat <Receipts@Example.com>") == "receipts@example.com"


def test_missing_sender_gives_none():
    assert sender_address(None) is None


# --- Month-first dates ----------------------------------------------------
def test_month_first_date_reparsed():
    assert reparse_date("10/08/2026", "MDY") == date(2026, 10, 8)


def test_impossible_month_gives_none():
    assert reparse_date("13/08/2026", "MDY") is None


def test_only_month_first_rules_reparse():
    assert reparse_date("10/08/2026", "DMY") is None


def test_date_found_in_text_month_first():
    assert date_from_text("Date/Time 10/08/2026 19:40:19", "MDY") == date(2026, 10, 8)


# --- Resolution with a merchant rule --------------------------------------
LAUNDROMAT_RULE = {
    "rule_id": 1, "match_type": "sender", "match_value": "noreply@laundry.example.com",
    "merchant_name": "Laundromat", "category": "linen",
    "scope": "listing", "listing_id": 3, "date_order": "MDY",
}
RULES = {("sender", "noreply@laundry.example.com"): LAUNDROMAT_RULE}


def receipt(**fields):
    base = {"abn": None, "merchant_name": None, "transaction_date": None,
            "transaction_date_text": None, "sender": None,
            "extraction_json": json.dumps({"content": ""})}
    base.update(fields)
    return base


def test_sender_rule_supplies_merchant_and_month_first_date():
    # The real case: no name or ABN printed, date field missing,
    # 10/08/2026 in the text meaning 8 October
    r = receipt(sender="Example Laundromat <noreply@laundry.example.com>",
                extraction_json=json.dumps({"content": "Date/Time 10/08/2026 19:40:19"}))
    resolved = resolve(r, RULES)
    assert resolved["rule"] is LAUNDROMAT_RULE
    assert resolved["merchant_name"] == "Laundromat"
    assert resolved["transaction_date"] == date(2026, 10, 8)


def test_without_a_rule_extracted_values_stand():
    r = receipt(merchant_name="BWS", transaction_date=date(2026, 7, 18),
                sender="me <me@example.com>")
    resolved = resolve(r, RULES)
    assert resolved["rule"] is None
    assert resolved["merchant_name"] == "BWS"
    assert resolved["transaction_date"] == date(2026, 7, 18)


def test_missing_extraction_json_does_not_crash():
    assert resolve(receipt(extraction_json=None), RULES)["abn"] is None