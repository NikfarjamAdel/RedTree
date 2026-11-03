"""Load config/property.json into the database: properties, listings,
loans, apportionment rules, merchant rules and booking corrections.

The database is where the pipeline reads property facts from; the config is
where you edit them. Run this after editing the config:

    python3 python/setup_property.py [azure] [--dry-run] [--config config/property.example.json]

Rules:
- Everything is matched by its natural key (property key, listing key, loan
  code, rule period, merchant match), never by database ID, so it works
  whatever IDs a database has assigned.
- A listing key or loan code that already belongs to another property in the
  database is refused, never moved: keys are unique across properties.
- On the first run, an existing listing with the config's title and no key
  yet is adopted (given its key) rather than duplicated.
- Nothing is ever deleted. Rows in the database that the config doesn't
  mention are reported, so they can be copied into the config (e.g.
  merchant rules learned in review).
- Booking corrections are a log where the latest wins: one is added unless
  the latest manual correction for that booking and field already says the
  same; a changed reason is updated in place.
- Apportionment periods: the same valid_from updates a rule in place (a
  correction); a new valid_from starts a new period, and the previous one
  ends where it begins, unless the config gives valid_to.
- The whole config is checked before anything is written; everything is
  written in one transaction, then checked for overlapping rules and shares
  above 100%. --dry-run does all of that, reports, and rolls back.
"""
import json
import sys
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation

from engine_setup import REPO_ROOT, connect_db
from receipt_rules import abn_is_valid

CONFIG = REPO_ROOT / "config" / "property.json"

# Checks run inside the transaction, before committing (also in 30_data_quality.sql)
OVERLAPPING_RULES = """
    SELECT a.listing_id, a.category, a.valid_from, b.valid_from
    FROM APPORTIONMENT_RULE a
    JOIN APPORTIONMENT_RULE b
      ON  b.listing_id = a.listing_id AND b.category = a.category
      AND b.rule_id > a.rule_id
      AND a.valid_from < ISNULL(b.valid_to, '9999-12-31')
      AND b.valid_from < ISNULL(a.valid_to, '9999-12-31')"""

# Shares split a property's bills among that property's listings, so they
# are added up per property
SHARES_OVER_100 = """
    SELECT p.property_key, d.category, d.valid_from, SUM(r.rate)
    FROM (SELECT DISTINCT l.property_id, a.category, a.valid_from
          FROM APPORTIONMENT_RULE a
          JOIN LISTING l ON l.listing_id = a.listing_id) d
    JOIN PROPERTY p ON p.property_id = d.property_id
    JOIN APPORTIONMENT_RULE r
      ON  r.category = d.category AND r.valid_from <= d.valid_from
      AND (r.valid_to IS NULL OR r.valid_to > d.valid_from)
    JOIN LISTING rl ON rl.listing_id = r.listing_id AND rl.property_id = d.property_id
    GROUP BY p.property_key, d.category, d.valid_from
    HAVING SUM(r.rate) > 1"""


# ------------------------------------------------------------------
# Reading and checking the config
# ------------------------------------------------------------------
def as_date(value, where, errors):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        errors.append(f"{where}: '{value}' is not a YYYY-MM-DD date")
        return None


def as_decimal(value, where, errors, low=None, high=None):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        errors.append(f"{where}: '{value}' is not a number")
        return None
    if (low is not None and number < low) or (high is not None and number > high):
        errors.append(f"{where}: {number} is outside {low} to {high}")
    return number


def optional_decimal(item, field, where, errors, low=None, high=None):
    value = item.get(field)
    return None if value is None else as_decimal(value, f"{where}.{field}", errors, low, high)


