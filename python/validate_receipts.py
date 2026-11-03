"""Validate extracted receipts: record problems in RECEIPT_ISSUE and set status.

For each receipt with status 'extracted':
    duplicate_file found     -> 'not_expense' (certain duplicate, automatic)
    any other issue          -> 'needs_review'
    no issues                -> 'validated'

Duplicates are checked against EARLIER receipts (lower receipt_id), so
the first copy proceeds and later copies are flagged.

Merchant name and date come from receipt_rules.resolve(), so a matching
MERCHANT_RULE can fill a missing name or fix a month-first date before
any check runs.

Usage: python3 python/validate_receipts.py [azure]
"""
import sys
from datetime import timedelta

from engine_setup import connect_db
from receipt_rules import load_rules, resolve

MIN_CONFIDENCE = 0.80          # total or date below this -> low_confidence
TOLERANCE = 0.02               # dollars, for arithmetic checks
DATE_WINDOW_DAYS = 200         # purchase date at most this long before the email
SECOND_DOC_THRESHOLD = 0.50    # a second document scoring this or more -> review


def load_receipts(cur):
    """Every receipt the view exposes, with its sender and email date.

    vw_receipt_fields already excludes 'failed' receipts and any receipt
    without extraction JSON, so every row here has JSON. All statuses
    are loaded (not just 'extracted') because duplicate checks compare
    against earlier receipts whatever their status."""
    cur.execute("""
        SELECT f.*, r.status, r.file_sha256, r.extraction_json, r.blob_url,
               r.content_type, e.sender, e.recipient, e.received_at
        FROM vw_receipt_fields f
        JOIN RECEIPT   r ON r.receipt_id = f.receipt_id
        JOIN EMAIL_RAW e ON e.email_id   = r.email_id
        ORDER BY f.receipt_id
    """)
    columns = [c[0] for c in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def times_differ(a, b):
    """True if both times are known and different: two separate purchases.
    If either is missing, we can't tell, so it isn't treated as different."""
    return a is not None and b is not None and a != b

def check(receipt, resolved, earlier):
    """All issues for one receipt, as a list of (check_name, detail).

    receipt:  a row from load_receipts()
    resolved: resolve(receipt, rules) for that row
    earlier:  (receipt, resolved) pairs with a lower receipt_id that
              aren't 'not_expense'

    A check that needs a missing value is skipped; the missing_* checks
    already report the gap.
    """
    issues = []
    total = receipt["total"]
    date = resolved["transaction_date"]

    # Duplicates: the same file is certain; the same merchant, date and
    # total only likely. Only the first is reported, since the same file
    # always has the same fields.
    same_file = next((r for r, _ in earlier
                      if r["file_sha256"] == receipt["file_sha256"]), None)
    key = (resolved["merchant_name"], date, total)
    same_fields = None
    if None not in key:
        same_fields = next(
            (r for r, res in earlier
             if (res["merchant_name"], res["transaction_date"], r["total"]) == key
             and not times_differ(receipt["transaction_time"], r["transaction_time"])),
            None)
    if same_file:
        issues.append(("duplicate_file", f"same file as receipt {same_file['receipt_id']}"))
    elif same_fields:
        issues.append(("possible_duplicate",
                       f"same merchant, date and total as receipt {same_fields['receipt_id']}"))

    # Key fields. Merchant and date are the resolved ones, so a rule can
    # fill them in.
    if not resolved["merchant_name"]:
        issues.append(("missing_merchant", "merchant name is missing"))
    if total is None:
        issues.append(("missing_total", "total is missing"))
    if date is None:
        issues.append(("missing_date", "date is missing"))

    # Confidence: Azure's score for the total or the date is too low to trust.
    total_conf = receipt["total_confidence"]
    date_conf = receipt["transaction_date_confidence"]
    if ((total_conf is not None and total_conf < MIN_CONFIDENCE)
            or (date_conf is not None and date_conf < MIN_CONFIDENCE)):
        issues.append(("low_confidence", f"low confidence, total: {total_conf}, date: {date_conf}"))

    # Arithmetic. The total may be printed with GST added (subtotal + gst)
    # or already included (subtotal = total); a mismatch is neither.
    # GST is 1/11 of a GST-inclusive total, so more than that is wrong.
    subtotal = receipt["subtotal"]
    gst = receipt["gst"]
    if subtotal is not None and gst is not None and total is not None:
        if abs(subtotal + gst - total) > TOLERANCE and abs(subtotal - total) > TOLERANCE:
            issues.append(("amount_mismatch", f"amounts are not matched, subtotal: {subtotal}, gst: {gst}, total: {total}"))
    if gst is not None and total is not None:
        if float(gst) > float(total / 11) + TOLERANCE:
            issues.append(("gst_too_high", f"gst is too high, gst: {gst}, total: {total}"))

    # Date plausibility: a purchase can't be after the email that sent its
    # receipt, and one far earlier is probably a misread date.
    if date is not None:
        received = receipt["received_at"].date()
        if date > received:
            issues.append(("date_after_email", f"date {date}, email received {received}"))
        elif date < received - timedelta(days=DATE_WINDOW_DAYS):
            issues.append(("date_too_early",
                           (f"date {date}, email received {received}, "
                            f"more than {DATE_WINDOW_DAYS} days apart")))

    # Other: the file may hold a second receipt, or the ABN fails its check
    # digit. abn_valid is None when no ABN was found, which is fine.
    second = receipt["second_doc_confidence"]
    if second is not None and second >= SECOND_DOC_THRESHOLD:
        issues.append(("second_document", f"second document confidence {second}"))
    if resolved["abn_valid"] is False:
        issues.append(("invalid_abn", f"ABN {resolved['abn']} fails the check digit"))

    return issues


def save(db, cur, receipt_id, issues):
    """Replace this receipt's unresolved issues and set its status, in
    one transaction. Returns the new status.

    Resolved issues are kept: they're review history. A check that was
    already resolved isn't raised again, both because a reviewer has
    accepted it and because UQ_RECEIPT_ISSUE would refuse the insert.
    """
    cur.execute(
        "DELETE FROM RECEIPT_ISSUE "
        "WHERE receipt_id = ? "
        "AND resolved_at IS NULL ",
        receipt_id,
    )
    cur.execute(
        "SELECT check_name FROM RECEIPT_ISSUE "
        "WHERE receipt_id = ? AND resolved_at IS NOT NULL",
        receipt_id,
    )
    resolved_names = {row[0] for row in cur.fetchall()}

    # Set before the skip, so a duplicate still becomes 'not_expense'
    # even if its issue was resolved earlier.
    open_issues = [(name, detail) for name, detail in issues
                   if name not in resolved_names]
    for check_name, detail in open_issues:
        cur.execute(
            "INSERT INTO RECEIPT_ISSUE (receipt_id, check_name, detail) VALUES (?, ?, ?)",
            receipt_id, check_name, detail,
        )

    if any(name == "duplicate_file" for name, _ in issues):
        status = "not_expense"          # checked on ALL issues: a duplicate stays one
    elif open_issues:
        status = "needs_review"
    else:
        status = "validated"
    cur.execute(
        "UPDATE RECEIPT SET status = ? WHERE receipt_id = ? ",
        status, receipt_id,
    )
    db.commit()
    return status


def main():
    target = "azure" if "azure" in sys.argv[1:] else "local"
    db = connect_db(target)
    cur = db.cursor()
    try:
        rules = load_rules(cur)
        receipts = load_receipts(cur)
        resolved = {r["receipt_id"]: resolve(r, rules) for r in receipts}

        counts = {"validated": 0, "needs_review": 0, "not_expense": 0}
        for receipt in receipts:
            if receipt["status"] != "extracted":
                continue
            earlier = [(r, resolved[r["receipt_id"]]) for r in receipts
                       if r["receipt_id"] < receipt["receipt_id"]
                       and r["status"] != "not_expense"]
            issues = check(receipt, resolved[receipt["receipt_id"]], earlier)
            status = save(db, cur, receipt["receipt_id"], issues)
            counts[status] += 1
            names = ", ".join(name for name, _ in issues) or "ok"
            print(f"receipt {receipt['receipt_id']}: {status}  ({names})")
    finally:
        db.close()

    print(f"\n{counts['validated']} validated, {counts['needs_review']} need review, "
          f"{counts['not_expense']} not expenses")


if __name__ == "__main__":
    main()
