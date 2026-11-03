"""Categorise receipts with both models; store every answer; set status.

Processes receipts with status 'validated' or 'needs_review' that lack an
answer from every model for the current PROMPT_VERSION.

Each answer is stored in RECEIPT_CATEGORISATION (raw + validated). Then:
  validated + models agree + action 'accept'   -> 'categorised'
  validated + models disagree                  -> model_disagreement, 'needs_review'
  validated + agree on another action          -> review_suggested, 'needs_review'
  needs_review                                 -> unchanged (answers are suggestions)

A merchant rule's category, when it has one, is used for both answers in
the comparison, so the models can't override a rule.

Usage: python3 python/categorise_receipts.py [azure] [--limit N]
"""
import json
import sys

import anthropic
from categorise_common import (
    MODELS,
    PROMPT_VERSION,
    TEXT_LIMIT,
    ask_cached,
    build_schema,
    build_system,
    build_user_message,
    categorisation_container,
    load_property,
    normalise,
)
from engine_setup import connect_db
from receipt_rules import load_rules, resolve
from validate_receipts import load_receipts

COMPARED = ("document_type", "category", "scope", "action")

# Issues both models can clear by agreeing to 'accept': each is a question
# the document text can answer. Duplicates, missing fields and second
# documents always go to a person.
AUTO_RESOLVABLE = {"low_confidence", "amount_mismatch", "gst_too_high",
                   "date_too_early", "invalid_abn"}

EXPENSE_DOCUMENTS = {"receipt", "invoice"}

# Document types that, when both models agree on them, are not expenses.
# Payment confirmations stay with a person: without the bill they ARE the expense.
AUTO_NOT_EXPENSE_TYPES = {"report", "other"}


def resolve_open(cur, receipt_id, note):
    cur.execute("UPDATE RECEIPT_ISSUE SET resolved_at = SYSDATETIME(), resolution = ? "
                "WHERE receipt_id = ? AND resolved_at IS NULL", note, receipt_id)


def parse_args():
    args = sys.argv[1:]
    target = "azure" if "azure" in args else "local"
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else None
    return target, limit


def done_models(cur, receipt_id):
    """Models that already have an answer for this receipt and prompt."""
    cur.execute("SELECT model FROM RECEIPT_CATEGORISATION "
                "WHERE receipt_id = ? AND prompt_version = ?",
                receipt_id, PROMPT_VERSION)
    return {row[0] for row in cur.fetchall()}


def store_answer(cur, receipt_id, model, clean, raw):
    """One row in RECEIPT_CATEGORISATION (not committed here)."""
    cur.execute(
        "INSERT INTO RECEIPT_CATEGORISATION "
        "(receipt_id, model, prompt_version, document_type, category, scope, "
        "reason, action, action_field, action_value, action_reason, raw_response) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ",
        receipt_id, model, PROMPT_VERSION, clean['document_type'], clean['category'],
        clean['scope'], clean['reason'], clean['action'], clean['field'], clean['value'],
        clean['action_reason'], raw, 
    )


ANSWER_COLUMNS = ("document_type", "category", "scope", "action", "action_field", "action_value")


def stored_answers(cur, receipt_id):
    """This receipt's validated answers for the current prompt, one per model."""
    cur.execute(f"SELECT {', '.join(ANSWER_COLUMNS)} FROM RECEIPT_CATEGORISATION "
                "WHERE receipt_id = ? AND prompt_version = ?",
                receipt_id, PROMPT_VERSION)
    return [dict(zip(ANSWER_COLUMNS, row)) for row in cur.fetchall()]


def add_issue(cur, receipt_id, check_name, detail):
    """Insert an issue unless one with this check_name already exists."""
    cur.execute(
        "INSERT INTO RECEIPT_ISSUE (receipt_id, check_name, detail) "
        "SELECT ?, ?, ? "
        "WHERE NOT EXISTS (SELECT 1 FROM RECEIPT_ISSUE "
        "                  WHERE receipt_id = ? AND check_name = ?) ",
        receipt_id, check_name, detail, receipt_id, check_name, 
    )

def open_issues(cur, receipt_id):
    cur.execute("SELECT check_name FROM RECEIPT_ISSUE "
                "WHERE receipt_id = ? AND resolved_at IS NULL", receipt_id)
    return {row[0] for row in cur.fetchall()}

def current_value(field, receipt, resolved):
    """A key field's current value, in the format set_field values use."""
    if field == "transaction_date":
        return resolved["transaction_date"].isoformat() if resolved["transaction_date"] else None
    if field == "total":
        return f"{receipt['total']:.2f}" if receipt["total"] is not None else None
    if field == "merchant_name":
        return resolved["merchant_name"]
    return None


def comparable(answer, receipt, resolved):
    """The parts of an answer the models must agree on, by meaning:
    - receipt and invoice are the same thing: a document showing an expense
    - set_field to the value the field already has changes nothing: accept"""
    doc = "expense" if answer["document_type"] in EXPENSE_DOCUMENTS else answer["document_type"]
    action = answer["action"]
    if (action == "set_field"
            and answer["action_value"] == current_value(answer["action_field"], receipt, resolved)):
        action = "accept"
    return (doc, answer["category"], answer["scope"], action)