def load_plan(config, categories):
    """The config as rows ready to write, and a list of every problem found."""
    errors = []
    known = {"properties", "apportionment", "merchant_rules", "booking_corrections"}
    for key in config:
        if key not in known:
            errors.append(f"unknown top-level key '{key}' "
                          f"(expected one of: {', '.join(sorted(known))})")

    plan = {"properties": [], "listings": [], "loans": [], "apportionment": [], "rules": [],
            "corrections": []}
    property_keys, listing_keys, loan_codes = set(), set(), set()

    for i, p in enumerate(config.get("properties") or []):
        where = f"properties[{i}]"
        key = p.get("key")
        if not key:
            errors.append(f"{where}: missing key")
            continue
        if key in property_keys:
            errors.append(f"{where}: duplicate property key '{key}'")
        property_keys.add(key)
        plan["properties"].append({
            "property_key": key,
            "property_name": p.get("name") or key,
            "address": p.get("address"),
            "description": p.get("description"),
            "categorisation_notes": p.get("categorisation_notes"),
        })

        for j, listing in enumerate(p.get("listings") or []):
            lw = f"{where}.listings[{j}]"
            lkey = listing.get("key")
            if not lkey:
                errors.append(f"{lw}: missing key")
                continue
            if lkey in listing_keys:
                errors.append(f"{lw}: duplicate listing key '{lkey}'")
            listing_keys.add(lkey)
            address = listing.get("receipt_address")
            if address and (address != address.lower() or "@" not in address):
                errors.append(f"{lw}: receipt_address must be a lower-case email address")
            plan["listings"].append({
                "listing_key": lkey,
                "property_key": key,
                "listing_title": listing.get("title") or lkey,
                "listing_address": p.get("address") or "",
                "listed_from": as_date(listing.get("listed_from"), f"{lw}.listed_from", errors),
                "airbnb_name": listing.get("airbnb_name"),
                "receipt_address": address,
                "default_for_receipts": bool(listing.get("default_for_receipts", False)),
                "ical_url_setting": listing.get("ical_url_setting"),
                "cleaning_cost_per_turnover": optional_decimal(
                    listing, "cleaning_cost_per_turnover", lw, errors, low=Decimal(0)),
                "loan_interest_share": optional_decimal(
                    listing, "loan_interest_share", lw, errors, Decimal(0), Decimal(1)),
            })

        for j, loan in enumerate(p.get("loans") or []):
            lw = f"{where}.loans[{j}]"
            code = loan.get("code")
            if not code:
                errors.append(f"{lw}: missing code")
                continue
            if code in loan_codes:
                errors.append(f"{lw}: duplicate loan code '{code}'")
            loan_codes.add(code)
            active_from = as_date(loan.get("active_from"), f"{lw}.active_from", errors)
            active_to = (as_date(loan["active_to"], f"{lw}.active_to", errors)
                         if loan.get("active_to") else None)
            for name, d in (("active_from", active_from), ("active_to", active_to)):
                if d and d.day != 1:
                    errors.append(f"{lw}.{name}: must be the first day of a month")
            charge_day = loan.get("charge_day", 19)
            if not isinstance(charge_day, int) or not 1 <= charge_day <= 28:
                errors.append(f"{lw}.charge_day: must be a whole number from 1 to 28")
            plan["loans"].append({
                "loan_code": code, "property_key": key,
                "description": loan.get("description") or code,
                "active_from": active_from, "active_to": active_to,
                "charge_day": charge_day,
            })

    if not plan["properties"]:
        errors.append("the config has no properties")

    # One default listing in the whole config: the plain receipt address
    # belongs to it, and through it to its property.
    defaults = [listing["listing_key"] for listing in plan["listings"] if listing["default_for_receipts"]]
    if len(defaults) > 1:
        errors.append(f"more than one listing has default_for_receipts: {', '.join(defaults)}")

    # Apportionment: group into periods per listing and category; each period
    # ends where the next begins, unless the config says otherwise.
    periods = {}
    for i, a in enumerate(config.get("apportionment") or []):
        w = f"apportionment[{i}]"
        if a.get("listing") not in listing_keys:
            errors.append(f"{w}: unknown listing '{a.get('listing')}'")
        if a.get("category") not in categories:
            errors.append(f"{w}: unknown category '{a.get('category')}'")
        row = {
            "listing_key": a.get("listing"),
            "category": a.get("category"),
            "rate": as_decimal(a.get("rate"), f"{w}.rate", errors, Decimal(0), Decimal(1)),
            "valid_from": as_date(a.get("valid_from"), f"{w}.valid_from", errors),
            "valid_to": as_date(a["valid_to"], f"{w}.valid_to", errors) if a.get("valid_to") else None,
            "basis": a.get("basis"),
        }
        periods.setdefault((row["listing_key"], row["category"]), []).append((row, w))

    for (listing, category), rows in periods.items():
        rows.sort(key=lambda item: item[0]["valid_from"] or date.min)
        starts = [row["valid_from"] for row, _ in rows]
        if len(set(starts)) != len(starts):
            errors.append(f"apportionment: {listing}/{category} has two periods with the same valid_from")
        for (row, w), following in zip(rows, rows[1:] + [(None, None)]):
            next_start = following[0]["valid_from"] if following[0] else None
            if row["valid_to"] is None:
                row["valid_to"] = next_start
            elif next_start and row["valid_to"] > next_start:
                errors.append(f"{w}: valid_to runs into the next period")
            if row["valid_to"] and row["valid_from"] and row["valid_to"] <= row["valid_from"]:
                errors.append(f"{w}: valid_to must be after valid_from")
            plan["apportionment"].append(row)

    # Merchant rules
    seen = set()
    for i, r in enumerate(config.get("merchant_rules") or []):
        w = f"merchant_rules[{i}]"
        match_type, value = r.get("match_type"), r.get("match_value")
        if match_type == "abn":
            if not (value and abn_is_valid(value)):
                errors.append(f"{w}: '{value}' is not a valid ABN")
        elif match_type == "sender":
            if not value or value != value.lower() or "@" not in value:
                errors.append(f"{w}: sender must be a lower-case email address")
        else:
            errors.append(f"{w}: match_type must be 'abn' or 'sender'")
        if (match_type, value) in seen:
            errors.append(f"{w}: duplicate rule for {match_type} {value}")
        seen.add((match_type, value))

        category, scope, listing = r.get("category"), r.get("scope"), r.get("listing")
        if category is not None and category not in categories:
            errors.append(f"{w}: unknown category '{category}'")
        if scope not in (None, "listing", "property"):
            errors.append(f"{w}: scope must be 'listing', 'property' or null")
        if scope == "listing" and listing not in listing_keys:
            errors.append(f"{w}: listing scope needs a known listing")
        if scope != "listing" and listing:
            errors.append(f"{w}: a listing is only allowed with listing scope")
        if r.get("date_order") not in (None, "DMY", "MDY"):
            errors.append(f"{w}: date_order must be 'DMY', 'MDY' or null")
        plan["rules"].append({
            "match_type": match_type, "match_value": value,
            "merchant_name": r.get("merchant_name") or value,
            "category": category, "scope": scope, "listing_key": listing,
            "date_order": r.get("date_order"), "notes": r.get("notes"),
        })

    # Booking corrections: dates the exports get wrong, applied by 54
    corrected = set()
    for i, c in enumerate(config.get("booking_corrections") or []):
        w = f"booking_corrections[{i}]"
        code, field, reason = c.get("confirmation_code"), c.get("field"), c.get("reason")
        if not code or len(code) > 20:
            errors.append(f"{w}: confirmation_code must be 1 to 20 characters")
        if field not in ("check_in", "check_out"):
            errors.append(f"{w}: field must be 'check_in' or 'check_out'")
        if not reason or len(reason) > 300:
            errors.append(f"{w}: reason must be 1 to 300 characters")
        if (code, field) in corrected:
            errors.append(f"{w}: a second correction for {code} {field} (keep only the latest)")
        corrected.add((code, field))
        plan["corrections"].append({
            "confirmation_code": code, "field": field, "reason": reason,
            "corrected_value": as_date(c.get("corrected_value"), f"{w}.corrected_value", errors),
        })

    return plan, errors

