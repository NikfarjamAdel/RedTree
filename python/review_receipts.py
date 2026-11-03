"""Review receipts by hand: the queue, and optionally an audit of
expenses the pipeline created on its own.

Queue (default): receipts in 'needs_review'. Each decision is recorded in
RECEIPT_REVIEW, and the receipt moves on:
    expense      -> 'categorised'  (create_expenses.py then builds it, verified)
    not expense  -> 'not_expense'
Audit (--audit, --audit-all): unverified pipeline expenses, by default only
those of AUDIT_THRESHOLD or more. Confirm marks one verified; a change
removes the expense and sends its receipt back to 'categorised', so
create_expenses.py rebuilds it with your values.

Runs against Azure, the system of record, unless 'local' is given.

Usage:
    python3 python/review_receipts.py [local]
    python3 python/review_receipts.py [local] --audit
    python3 python/review_receipts.py [local] --audit-all
Afterwards: python3 python/create_expenses.py azure
"""
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import partial

import pyodbc
from create_expenses import SUPPLIER_LIMIT, listing_for
from engine_setup import connect_db
from open_receipts import open_receipt
from receipt_rules import load_rules, resolve
from validate_receipts import load_receipts

AUDIT_THRESHOLD = Decimal("300.00")

SCOPES = ("listing", "property")
EXPENSE_TYPES = ("direct", "periodic", "capital", "consumable")
NOT_EXPENSE_REASONS = {"r": "report", "p": "payment_confirmation",
                       "d": "duplicate", "x": "personal", "o": "other"}

# The five values every expense decision records
KEY_FIELDS = ("expense_date", "expense_amount", "supplier", "category", "scope")
# set_field names (from the models) -> expense field names
SET_FIELD_MAP = {"transaction_date": "expense_date",
                 "total": "expense_amount",
                 "merchant_name": "supplier"}


# ------------------------------------------------------------------
# Input
# ------------------------------------------------------------------
def ask_choice(prompt, keys):
    """One key from keys."""
    while True:
        answer = input(prompt).strip().lower()
        if answer in keys:
            return answer
        print(f"  choose one of: {', '.join(keys)}")


def ask_value(label, current, parse):
    """Show the current value; Enter keeps it, typing replaces it."""
    while True:
        text = input(f"  {label} [{current}]: ").strip()
        if not text:
            return current
        try:
            return parse(text)
        except (ValueError, InvalidOperation) as e:
            print(f"    not valid: {e}")


def parse_date(text):
    return date.fromisoformat(text)


def parse_amount(text):
    amount = Decimal(text)
    if amount <= 0 or amount.as_tuple().exponent < -2:
        raise ValueError("a positive amount with at most two decimals")
    return amount


def parse_text(limit, text):
    if len(text) > limit:
        raise ValueError(f"at most {limit} characters")
    return text


def parse_from(options, text):
    if text not in options:
        raise ValueError(f"one of {', '.join(options)}")
    return text


def parse_id(text):
    return int(text)


# ------------------------------------------------------------------
# Reading
# ------------------------------------------------------------------
def model_answers(cur, receipt_id):
    """Each model's most recent answer for this receipt."""
    cur.execute("""
        SELECT c.model, c.document_type, c.category, c.scope, c.action,
               c.action_field, c.action_value, c.action_reason
        FROM RECEIPT_CATEGORISATION c
        WHERE c.receipt_id = ?
          AND c.created_at = (SELECT MAX(c2.created_at) FROM RECEIPT_CATEGORISATION c2
                              WHERE c2.receipt_id = c.receipt_id AND c2.model = c.model)
        ORDER BY c.model
    """, receipt_id)
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def open_issue_rows(cur, receipt_id):
    cur.execute("SELECT check_name, detail FROM RECEIPT_ISSUE "
                "WHERE receipt_id = ? AND resolved_at IS NULL", receipt_id)
    return cur.fetchall()


def agreed(answers, key):
    """The models' common value for key, or None if they differ or are absent."""
    if answers and all(a[key] == answers[0][key] for a in answers):
        return answers[0][key]
    return None


