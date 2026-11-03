import hashlib
import json
import os
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import unquote, urlparse

from azure.storage.blob import BlobServiceClient

MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5")
PROMPT_VERSION = "v7"
TEXT_LIMIT = 3000          # characters of the receipt's text sent to the model

DOCUMENT_TYPES = ["receipt", "invoice", "payment_confirmation", "report", "other"]
ACTIONS = ["not_expense", "set_field", "accept", "needs_human"]
FIELDS = ["transaction_date", "total", "merchant_name"]

# Document types that are never an expense. Derived in code from
# document_type, whatever action the model chose.
NOT_EXPENSE_TYPES = {"payment_confirmation", "report", "other"}

# Column sizes in RECEIPT_CATEGORISATION
TEXT_LIMITS = {"reason": 500, "action_reason": 1000}
VALUE_LIMIT = 100

SYSTEM_TEMPLATE = """You categorise expenses for a small short-term rental business.

{property_description}

Every document here was sent by the owner because it is a business expense
for the property. Don't question whether a purchase is personal.

For the document you are given, decide:

1. document_type: what the document is.
   - receipt: proof of a purchase from a shop or machine
   - invoice: a bill from a supplier
   - payment_confirmation: a bank or payment-app record of money sent;
     the real expense is the bill that was paid, not this record
   - report: a service or inspection report, information sheet or quote,
     with no amount owed
   - other: anything else, such as a logo or an unrelated image

2. category: exactly one of these codes:
{categories}
   If a document mixes categories, choose the category of the items that
   make up most of the total.

3. scope:
   - listing: for one listing only (guest supplies, linen, welcome items,
     furnishings for the listing)
   - property: for the whole property (shared utilities, rates, insurance,
     building-wide repairs)
   Rules specific to this property: {categorisation_notes}

4. reason: one sentence explaining the category and scope, quoting the items
   or wording that decided it.

5. action: what review should do with this document. Your answer is applied
   directly in review, so choose exactly one; never advise contacting anyone.
   - not_expense: the document is a report, a payment confirmation, or not a
     receipt or invoice at all
   - set_field: a key field (transaction_date, total or merchant_name) is
     missing or wrong in the extracted fields, and the document text clearly
     shows the right value; give the field and the value
     Give value in this format: transaction_date as YYYY-MM-DD; total as a
     plain number with two decimals and no currency symbol (53.97);
     merchant_name as printed, at most 100 characters.
   - accept: the open issues don't affect the expense (for example the total
     is clearly right even though another figure on the document is wrong)
   - needs_human: the document doesn't let you decide
   If there are no open issues and the document is a receipt or invoice,
   choose accept. Unless the action is set_field, field and value are
   empty strings ("").

6. action_reason: one sentence justifying the action, quoting the document.

If the document is not a receipt or invoice, still choose the closest
category, and say in the reason why it is probably not an expense.
"""


def build_system(categories, prop):
    """The system prompt for one property.
    prop: (description, categorisation_notes), from load_property()."""
    lines = "\n".join(f"   - {code}: {description}" for code, description in categories)
    description, notes = prop
    return SYSTEM_TEMPLATE.format(categories=lines,
                                  property_description=description or "",
                                  categorisation_notes=notes or "none")


def load_property(cur):
    """(description, categorisation_notes) of the property whose listing is
    the receipts default. Per-receipt properties come with multi-property
    routing."""
    cur.execute("SELECT p.description, p.categorisation_notes FROM PROPERTY p "
                "JOIN LISTING l ON l.property_id = p.property_id "
                "WHERE l.default_for_receipts = 1")
    rows = cur.fetchall()
    if len(rows) != 1:
        raise RuntimeError("Expected exactly one default listing to describe the property")
    return tuple(rows[0])


def build_schema(categories):
    """The JSON the model must answer in. Every key is required, so every
    answer has the same shape. Plain strings only: "" means 'none' for
    field and value, and normalise() turns it into NULL."""
    return {
        "type": "object",
        "properties": {
            "document_type": {"type": "string", "enum": DOCUMENT_TYPES},
            "category": {"type": "string", "enum": [code for code, _ in categories]},
            "scope": {"type": "string", "enum": ["listing", "property"]},
            "reason": {"type": "string"},
            "action": {"type": "string", "enum": ACTIONS},
            "field": {"type": "string", "enum": FIELDS + [""]},
            "value": {"type": "string"},
            "action_reason": {"type": "string"},
        },
        "required": ["document_type", "category", "scope", "reason",
                     "action", "field", "value", "action_reason"],
        "additionalProperties": False,
    }