def ownership_errors(plan, listing_owners, loan_owners):
    """Listing keys and loan codes the database already has under another
    property. The owners are {key or code: property_key} from the database."""
    errors = []
    for listing in plan["listings"]:
        owner = listing_owners.get(listing["listing_key"])
        if owner is not None and owner != listing["property_key"]:
            errors.append(f"listing key '{listing['listing_key']}' belongs to property '{owner}' "
                          f"in the database; choose another key")
    for loan in plan["loans"]:
        owner = loan_owners.get(loan["loan_code"])
        if owner is not None and owner != loan["property_key"]:
            errors.append(f"loan code '{loan['loan_code']}' belongs to property '{owner}' "
                          f"in the database; choose another code")
    return errors


def owners(cur, sql):
    cur.execute(sql)
    return {key: property_key for key, property_key in cur.fetchall()}


# ------------------------------------------------------------------
# Writing
# ------------------------------------------------------------------
def same(db_value, new_value):
    """Equal, treating 0.4 and Decimal('0.4000') as the same number."""
    if db_value is None or new_value is None:
        return db_value is None and new_value is None
    if isinstance(db_value, Decimal) or isinstance(new_value, Decimal):
        return Decimal(str(db_value)) == Decimal(str(new_value))
    return db_value == new_value


def upsert(cur, table, keys, values, insert_only=None):
    """Insert the row, or update it if any value differs.

    keys, values and insert_only are {column: value}; insert_only columns
    are set only when the row is new. Returns 'added', 'updated' or
    'unchanged'. Table and column names come from this file, never from
    the config.
    """
    where = " AND ".join(f"{c} = ?" for c in keys)
    columns = list(values)
    cur.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE {where}", *keys.values())
    row = cur.fetchone()
    if row is None:
        extra = insert_only or {}
        all_columns = list(keys) + columns + list(extra)
        cur.execute(f"INSERT INTO {table} ({', '.join(all_columns)}) "
                    f"VALUES ({', '.join('?' * len(all_columns))})",
                    *keys.values(), *values.values(), *extra.values())
        return "added"
    if all(same(old, new) for old, new in zip(row, values.values())):
        return "unchanged"
    cur.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in columns)} WHERE {where}",
                *values.values(), *keys.values())
    return "updated"


