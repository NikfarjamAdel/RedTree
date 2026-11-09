"""Everything the dashboard shows, read from the database in one pass.

Read-only: SELECTs only. Money comes from the business-logic views
(vw_monthly_report, vw_expense_monthly, vw_expense_spread, vw_available_night),
so the dashboard's figures are the report's figures, never a second
calculation of them. The one figure the views don't carry, the platform fee,
is spread over a booking's nights exactly as vw_monthly_profit spreads the
payout.

The data checks are the build's own (30_data_quality.sql, 63_star_checks.sql):
each batch returns no rows when the data is right.
"""
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sql_runner import split_batches

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
CHECK_FILES = ("30_data_quality.sql", "63_star_checks.sql")

# Plain-language names for the build's checks; a check without one here
# falls back to the first sentence of its comment in the SQL file.
CHECK_LABELS = {
    ("30_data_quality.sql", 1): "Stock levels never go below zero",
    ("30_data_quality.sql", 2): "No two stays overlap on a listing",
    ("30_data_quality.sql", 3): "Cost-sharing rules never overlap in time",
    ("30_data_quality.sql", 4): "Shared costs never add up to more than 100%",
    ("30_data_quality.sql", 5): "Every shared cost has a sharing rule",
    ("63_star_checks.sql", 1): "Analysis tables agree with the expense report",
}

THIS_MONTH = "DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1)"

QUERIES = {
    "listings": """
        SELECT l.listing_id, l.listing_title, l.listing_status, l.listed_from,
               l.cleaning_cost_per_turnover, l.loan_interest_share,
               p.property_id, p.property_name, p.address
        FROM LISTING l
        JOIN PROPERTY p ON p.property_id = l.property_id
        ORDER BY l.listing_id""",

    "months": """
        SELECT listing_id, month_start, nights_sold, revenue, expenses, cleaning,
               operating_profit, loan_interest, profit_after_interest,
               interest_missing, revenue_complete, unverified_expenses
        FROM vw_monthly_report
        ORDER BY listing_id, month_start""",

    # The platform fee, placed in months the way vw_monthly_profit places the
    # payout: spread evenly over the booking's nights, or in the month paid
    # when the booking has no nights.
    "fees": """
        WITH booking_fee AS (
            SELECT booking_id, SUM(platform_fee) AS fee, MIN(payout_date) AS payout_date
            FROM PAYOUT
            GROUP BY booking_id
        ),
        booking_nights AS (
            SELECT booking_id, COUNT(*) AS nights
            FROM STAY_NIGHT
            GROUP BY booking_id
        ),
        spread AS (
            SELECT s.listing_id,
                   DATEFROMPARTS(YEAR(s.night_date), MONTH(s.night_date), 1) AS month_start,
                   CAST(bf.fee AS DECIMAL(18,6)) / n.nights                   AS fee
            FROM STAY_NIGHT s
            JOIN booking_fee    bf ON bf.booking_id = s.booking_id
            JOIN booking_nights n  ON n.booking_id  = s.booking_id
            UNION ALL
            SELECT b.listing_id,
                   DATEFROMPARTS(YEAR(bf.payout_date), MONTH(bf.payout_date), 1),
                   bf.fee
            FROM booking_fee bf
            JOIN BOOKING b ON b.booking_id = bf.booking_id
            WHERE NOT EXISTS (SELECT 1 FROM STAY_NIGHT s WHERE s.booking_id = bf.booking_id)
        )
        SELECT listing_id, month_start, CAST(SUM(fee) AS DECIMAL(10,2)) AS platform_fee
        FROM spread
        GROUP BY listing_id, month_start""",

    "available": f"""
        SELECT listing_id,
               DATEFROMPARTS(year_number, month_number, 1) AS month_start,
               COUNT(*)                                     AS available_nights
        FROM vw_available_night
        WHERE date_key < DATEADD(MONTH, 1, {THIS_MONTH})
        GROUP BY listing_id, year_number, month_number""",

    "bookings": """
        SELECT b.booking_id, b.listing_id, b.confirmation_code,
               g.display_name                    AS guest,
               CAST(b.booked_on AS DATE)         AS booked_on,
               b.check_in, b.check_out, b.nightly_rate, b.number_of_guests, b.source,
               p.gross_amount, p.platform_fee, p.net_amount, p.payout_date,
               c.cleaning_cost,
               CASE WHEN EXISTS (SELECT 1 FROM BOOKING_HISTORY h
                                 WHERE h.booking_id = b.booking_id
                                   AND h.action_type = 'cancelled')
                    THEN 1 ELSE 0 END            AS is_cancelled
        FROM BOOKING b
        JOIN GUEST g ON g.guest_id = b.guest_id
        LEFT JOIN (SELECT booking_id, SUM(gross_amount) AS gross_amount,
                          SUM(platform_fee) AS platform_fee, SUM(net_amount) AS net_amount,
                          MIN(payout_date) AS payout_date
                   FROM PAYOUT GROUP BY booking_id) p ON p.booking_id = b.booking_id
        LEFT JOIN (SELECT booking_id, SUM(cleaning_cost) AS cleaning_cost
                   FROM CLEANING GROUP BY booking_id) c ON c.booking_id = b.booking_id
        ORDER BY b.listing_id, b.check_in""",

    "nights": """
        SELECT listing_id, booking_id, night_date, rate_night
        FROM STAY_NIGHT
        ORDER BY listing_id, night_date""",

    "blocked": """
        SELECT listing_id, blocked_start_date, blocked_end_date, blocked_reason
        FROM BLOCKED_PERIOD""",

    # Each listing's share of each cost category, by month (where shares are
    # calculated), up to this month: capital spreads run years ahead.
    "expense_shares": f"""
        SELECT listing_id, month_start, category, expense_type,
               SUM(amount)                                                AS amount,
               SUM(CASE WHEN verified_by_user = 0 THEN amount ELSE 0 END) AS unverified
        FROM vw_expense_monthly
        WHERE month_start <= {THIS_MONTH}
        GROUP BY listing_id, month_start, category, expense_type""",

    # The full bills behind those shares, before splitting: a listing's own
    # bills, and every bill of its property.
    "expense_bills": f"""
        SELECT l.listing_id, s.month_start, s.category, s.scope, SUM(s.amount) AS amount
        FROM vw_expense_spread s
        JOIN LISTING l
          ON (s.scope = 'listing'  AND s.listing_id  = l.listing_id)
          OR (s.scope = 'property' AND s.property_id = l.property_id)
        WHERE s.month_start <= {THIS_MONTH}
        GROUP BY l.listing_id, s.month_start, s.category, s.scope""",

    "categories": """
        SELECT category_code, description, default_expense_type,
               capital_life_months, spread_months
        FROM CATEGORY""",

    "apportionment": """
        SELECT listing_id, category, rate, valid_from, valid_to, basis
        FROM APPORTIONMENT_RULE""",

    "freshness": """
        SELECT (SELECT MAX(export_date) FROM LOAD_LOG WHERE file_kind = 'paid') AS latest_export,
               (SELECT MAX(loaded_at)   FROM LOAD_LOG)                          AS last_loaded,
               (SELECT MAX(month_start) FROM LOAN_INTEREST)                     AS latest_interest,
               (SELECT COUNT(*) FROM RECEIPT WHERE status = 'needs_review')     AS receipts_to_review,
               CAST(GETDATE() AS DATE)                                          AS today""",
}


