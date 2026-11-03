"""Create EXPENSE rows from categorised receipts.

For each RECEIPT with status 'categorised', in one transaction:
  - start from the pipeline's values: the resolved fields
    (receipt_rules.resolve), the merchant rule or the models' agreed
    answer, and the category's default expense type
  - the receipt is routed to a listing: the one whose receipt address the
    email was sent to, else the default listing (the plain address)
  - a listing-scope expense takes the merchant rule's listing, else the
    routed one; a property-scope expense takes the routed listing's
    PROPERTY, with no listing. Every expense records its property.
    If routing gives nothing, the receipt goes to review.
  - if the receipt has been reviewed, the latest review's values win and
    the expense is marked verified
  - insert the expense and set the receipt to 'done'

If the same supplier invoice is already an expense, the database refuses the
insert (UX_EXPENSE_supplier_invoice); the receipt then goes to 'needs_review'
with a possible_duplicate issue. A receipt that already has an expense is
simply marked 'done', so a rerun never creates a second one.

Usage: python3 python/create_expenses.py [azure]
"""
import sys

import pyodbc
from categorise_receipts import add_issue
from engine_setup import connect_db
from receipt_rules import load_rules, resolve
from validate_receipts import load_receipts

SUPPLIER_LIMIT = 250        # EXPENSE.supplier
INVOICE_LIMIT = 50          # EXPENSE.invoice_number

COLUMNS = ("property_id", "listing_id", "booking_id", "expense_date", "expense_amount",
           "category", "expense_type", "supplier", "receipt_id", "scope",
           "supplier_abn", "invoice_number", "verified_by_user")

# Fields a review can set (RECEIPT_REVIEW column = EXPENSE column)
REVIEW_FIELDS = ("expense_date", "expense_amount", "supplier", "category",
                 "scope", "listing_id", "booking_id", "expense_type")


class NeedsReview(Exception):
    """The receipt can't become an expense as it stands; the message says why."""


def listing_for(cur, recipient):
    """The listing a receipt was sent to:
    1. the listing whose receipt address the email was sent to;
    2. otherwise the default listing (the plain address);
    3. otherwise None, and the receipt goes to review."""
    if recipient:
        cur.execute("SELECT listing_id FROM LISTING WHERE receipt_address = ?", recipient)
        row = cur.fetchone()
        if row:
            return row[0]
    cur.execute("SELECT listing_id FROM LISTING WHERE default_for_receipts = 1")
    row = cur.fetchone()
    return row[0] if row else None


def listing_properties(cur):
    """{listing_id: property_id} for every listing."""
    cur.execute("SELECT listing_id, property_id FROM LISTING")
    return {listing_id: property_id for listing_id, property_id in cur.fetchall()}


def expense_types(cur):
    """Each category's default expense type."""
    cur.execute("SELECT category_code, default_expense_type FROM CATEGORY")
    return {code: kind for code, kind in cur.fetchall()}


def latest_answer(cur, receipt_id):
    """(category, scope) from the most recent model answer."""
    cur.execute("""
        SELECT TOP 1 category, scope
        FROM RECEIPT_CATEGORISATION
        WHERE receipt_id = ?
        ORDER BY created_at DESC
    """, receipt_id)
    row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def latest_review(cur, receipt_id):
    """The most recent 'expense' review for this receipt, as a dict, or None."""
    cur.execute(f"""
        SELECT TOP 1 {', '.join(REVIEW_FIELDS)}
        FROM RECEIPT_REVIEW
        WHERE receipt_id = ? AND decision = 'expense'
        ORDER BY reviewed_at DESC, review_id DESC
    """, receipt_id)
    row = cur.fetchone()
    return dict(zip(REVIEW_FIELDS, row)) if row else None