def proposal(receipt, resolved, answers):
    """The pipeline's values: the starting point for a decision."""
    rule = resolved["rule"]
    values = {
        "expense_date": resolved["transaction_date"],
        "expense_amount": receipt["total"],
        "supplier": resolved["merchant_name"],
        "category": (rule and rule["category"]) or agreed(answers, "category"),
        "scope": (rule and rule["scope"]) or agreed(answers, "scope"),
    }
    # A set_field both models agree on becomes the starting value
    field = SET_FIELD_MAP.get(agreed(answers, "action_field") or "")
    value = agreed(answers, "action_value")
    if agreed(answers, "action") == "set_field" and field and value:
        try:
            values[field] = (parse_date(value) if field == "expense_date"
                             else parse_amount(value) if field == "expense_amount"
                             else value)
        except (ValueError, InvalidOperation):
            pass                                  # unusable suggestion: ignore it
    return values


# ------------------------------------------------------------------
# Display
# ------------------------------------------------------------------
def open_file(receipt):
    """Download the receipt and open it in Preview, unless it is refused."""
    open_receipt(receipt["receipt_id"], receipt["blob_url"], receipt["content_type"])


def show(receipt, resolved, values, issues, answers):
    print("\n" + "=" * 72)
    print(f"Receipt {receipt['receipt_id']}   ({receipt['status']})   from {receipt['sender']}")
    for key, value in values.items():
        print(f"  {key:16} {value}")
    abn_state = {True: "valid", False: "INVALID", None: "none found"}[resolved["abn_valid"]]
    print(f"  {'abn':16} {resolved['abn']}  ({abn_state})")
    if resolved["rule"]:
        print(f"  {'rule':16} {resolved['rule']['merchant_name']} ({resolved['rule']['match_type']})")
    if issues:
        print("Open issues:")
        for check_name, detail in issues:
            print(f"  - {check_name}: {detail}")
    for a in answers:
        extra = f"  {a['action_field']}={a['action_value']}" if a["action_field"] else ""
        print(f"{a['model']}: {a['document_type']}/{a['category']}/{a['scope']}/{a['action']}{extra}")
        print(f"    {a['action_reason']}")


# ------------------------------------------------------------------
# Writing (callers commit)
# ------------------------------------------------------------------
def edit(values, categories):
    """Ask for each field; return all values after editing."""
    print("Edit (Enter keeps the value in brackets):")
    new = dict(values)
    new["expense_date"] = ask_value("date (YYYY-MM-DD)", values["expense_date"], parse_date)
    new["expense_amount"] = ask_value("total", values["expense_amount"], parse_amount)
    new["supplier"] = ask_value("supplier", values["supplier"], partial(parse_text, SUPPLIER_LIMIT))
    new["category"] = ask_value("category", values["category"], partial(parse_from, categories))
    new["scope"] = ask_value("scope", values["scope"], partial(parse_from, SCOPES))
    new["expense_type"] = ask_value("expense type (blank = category default)",
                                    values.get("expense_type"),
                                    partial(parse_from, EXPENSE_TYPES))
    new["booking_id"] = ask_value("booking id (blank = none)",
                                  values.get("booking_id"), parse_id)
    return new


def record_review(cur, receipt_id, decision, values=None, reason=None,
                  duplicate_of=None, note=None):
    """One RECEIPT_REVIEW row. values: the expense fields to record."""
    values = {k: v for k, v in (values or {}).items() if v is not None}
    columns = ["receipt_id", "decision", "not_expense_reason",
               "duplicate_of_receipt_id", "note"] + list(values)
    params = [receipt_id, decision, reason, duplicate_of, note] + list(values.values())
    cur.execute(f"INSERT INTO RECEIPT_REVIEW ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' * len(columns))})", *params)


def resolve_issues(cur, receipt_id, note):
    cur.execute("UPDATE RECEIPT_ISSUE SET resolved_at = SYSDATETIME(), resolution = ? "
                "WHERE receipt_id = ? AND resolved_at IS NULL", note[:200], receipt_id)