def plain(value):
    """A database value as something JSON can carry."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def fetch(cur, sql):
    cur.execute(sql)
    names = [d[0] for d in cur.description]
    return [{n: plain(v) for n, v in zip(names, row)} for row in cur.fetchall()]


def check_title(file_name, number, batch):
    """The check's plain-language name, else the first sentence of its comment."""
    if (file_name, number) in CHECK_LABELS:
        return CHECK_LABELS[(file_name, number)]
    lines = batch.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"\s*--\s*CHECK\s*\d+:\s*(.*)", line)
        if not m:
            continue
        words = [m.group(1).strip()]
        for more in lines[i + 1:]:
            text = more.strip()
            if not text.startswith("--") or text == "--":
                break
            words.append(text.lstrip("-").strip())
        title = re.split(r"(?<=\.)\s|\(expect", " ".join(words))[0]
        return title.strip().rstrip(".")
    return f"{file_name} check {number}"


def run_checks(cur):
    results = []
    for file_name in CHECK_FILES:
        batches = split_batches((SQL_DIR / file_name).read_text())
        for number, batch in enumerate(batches, start=1):
            cur.execute(batch)
            failing = len(cur.fetchall())
            results.append({"file": file_name, "number": number,
                            "title": check_title(file_name, number, batch),
                            "failing_rows": failing})
    return results


def dashboard_payload(db):
    """Everything the page needs, as one JSON-ready dict."""
    cur = db.cursor()
    try:
        payload = {name: fetch(cur, sql) for name, sql in QUERIES.items()}
        payload["freshness"] = payload["freshness"][0]
        payload["checks"] = run_checks(cur)
    finally:
        cur.close()
    payload["generated_at"] = datetime.now().isoformat(timespec="seconds")
    return payload