def lookup(cur, sql, value):
    cur.execute(sql, value)
    row = cur.fetchone()
    return row[0] if row else None


def apply(cur, plan):
    """Write the plan; return a Counter of outcomes per table."""
    counts = Counter()

    for p in plan["properties"]:
        key = p["property_key"]
        result = upsert(cur, "PROPERTY", {"property_key": key},
                        {k: v for k, v in p.items() if k != "property_key"})
        counts[f"PROPERTY {result}"] += 1

    property_ids = {p["property_key"]: lookup(cur, "SELECT property_id FROM PROPERTY "
                                                    "WHERE property_key = ?", p["property_key"])
                    for p in plan["properties"]}

    # One default listing overall (unique index): clear any other default
    # before the config's own is written
    default_key = next((listing["listing_key"] for listing in plan["listings"]
                        if listing["default_for_receipts"]), "")
    cur.execute("UPDATE LISTING SET default_for_receipts = 0 "
                "WHERE default_for_receipts = 1 AND ISNULL(listing_key, '') <> ?",
                default_key)

    for listing in plan["listings"]:
        key = listing["listing_key"]
        # First run: adopt the existing listing with this title, if it has no key yet
        if lookup(cur, "SELECT listing_id FROM LISTING WHERE listing_key = ?", key) is None:
            cur.execute("UPDATE LISTING SET listing_key = ? "
                        "WHERE listing_title = ? AND listing_key IS NULL", key, listing["listing_title"])
            if cur.rowcount:
                counts["LISTING adopted"] += 1
        values = {k: v for k, v in listing.items() if k not in ("listing_key", "property_key")}
        values["property_id"] = property_ids[listing["property_key"]]
        result = upsert(cur, "LISTING", {"listing_key": key}, values,
                        insert_only={"listing_status": "active"})
        counts[f"LISTING {result}"] += 1

    listing_ids = {listing["listing_key"]: lookup(cur, "SELECT listing_id FROM LISTING "
                                                 "WHERE listing_key = ?", listing["listing_key"])
                   for listing in plan["listings"]}

    for loan in plan["loans"]:
        result = upsert(cur, "LOAN", {"loan_code": loan["loan_code"]}, {
            "description": loan["description"],
            "active_from": loan["active_from"],
            "active_to": loan["active_to"],
            "charge_day": loan["charge_day"],
            "property_id": property_ids[loan["property_key"]],
        })
        counts[f"LOAN {result}"] += 1

    for a in plan["apportionment"]:
        result = upsert(cur, "APPORTIONMENT_RULE",
                        {"listing_id": listing_ids[a["listing_key"]],
                         "category": a["category"], "valid_from": a["valid_from"]},
                        {"rate": a["rate"], "valid_to": a["valid_to"], "basis": a["basis"]})
        counts[f"APPORTIONMENT_RULE {result}"] += 1

    for r in plan["rules"]:
        result = upsert(cur, "MERCHANT_RULE",
                        {"match_type": r["match_type"], "match_value": r["match_value"]},
                        {"merchant_name": r["merchant_name"], "category": r["category"],
                         "scope": r["scope"],
                         "listing_id": listing_ids.get(r["listing_key"]) if r["listing_key"] else None,
                         "date_order": r["date_order"], "notes": r["notes"]})
        counts[f"MERCHANT_RULE {result}"] += 1

    for c in plan["corrections"]:
        counts[f"BOOKING_CORRECTION {apply_correction(cur, c)}"] += 1

    return counts


