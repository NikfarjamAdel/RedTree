"""Receipt logic shared by validation, categorisation and the expense insert.

Nothing here is stored: every result can be recomputed at no cost from
RECEIPT, EMAIL_RAW and MERCHANT_RULE, so all steps use these functions
rather than saving their own copies.
"""
import json
import re
from datetime import datetime
from email.utils import parseaddr

ABN_WEIGHTS = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)

# 'ABN' then 11 digits, optionally spaced (51 824 753 556 or 51824753556).
# The laundromat prints 'ABN' with no number after it, so it won't match.
ABN_PATTERN = re.compile(r"ABN[:\s]*((?:\d\s?){11})", re.IGNORECASE)

# Formats tried when a merchant rule says dates are month first.
MDY_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y")


def abn_is_valid(abn):
    """True if abn is 11 digits with a valid check digit."""
    if len(abn) != 11 or not abn.isdigit():
        return False
    # ATO check: take 1 from the first digit, weight each digit, and the
    # sum must divide by 89.
    digits = [int(c) for c in abn]
    digits[0] -= 1
    return sum(d * w for d, w in zip(digits, ABN_WEIGHTS)) % 89 == 0


def abn_from_text(text):
    """First ABN in the text, digits only, or None."""
    m = ABN_PATTERN.search(text or "")
    return re.sub(r"\D", "", m.group(1)) if m else None


def sender_address(sender):
    """'Laundromat <Receipts@Example.com>' -> 'receipts@example.com'."""
    _, address = parseaddr(sender or "")
    return address.lower() or None


def load_rules(cur):
    """All merchant rules, keyed by (match_type, match_value)."""
    cur.execute(
        "SELECT rule_id, match_type, match_value, merchant_name, category, "
        "scope, listing_id, date_order FROM MERCHANT_RULE"
    )
    columns = [c[0] for c in cur.description]
    rules = {}
    for row in cur.fetchall():
        rule = dict(zip(columns, row))
        rules[(rule["match_type"], rule["match_value"])] = rule
    return rules


def reparse_date(text, date_order):
    """The printed date reparsed in the rule's order, or None."""
    if not text or date_order != "MDY":
        return None
    for fmt in MDY_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            pass
    return None


# A date like 10/08/2026 or 10/8/26, for senders whose date field is missing.
DATE_IN_TEXT = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b")


def date_from_text(text, date_order):
    """The first date in the text, parsed in the rule's order, or None."""
    m = DATE_IN_TEXT.search(text or "")
    return reparse_date(m.group(1), date_order) if m else None


def resolve(receipt, rules):
    """The values later steps use, after applying any matching rule.

    receipt: a dict with the view's columns plus 'sender' and
    'extraction_json'. Returns a dict with:
        abn, abn_valid, rule, merchant_name, transaction_date
    """
    # The full text comes from the JSON, not the view, because JSON_VALUE
    # returns NULL for anything over 4,000 characters.
    raw = receipt["extraction_json"]
    text = json.loads(raw).get("content", "") if raw else ""

    # 1. ABN: the invoice field if the view has one, otherwise the full text.
    abn = receipt["abn"] or abn_from_text(text)
    abn_valid = abn_is_valid(abn) if abn else None

    # 2. Rule: a valid ABN first, then the sender address.
    rule = rules.get(("abn", abn)) if abn_valid else None
    if rule is None:
        rule = rules.get(("sender", sender_address(receipt["sender"])))

    # 3. Merchant: the rule's clean name if there is one, else the extracted name.
    merchant_name = rule["merchant_name"] if rule else receipt["merchant_name"]

    # 4. Date: when the rule gives a date order, reparse the printed date that
    #    way; if the date field is missing, find the date in the text instead.
    transaction_date = receipt["transaction_date"]
    if rule and rule["date_order"]:
        reparsed = (reparse_date(receipt["transaction_date_text"], rule["date_order"])
                    or date_from_text(text, rule["date_order"]))
        if reparsed:
            transaction_date = reparsed

    return dict(abn=abn, abn_valid=abn_valid, rule=rule,
                merchant_name=merchant_name, transaction_date=transaction_date)