def set_status(cur, receipt_id, status):
    cur.execute("UPDATE RECEIPT SET status = ? WHERE receipt_id = ?", status, receipt_id)


def ask_not_expense():
    """(reason, duplicate_of, note) for a not-an-expense decision."""
    key = ask_choice("  reason: [r]eport  [p]ayment confirmation  [d]uplicate  "
                     "[x] personal  [o]ther: ", NOT_EXPENSE_REASONS)
    reason = NOT_EXPENSE_REASONS[key]
    duplicate_of = (ask_value("duplicate of receipt id", None, parse_id)
                    if reason == "duplicate" else None)
    note = input("  note (optional): ").strip() or None
    return reason, duplicate_of, note


def maybe_remember(cur, resolved, chosen, listing_id):
    """Offer a learned rule for this ABN. Sender rules stay manual: forwarded
    receipts come from your own address, which a rule would wrongly match.
    listing_id: the receipt's listing (from listing_for), or None if unknown."""
    if resolved["rule"] or not resolved["abn_valid"] or len(chosen["supplier"]) > 100:
        return
    if ask_choice(f"Remember '{chosen['supplier']}' for ABN {resolved['abn']}? [y/n]: ",
                  "yn") == "n":
        return
    fixed = ask_choice(f"  Also always {chosen['category']} / {chosen['scope']}? [y/n]: ",
                       "yn") == "y"
    category = chosen["category"] if fixed else None
    scope = chosen["scope"] if fixed else None
    if scope == "listing" and listing_id is None:
        print("  The receipt's listing is unknown, so only the name is remembered.")
        category = scope = None
    cur.execute(
        "INSERT INTO MERCHANT_RULE (match_type, match_value, merchant_name, "
        "category, scope, listing_id, source, notes) "
        "VALUES ('abn', ?, ?, ?, ?, ?, 'learned', 'learned from review')",
        resolved["abn"], chosen["supplier"], category, scope,
        listing_id if scope == "listing" else None)
    print("  Rule saved. Add it to config/property.json as well.")


# ------------------------------------------------------------------
# Modes
# ------------------------------------------------------------------
def review_queue(db, cur, categories, rules):
    todo = [r for r in load_receipts(cur) if r["status"] == "needs_review"]
    print(f"{len(todo)} receipts to review")

    for receipt in todo:
        rid = receipt["receipt_id"]
        resolved = resolve(receipt, rules)
        answers = model_answers(cur, rid)
        values = proposal(receipt, resolved, answers)
        show(receipt, resolved, values, open_issue_rows(cur, rid), answers)
        open_file(receipt)

        complete = all(values[k] is not None for k in KEY_FIELDS)
        menu = (("[a]ccept as shown  " if complete else "")
                + "[e]xpense with changes  [n]ot an expense  [s]kip  [q]uit: ")
        choice = ask_choice(menu, "aenqs" if complete else "enqs")
        if choice == "q":
            return
        if choice == "s":
            continue

        try:
            if choice == "a":
                record_review(cur, rid, "expense", values, note="accepted as shown")
                resolve_issues(cur, rid, "review: accepted")
                set_status(cur, rid, "categorised")
                chosen = values
            elif choice == "e":
                chosen = edit(values, categories)
                record_review(cur, rid, "expense", chosen, note="expense with changes")
                resolve_issues(cur, rid, "review: expense with changes")
                set_status(cur, rid, "categorised")
            else:
                reason, duplicate_of, note = ask_not_expense()
                record_review(cur, rid, "not_expense", reason=reason,
                              duplicate_of=duplicate_of, note=note)
                resolve_issues(cur, rid, f"review: not an expense ({reason})")
                set_status(cur, rid, "not_expense")
                chosen = None
            db.commit()
            print("  saved")
        except pyodbc.Error as e:
            db.rollback()
            print(f"  NOT saved: {e}")
            continue

        if chosen is not None and (choice == "e" or chosen["supplier"] != values["supplier"]):
            try:
                maybe_remember(cur, resolved, chosen, listing_for(cur, receipt["recipient"]))
                db.commit()
            except pyodbc.Error as e:
                db.rollback()
                print(f"  rule NOT saved: {e}")


