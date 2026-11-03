"""Validating model answers, and comparing them by meaning."""
from datetime import date
from decimal import Decimal

from categorise_common import normalise, valid_value
from categorise_receipts import comparable


# --- Values a model may propose ------------------------------------------
def test_valid_values():
    assert valid_value("transaction_date", "2026-10-08")
    assert valid_value("total", "53.97")
    assert valid_value("merchant_name", "Laundromat")


def test_invalid_values():
    assert not valid_value("transaction_date", "08/10/2026")    # not YYYY-MM-DD
    assert not valid_value("total", "$53.97")                   # currency symbol
    assert not valid_value("total", "53.975")                   # three decimals
    assert not valid_value("total", "-5.00")
    assert not valid_value("merchant_name", "x" * 101)          # too long
    assert not valid_value("total", "")


# --- normalise() ------------------------------------------------------------
def answer(**fields):
    base = {"document_type": "receipt", "category": "supplies", "scope": "listing",
            "reason": "r", "action": "accept", "field": "", "value": "",
            "action_reason": "a"}
    base.update(fields)
    return base


def test_reports_are_never_expenses():
    assert normalise(answer(document_type="report", action="accept"))["action"] == "not_expense"


def test_empty_strings_mean_none():
    clean = normalise(answer())
    assert clean["field"] is None and clean["value"] is None


def test_field_without_set_field_is_dropped():
    clean = normalise(answer(action="accept", field="total", value="1.00"))
    assert clean["field"] is None and clean["value"] is None


def test_set_field_with_a_bad_value_is_unusable():
    clean = normalise(answer(action="set_field", field="total", value="$53.97"))
    assert clean["action"] is None


def test_set_field_with_a_good_value_is_kept():
    clean = normalise(answer(action="set_field", field="transaction_date", value="2026-10-08"))
    assert clean["action"] == "set_field" and clean["value"] == "2026-10-08"


def test_long_reason_is_shortened_to_fit_the_column():
    assert len(normalise(answer(reason="x" * 2000))["reason"]) == 500


# --- comparable(): agreement by meaning -------------------------------------
RECEIPT = {"total": Decimal("12.00")}
RESOLVED = {"transaction_date": date(2026, 10, 8), "merchant_name": "BWS"}


def stored(**fields):
    base = {"document_type": "receipt", "category": "welcome_box", "scope": "listing",
            "action": "accept", "action_field": None, "action_value": None}
    base.update(fields)
    return base


def test_receipt_and_invoice_count_as_the_same():
    # BWS prints TAX INVOICE on its till receipts
    assert (comparable(stored(document_type="receipt"), RECEIPT, RESOLVED)
            == comparable(stored(document_type="invoice"), RECEIPT, RESOLVED))


def test_set_field_to_the_current_value_counts_as_accept():
    same_date = stored(action="set_field", action_field="transaction_date",
                       action_value="2026-10-08")
    assert comparable(same_date, RECEIPT, RESOLVED) == comparable(stored(), RECEIPT, RESOLVED)


def test_set_field_to_a_new_value_is_a_real_difference():
    new_date = stored(action="set_field", action_field="transaction_date",
                      action_value="2026-08-10")
    assert comparable(new_date, RECEIPT, RESOLVED) != comparable(stored(), RECEIPT, RESOLVED)