def apply_correction(cur, c):
    """Add a manual correction unless the latest one for this booking and
    field already says the same; update its reason if that changed."""
    cur.execute("SELECT TOP 1 correction_id, corrected_value, reason FROM BOOKING_CORRECTION "
                "WHERE confirmation_code = ? AND field = ? AND source = 'manual' "
                "ORDER BY created_at DESC, correction_id DESC",
                c["confirmation_code"], c["field"])
    row = cur.fetchone()
    if row and same(row[1], c["corrected_value"]):
        if row[2] == c["reason"]:
            return "unchanged"
        cur.execute("UPDATE BOOKING_CORRECTION SET reason = ? WHERE correction_id = ?",
                    c["reason"], row[0])
        return "updated"
    cur.execute("INSERT INTO BOOKING_CORRECTION (confirmation_code, field, corrected_value, "
                "reason, source) VALUES (?, ?, ?, ?, 'manual')",
                c["confirmation_code"], c["field"], c["corrected_value"], c["reason"])
    return "added"


def check(cur):
    """Refuse to commit overlapping rules or shares above 100%."""
    problems = []
    for name, sql in (("overlapping apportionment periods", OVERLAPPING_RULES),
                      ("shares above 100% within a property", SHARES_OVER_100)):
        cur.execute(sql)
        rows = cur.fetchall()
        if rows:
            problems.append(f"{name}: {[tuple(r) for r in rows[:5]]}")
    if problems:
        raise RuntimeError("Not saved: " + "; ".join(problems))


def report_unmanaged(cur, plan):
    """Rows the config doesn't mention: never deleted, just listed."""
    configured_rules = {(r["match_type"], r["match_value"]) for r in plan["rules"]}
    cur.execute("SELECT match_type, match_value, merchant_name, source FROM MERCHANT_RULE")
    extra = [r for r in cur.fetchall() if (r[0], r[1]) not in configured_rules]
    for match_type, value, name, source in extra:
        print(f"  not in config: merchant rule {match_type} {value} ({name}, {source})")

    configured_loans = {loan["loan_code"] for loan in plan["loans"]}
    cur.execute("SELECT loan_code FROM LOAN")
    for (code,) in cur.fetchall():
        if code not in configured_loans:
            print(f"  not in config: loan {code}")

    configured_periods = {(a["listing_key"], a["category"], a["valid_from"])
                          for a in plan["apportionment"]}
    cur.execute("SELECT l.listing_key, r.category, r.valid_from FROM APPORTIONMENT_RULE r "
                "JOIN LISTING l ON l.listing_id = r.listing_id WHERE l.listing_key IS NOT NULL")
    for key, category, valid_from in cur.fetchall():
        if (key, category, valid_from) not in configured_periods:
            print(f"  not in config: apportionment {key}/{category} from {valid_from}")

    configured_corrections = {(c["confirmation_code"], c["field"], c["corrected_value"])
                              for c in plan["corrections"]}
    cur.execute("SELECT confirmation_code, field, corrected_value FROM BOOKING_CORRECTION "
                "WHERE source = 'manual'")
    for code, field, value in cur.fetchall():
        if (code, field, value) not in configured_corrections:
            print(f"  not in config: manual correction {code} {field} {value}")

    if not extra:
        print("  every merchant rule in the database is in the config")


def main():
    args = sys.argv[1:]
    target = "azure" if "azure" in args else "local"
    dry_run = "--dry-run" in args
    config_path = (REPO_ROOT / args[args.index("--config") + 1]
                   if "--config" in args else CONFIG)
    config = json.loads(config_path.read_text())
    print(f"Config: {config_path.name}")

    db = connect_db(target)
    cur = db.cursor()
    try:
        cur.execute("SELECT category_code FROM CATEGORY")
        categories = {row[0] for row in cur.fetchall()}

        plan, errors = load_plan(config, categories)
        if not errors:
            errors = ownership_errors(
                plan,
                owners(cur, "SELECT l.listing_key, p.property_key FROM LISTING l "
                            "JOIN PROPERTY p ON p.property_id = l.property_id "
                            "WHERE l.listing_key IS NOT NULL"),
                owners(cur, "SELECT l.loan_code, p.property_key FROM LOAN l "
                            "JOIN PROPERTY p ON p.property_id = l.property_id"))
        if errors:
            print("Config not loaded. Fix these first:")
            for error in errors:
                print(f"  - {error}")
            sys.exit(1)

        counts = apply(cur, plan)
        check(cur)
        for outcome, n in sorted(counts.items()):
            print(f"{outcome}: {n}")
        print("Rows the config doesn't mention (never deleted):")
        report_unmanaged(cur, plan)

        if dry_run:
            db.rollback()
            print("Dry run: nothing written.")
        else:
            db.commit()
            print("Saved.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

if __name__ == "__main__":
    main()