def review_audit(db, cur, categories, show_all, expense_ids=None):
    """Audit pipeline expenses: the given IDs (whatever their state), or else
    unverified ones of AUDIT_THRESHOLD or more (all unverified with show_all)."""
    columns_sql = ("expense_id, receipt_id, expense_date, expense_amount, supplier, "
                   "category, scope, expense_type, booking_id")
    if expense_ids:
        placeholders = ", ".join("?" * len(expense_ids))
        cur.execute(f"SELECT {columns_sql} FROM EXPENSE "
                    f"WHERE expense_id IN ({placeholders}) ORDER BY expense_id",
                    *expense_ids)
    else:
        cur.execute(f"""
            SELECT {columns_sql}
            FROM EXPENSE
            WHERE extracted_by = 'pipeline' AND verified_by_user = 0
              AND (? = 1 OR expense_amount >= ?)
            ORDER BY expense_amount DESC
        """, 1 if show_all else 0, AUDIT_THRESHOLD)
    columns = [d[0] for d in cur.description]
    expenses = [dict(zip(columns, row)) for row in cur.fetchall()]
    receipts = {r["receipt_id"]: r for r in load_receipts(cur)}
    print(f"{len(expenses)} expenses to audit")

    for expense in expenses:
        rid = expense["receipt_id"]
        receipt = receipts.get(rid)
        if receipt is None:
            # Only receipt-based expenses can be rebuilt by create_expenses
            print(f"\nExpense {expense['expense_id']} has no receipt; skipped")
            continue

        values = {k: expense[k] for k in KEY_FIELDS + ("expense_type", "booking_id")}
        print("\n" + "=" * 72)
        print(f"Expense {expense['expense_id']}   from receipt {rid}")
        for key, value in values.items():
            print(f"  {key:16} {value}")
        for a in model_answers(cur, rid):
            print(f"{a['model']}: {a['category']}/{a['scope']}  {a['action_reason']}")
        open_file(receipt)

        choice = ask_choice("[c]onfirm  [e] change  [n]ot an expense  [s]kip  [q]uit: ",
                            "cenqs")
        if choice == "q":
            return
        if choice == "s":
            continue

        try:
            if choice == "c":
                cur.execute("UPDATE EXPENSE SET verified_by_user = 1 WHERE expense_id = ?",
                            expense["expense_id"])
                record_review(cur, rid, "expense", values, note="audit: confirmed")
            elif choice == "e":
                chosen = edit(values, categories)
                record_review(cur, rid, "expense", chosen, note="audit: changed")
                cur.execute("DELETE FROM EXPENSE WHERE expense_id = ?", expense["expense_id"])
                set_status(cur, rid, "categorised")
            else:
                reason, duplicate_of, note = ask_not_expense()
                record_review(cur, rid, "not_expense", reason=reason,
                              duplicate_of=duplicate_of, note=note)
                cur.execute("DELETE FROM EXPENSE WHERE expense_id = ?", expense["expense_id"])
                set_status(cur, rid, "not_expense")
            db.commit()
            print("  saved")
        except pyodbc.Error as e:
            db.rollback()
            print(f"  NOT saved: {e}")


def main():
    args = sys.argv[1:]
    target = "local" if "local" in args else "azure"
    expense_ids = ([int(a) for a in args[args.index("--expense") + 1:] if a.isdigit()]
                   if "--expense" in args else None)

    db = connect_db(target)
    cur = db.cursor()
    try:
        cur.execute("SELECT category_code FROM CATEGORY ORDER BY category_code")
        categories = [row[0] for row in cur.fetchall()]
        if expense_ids or "--audit" in args or "--audit-all" in args:
            review_audit(db, cur, categories, show_all="--audit-all" in args,
                         expense_ids=expense_ids)
        else:
            review_queue(db, cur, categories, load_rules(cur))
    finally:
        db.close()
    print("\nDone. Run create_expenses.py to build expenses for reviewed receipts.")


if __name__ == "__main__":
    main()