def build_expense(receipt, resolved, answer, review, types, routed_listing, properties):
    """The EXPENSE row as a dict. Raises NeedsReview if it can't be built.

    routed_listing: from listing_for(), the listing the receipt was sent to.
    properties: {listing_id: property_id}, from listing_properties()."""
    rule = resolved["rule"]
    e = {
        "expense_date": resolved["transaction_date"],
        "expense_amount": receipt["total"],
        "supplier": resolved["merchant_name"],
        "category": (rule and rule["category"]) or answer[0],     # a rule beats the models
        "scope": (rule and rule["scope"]) or answer[1],
        "listing_id": None,
        "booking_id": None,
        "expense_type": None,
    }
    if review:                                    # your values win
        for field in REVIEW_FIELDS:
            if review[field] is not None:
                e[field] = review[field]

    missing = [f for f in ("expense_amount", "expense_date", "supplier", "category", "scope")
               if e[f] is None]
    if missing:
        raise NeedsReview("cannot create expense, missing: " + ", ".join(missing))
    if len(e["supplier"]) > SUPPLIER_LIMIT:
        raise NeedsReview(f"merchant name longer than {SUPPLIER_LIMIT} characters")
    invoice = receipt["invoice_number"] or None
    if invoice and len(invoice) > INVOICE_LIMIT:
        raise NeedsReview(f"invoice number longer than {INVOICE_LIMIT} characters")

    if e["scope"] == "property":
        # Whole property: no listing; the property is the routed listing's
        e["listing_id"] = None
        if routed_listing is None:
            raise NeedsReview("property scope, but the receipt doesn't say which property")
        e["property_id"] = properties[routed_listing]
    else:
        if e["listing_id"] is None:
            e["listing_id"] = (rule and rule["listing_id"]) or routed_listing
        if e["listing_id"] is None:
            raise NeedsReview("listing scope, but the receipt doesn't say which listing")
        e["property_id"] = properties[e["listing_id"]]

    e["expense_type"] = e["expense_type"] or types[e["category"]]
    e["receipt_id"] = receipt["receipt_id"]
    e["supplier_abn"] = resolved["abn"] if resolved["abn_valid"] else None
    e["invoice_number"] = invoice
    e["verified_by_user"] = 1 if review else 0
    return e


def insert_expense(cur, expense):
    """Insert one expense; return its id. Not committed here."""
    placeholders = ", ".join("?" * len(COLUMNS))
    cur.execute(
        f"INSERT INTO EXPENSE ({', '.join(COLUMNS)}, extracted_by) "
        f"OUTPUT INSERTED.expense_id "
        f"VALUES ({placeholders}, 'pipeline')",
        *[expense[c] for c in COLUMNS],
    )
    return cur.fetchone()[0]


def set_status(cur, receipt_id, status):
    cur.execute("UPDATE RECEIPT SET status = ? WHERE receipt_id = ?", status, receipt_id)


def main():
    target = "azure" if "azure" in sys.argv[1:] else "local"
    db = connect_db(target)
    cur = db.cursor()
    counts = {"done": 0, "needs_review": 0, "failed": 0}

    try:
        rules = load_rules(cur)
        types = expense_types(cur)
        properties = listing_properties(cur)
        todo = [r for r in load_receipts(cur) if r["status"] == "categorised"]
        print(f"{len(todo)} categorised receipts\n")

        for receipt in todo:
            rid = receipt["receipt_id"]
            expense = None
            try:
                # Safe rerun: an expense already made from this receipt
                cur.execute("SELECT expense_id FROM EXPENSE WHERE receipt_id = ?", rid)
                existing = cur.fetchone()
                if existing:
                    set_status(cur, rid, "done")
                    db.commit()
                    counts["done"] += 1
                    print(f"receipt {rid}: already expense {existing[0]}, marked done")
                    continue

                resolved = resolve(receipt, rules)
                expense = build_expense(receipt, resolved, latest_answer(cur, rid),
                                        latest_review(cur, rid), types,
                                        listing_for(cur, receipt["recipient"]), properties)
                expense_id = insert_expense(cur, expense)
                set_status(cur, rid, "done")
                db.commit()                      # expense and status together
                counts["done"] += 1
                verified = "  (reviewed)" if expense["verified_by_user"] else ""
                print(f"receipt {rid}: expense {expense_id}  {expense['expense_date']}  "
                      f"{expense['expense_amount']}  {expense['category']}/"
                      f"{expense['scope']}/{expense['expense_type']}  "
                      f"{expense['supplier']}{verified}")

            except NeedsReview as e:
                db.rollback()
                add_issue(cur, rid, "review_suggested", str(e)[:500])
                set_status(cur, rid, "needs_review")
                db.commit()
                counts["needs_review"] += 1
                print(f"receipt {rid}: needs_review ({e})")

            except pyodbc.IntegrityError as e:
                db.rollback()
                if expense is None or "UX_EXPENSE_supplier_invoice" not in str(e):
                    counts["failed"] += 1
                    print(f"receipt {rid}: FAILED {e}")
                    continue
                cur.execute("SELECT expense_id FROM EXPENSE "
                            "WHERE supplier_abn = ? AND invoice_number = ?",
                            expense["supplier_abn"], expense["invoice_number"])
                other = cur.fetchone()[0]
                add_issue(cur, rid, "possible_duplicate",
                          f"same ABN and invoice number as expense {other}")
                set_status(cur, rid, "needs_review")
                db.commit()
                counts["needs_review"] += 1
                print(f"receipt {rid}: needs_review (duplicate of expense {other})")

            except Exception as e:
                db.rollback()
                counts["failed"] += 1
                print(f"receipt {rid}: FAILED {type(e).__name__}: {e}")
    finally:
        db.close()

    print(f"\n{counts['done']} expenses created, {counts['needs_review']} sent to review, "
          f"{counts['failed']} failed")


if __name__ == "__main__":
    main()