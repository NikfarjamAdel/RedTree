"""Extract every pending receipt with Azure Document Intelligence.

For each RECEIPT row with status 'pending':

1. Choose the model by file type: PDFs use prebuilt-invoice, images use
   prebuilt-receipt.
2. Get the result through extraction_common.extract(), which reuses a cached
   result from the extraction container when one exists.
3. Store the result JSON and the model name in RECEIPT and set the status
   to 'extracted'.

Failures are handled in two ways:

- PermanentError (PDF too large, image cannot be shrunk, no document found)
  sets the status to 'failed' and records the reason in error_message.
- Any other error (rate limit, network, Azure outage) leaves the receipt
  'pending', records the reason and increments attempt_count. After
  MAX_ATTEMPTS the receipt is set to 'failed'.

Only pending receipts are processed, so the script is safe to rerun.

Usage:
    python3 python/extract_receipts.py [azure] [--limit N]

    azure       Use the Azure database instead of the local one.
    --limit N   Process only the first N pending receipts.
"""
import json
import sys
import time

from engine_setup import connect_db
from extraction_common import (
    KEY_FIELDS,
    MODELS,
    OTHER_MODEL,
    PermanentError,
    extract,
    key_field_count,
)

MAX_ATTEMPTS = 3
PAUSE_SECONDS = 2      # Pause after each Document Intelligence call (free tier is rate-limited)


def parse_args():
    """Return (target, limit) from the command line, in any order."""
    args = sys.argv[1:]
    target = "azure" if "azure" in args else "local"
    limit = None
    if "--limit" in args:
        i = args.index("--limit")
        limit = int(args[i + 1])
    return target, limit


def choose_model(content_type):
    """Return the MODELS key for a file: 'invoice' for PDFs, 'receipt' otherwise."""
    return "invoice" if content_type == "application/pdf" else "receipt"


def pending_receipts(cur, limit):
    """Return (receipt_id, blob_url, content_type, attempt_count) for each
    pending receipt, ordered by receipt_id.

    The rows are fetched up front because processing updates them.
    """
    cols = "receipt_id, blob_url, content_type, attempt_count"
    where = "FROM RECEIPT WHERE status = 'pending' ORDER BY receipt_id"
    if limit is None:
        cur.execute(f"SELECT {cols} {where}")
    else:
        cur.execute(f"SELECT TOP (?) {cols} {where}", limit)
    return cur.fetchall()


# Each outcome is committed immediately, so a crash partway through a run
# keeps every receipt processed before it.

def mark_extracted(db, cur, receipt_id, model, result):
    """Store a successful result and clear any earlier error."""
    extraction_json = json.dumps(result, default=str)
    cur.execute(
        "UPDATE RECEIPT "
        "SET status = 'extracted', "
        "    extraction_json = ?, "
        "    extraction_model = ?, "
        "    extracted_at = SYSDATETIME(), "
        "    error_message = NULL "
        "WHERE receipt_id = ?",
        extraction_json, MODELS[model], receipt_id,
    )
    db.commit()


def mark_failed(db, cur, receipt_id, message):
    """Mark a receipt as failed for a reason that retrying will not fix."""
    cur.execute(
        "UPDATE RECEIPT "
        "SET status = 'failed', "
        "error_message = ? "
        "WHERE receipt_id = ? ",
        message[:500], receipt_id,
    )
    db.commit()


def mark_retry(db, cur, receipt_id, attempts_so_far, message):
    """Record a temporary failure and return the new status.

    The receipt stays 'pending' until it reaches MAX_ATTEMPTS, then becomes
    'failed'.
    """
    attempts = attempts_so_far + 1
    status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
    cur.execute(
        "UPDATE RECEIPT "
        "SET status = ?, "
        "error_message = ?, "
        "attempt_count = ? "
        "WHERE receipt_id = ? ",
        status, message[:500], attempts, receipt_id,
    )
    db.commit()
    return status


def main():
    target, limit = parse_args()
    db = connect_db(target)
    cur = db.cursor()

    todo = pending_receipts(cur, limit)
    print(f"{len(todo)} pending receipts\n")

    n_new = n_cached = n_failed = n_retry = 0
    try:
        for i, (receipt_id, blob_url, content_type, attempts) in enumerate(todo, start=1):
            print(f"[{i}/{len(todo)}] receipt {receipt_id} ({content_type}) ",
                  end="", flush=True)
            try:
                model = choose_model(content_type)
                result, from_cache = extract(blob_url, content_type, model)
                if not from_cache:
                    time.sleep(PAUSE_SECONDS)

                # Fallback: if any key field is missing, try the other model
                # and keep whichever result has more key fields. 
                used_fallback = False
                if key_field_count(result, model) < len(KEY_FIELDS[model]):
                    other = OTHER_MODEL[model]
                    alt, alt_cached = extract(blob_url, content_type, other)
                    if not alt_cached:
                        time.sleep(PAUSE_SECONDS)
                    if key_field_count(alt, other) > key_field_count(result, model):
                        result, model, from_cache = alt, other, alt_cached
                        used_fallback = True

                # A file with no recognisable document (a logo, an unrelated
                # photo) will not improve on retry.
                if not result.get("documents"):
                    raise PermanentError("No document found")

                mark_extracted(db, cur, receipt_id, model, result)
                if from_cache:
                    n_cached += 1
                else:
                    n_new += 1
                print(f"-> extracted ({MODELS[model]}, {'cached' if from_cache else 'new'}"
                      f"{', fallback' if used_fallback else ''})")

            except PermanentError as e:
                mark_failed(db, cur, receipt_id, str(e))
                n_failed += 1
                print(f"-> failed: {e}")

            except Exception as e:
                # Unclassified errors are treated as temporary: retrying is
                # cheap, while giving up would silently lose the receipt.
                db.rollback()      # In case the error happened mid-UPDATE
                status = mark_retry(db, cur, receipt_id, attempts, str(e))
                if status == 'failed':
                    n_failed += 1
                else:
                    n_retry += 1
                print(f"-> {status}: {type(e).__name__}: {e}")
    finally:
        db.close()

    print(f"\n{n_new} extracted (new), {n_cached} extracted (cached), "
          f"{n_failed} failed, {n_retry} left pending for retry")


if __name__ == "__main__":
    main()