def decide(cur, receipt, resolved, status, answers):
    """Set the next status from the stored answers. Returns the new status.

    This step's own issues (model_disagreement, review_suggested) are deleted
    and recomputed on every run, like validation's: an earlier disagreement
    may no longer hold after a new rule or prompt version.

    Then:
      both models say report/other            -> not_expense (auto note)
      models disagree (by meaning)             -> model_disagreement
      models agree on an action other than
        accept for a receipt/invoice           -> review_suggested
      models accept and every open issue is
        AUTO_RESOLVABLE (or there are none)    -> categorised (issues auto-resolved)
      otherwise                                -> needs_review
    """
    receipt_id = receipt["receipt_id"]
    if status not in ("validated", "needs_review") or len(answers) < len(MODELS):
        return status

    rule = resolved["rule"]
    if rule:                                   # a rule's category and scope beat the models
        for a in answers:
            if rule["category"]:
                a["category"] = rule["category"]
            if rule["scope"]:
                a["scope"] = rule["scope"]

    cur.execute(
        "DELETE FROM RECEIPT_ISSUE WHERE receipt_id = ? AND resolved_at IS NULL "
        "AND check_name IN ('model_disagreement', 'review_suggested')",
        receipt_id)

    doc_type = answers[0]["document_type"]
    if (doc_type in AUTO_NOT_EXPENSE_TYPES
            and all(a["document_type"] == doc_type for a in answers)):
        resolve_open(cur, receipt_id, f"auto: both models say {doc_type}, not an expense")
        cur.execute("UPDATE RECEIPT SET status = 'not_expense' WHERE receipt_id = ?",
                    receipt_id)
        return "not_expense"

    meanings = [comparable(a, receipt, resolved) for a in answers]
    agree = all(m == meanings[0] for m in meanings)
    accepted = agree and meanings[0][0] == "expense" and meanings[0][3] == "accept"

    if not agree:
        detail = "; ".join(f"{a['document_type']}/{a['category']}/{a['scope']}/{a['action']}"
                           for a in answers)
        add_issue(cur, receipt_id, "model_disagreement", detail)
    elif not accepted:
        add_issue(cur, receipt_id, "review_suggested",
                  f"both models suggest {meanings[0][3]}")

    open_now = open_issues(cur, receipt_id)
    if accepted and open_now <= AUTO_RESOLVABLE:
        if open_now:
            resolve_open(cur, receipt_id,
                         f"auto: both models accept (prompt {PROMPT_VERSION})")
        new = "categorised"
    else:
        new = "needs_review"

    cur.execute("UPDATE RECEIPT SET status = ? WHERE receipt_id = ?", new, receipt_id)
    return new



def main():
    target, limit = parse_args()
    db = connect_db(target)
    cur = db.cursor()
    client = anthropic.Anthropic()
    container = categorisation_container()
    cached = 0

    try:
        rules = load_rules(cur)
        cur.execute("SELECT category_code, description FROM CATEGORY ORDER BY category_code")
        categories = cur.fetchall()
        system, schema = build_system(categories, load_property(cur)), build_schema(categories)

        todo = [r for r in load_receipts(cur)
                if r["status"] in ("validated", "needs_review")]
        if limit:
            todo = todo[:limit]
        print(f"{len(todo)} receipts to categorise (prompt {PROMPT_VERSION})\n")

        counts = {}
        for receipt in todo:
            rid = receipt["receipt_id"]
            try:
                resolved = resolve(receipt, rules)

                cur.execute("SELECT description FROM vw_receipt_line_items "
                            "WHERE receipt_id = ? ORDER BY line_no", rid)
                items = [row[0] for row in cur.fetchall() if row[0]]
                cur.execute("SELECT check_name, detail FROM RECEIPT_ISSUE "
                            "WHERE receipt_id = ? AND resolved_at IS NULL", rid)
                issues = cur.fetchall()
                raw_json = receipt["extraction_json"]
                text = json.loads(raw_json).get("content", "")[:TEXT_LIMIT] if raw_json else ""
                user_message = build_user_message(receipt, resolved, items, issues, text)

                for model in MODELS:
                    if model in done_models(cur, rid):
                        continue
                    answer, raw, from_cache = ask_cached(
                        client, container, receipt["blob_url"],
                        model, system, schema, user_message)
                    cached += from_cache
                    store_answer(cur, rid, model, normalise(answer), raw)

                status = decide(cur, receipt, resolved, receipt["status"],
                                stored_answers(cur, rid))
                db.commit()                  # one receipt = one transaction
                counts[status] = counts.get(status, 0) + 1
                print(f"receipt {rid}: {status}")

            except Exception as e:
                # API or database error: nothing for this receipt is kept,
                # and the next run tries it again.
                db.rollback()
                print(f"receipt {rid}: FAILED {type(e).__name__}: {e}")
    finally:
        db.close()

    print("\n" + ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
          + f"  ({cached} answers from cache)")


if __name__ == "__main__":
    main()