def build_user_message(receipt, resolved, items, issues, text):
    """The receipt itself, as plain labelled lines."""
    rule = resolved["rule"]
    lines = [
        f"Merchant: {resolved['merchant_name'] or 'unknown'}",
        f"ABN: {resolved['abn'] or 'unknown'}",
        f"Date: {resolved['transaction_date'] or 'unknown'}",
        (f"Total: {receipt['total'] if receipt['total'] is not None else 'unknown'}"
        f"    GST: {receipt['gst'] if receipt['gst'] is not None else 'unknown'}"),
    ]

    # Tell the models what a saved rule has already settled, so they don't
    # flag it as a problem or "correct" it the wrong way.
    if rule:
        lines.append("Note: the Merchant above comes from a saved rule for this "
                     "sender or ABN; it is correct even if not printed on the document.")
        if rule["date_order"] == "MDY":
            lines.append("Note: this merchant prints dates MONTH FIRST (MM/DD/YYYY). "
                         "The Date above is already converted. If you give a date, "
                         "read the printed date month first.")

    lines.append("Line items:")
    lines += [f"- {item}" for item in items] if items else ["(none)"]
    if issues:                          # only when there are open issues
        lines.append("Open issues:")
        lines += [f"- {check_name}: {detail}" for check_name, detail in issues]
    lines.append("Document text:")
    lines.append(text or "(none)")
    return "\n".join(lines)


# ------------------------------------------------------------------
# Model call
# ------------------------------------------------------------------
def ask(client, model, system, schema, user_message):
    """One categorisation. Returns (answer dict, raw text, usage)."""
    response = client.messages.create(
        model=model,
        max_tokens=2000,       # room for thinking, which counts against this
        system=system,
        output_config={"effort": "low",
                       "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": user_message}],
    )
    # The format guarantees the text block is JSON matching the schema.
    raw = next(b.text for b in response.content if b.type == "text")
    return json.loads(raw), raw, response.usage


def categorisation_container():
    """The blob container that caches model answers."""
    service = BlobServiceClient.from_connection_string(
        os.environ["AZURE_STORAGE_CONNECTION_STRING"])
    return service.get_container_client(os.environ["AZURE_CATEGORISATION_CONTAINER"])


def receipt_blob_name(blob_url):
    """'https://.../receipts/<name>' -> '<name>' (the receipt's file in blob storage)."""
    return unquote(urlparse(blob_url).path.split("/", 2)[2])


def cache_name(blob_url, model, system, schema, user_message):
    """One cache entry per receipt file, model and EXACT request: any change
    to the prompt, schema, rules or open issues gives a new name."""
    request = json.dumps([model, system, schema, user_message], sort_keys=True)
    digest = hashlib.sha256(request.encode("utf-8")).hexdigest()[:16]
    return f"{receipt_blob_name(blob_url)}.{model}.{digest}.json"


def ask_cached(client, container, blob_url, model, system, schema, user_message):
    """Like ask(), but reuses a cached answer to the identical request.
    Returns (answer dict, raw text, from_cache).

    A new answer is saved to the cache BEFORE the database is updated, the
    same order as extraction: an answer that costs money and
    can't be reproduced is never lost to a database failure."""
    blob = container.get_blob_client(cache_name(blob_url, model, system, schema, user_message))
    if blob.exists():
        raw = blob.download_blob().readall().decode("utf-8")
        return json.loads(raw), raw, True

    answer, raw, _ = ask(client, model, system, schema, user_message)
    blob.upload_blob(raw.encode("utf-8"), overwrite=False)
    return answer, raw, False

# ------------------------------------------------------------------
# Validation of the answer
# ------------------------------------------------------------------
def valid_value(field, value):
    """True if value is usable for field, in the format the prompt asks for."""
    if not value or len(value) > VALUE_LIMIT:
        return False
    try:
        if field == "transaction_date":
            date.fromisoformat(value)                 # YYYY-MM-DD only
        elif field == "total":
            amount = Decimal(value)                   # '$53.97' raises here
            if amount <= 0 or amount.as_tuple().exponent < -2:
                return False
        return True                                   # merchant_name: any text that fits
    except (ValueError, InvalidOperation):
        return False


def normalise(answer):
    """A validated copy of the answer, ready for RECEIPT_CATEGORISATION.

    raw_response keeps the original, so nothing here loses information:
    - "" for field or value means 'none' and becomes NULL
    - a report, payment confirmation or 'other' document is never an
      expense, whatever action the model chose
    - field/value without set_field are meaningless, so they're dropped
    - set_field with a missing or malformed field/value is unusable: the
      action becomes NULL and review decides
    - display texts too long for their column are shortened
    """
    a = dict(answer)
    for key in ("field", "value"):
        if a.get(key) == "":
            a[key] = None
    if a.get("document_type") in NOT_EXPENSE_TYPES:
        a["action"] = "not_expense"
    if a.get("action") != "set_field":
        a["field"] = a["value"] = None
    elif not a.get("field") or not valid_value(a["field"], a.get("value")):
        a["action"] = a["field"] = a["value"] = None
    for key, limit in TEXT_LIMITS.items():
        if a.get(key) and len(a[key]) > limit:
            a[key] = a[key][: limit - 1] + "…"
    return a