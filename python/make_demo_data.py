"""Demo data for the example listing: one year, April 2025 to March 2026,
from a fixed seed, so every build produces exactly the same data.

  bookings       Airbnb earnings exports (CSV), one per month, in the real
                 export's columns, for load_airbnb.py --from-dir to load
                 through the real pipeline
  expenses       manual expenses, inserted directly (every category)
  loan interest  one LOAN_INTEREST row a month for the example loan

Cleaning is not generated: 52_impute_cleaning.sql imputes it.

The profile below is loosely shaped like a real listing and blurred, so no
figure is a real one. Every name, supplier and confirmation code is invented.
The listing, property and loan come from config/property.example.json.

Usage (build_db.py --demo runs all of this):
    python3 python/make_demo_data.py --out <folder>    # write the CSVs only
"""
import csv
import io
import json
import random
import string
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = REPO_ROOT / "config" / "property.example.json"

SEED = 20250401
START = date(2025, 4, 1)        # first night
END = date(2026, 4, 1)          # first night NOT in the demo year

# ------------------------------------------------------------------
# The profile
# ------------------------------------------------------------------
# month -> (occupancy, average nightly rate)
MONTHS = {
    4: (0.55, 180), 5: (0.70, 175), 6: (0.55, 210), 7: (0.70, 225),
    8: (0.70, 225), 9: (0.55, 265), 10: (0.50, 280), 11: (0.60, 265),
    12: (0.65, 290), 1: (0.75, 300), 2: (0.65, 280), 3: (0.60, 250),
}
WEEKEND_PREMIUM = 0.35                       # Friday and Saturday nights
STAY_LENGTHS = [((1, 1), 0.40), ((2, 2), 0.35), ((3, 3), 0.15),
                ((4, 6), 0.05), ((7, 10), 0.05)]
LEAD_DAYS = [((0, 2), 0.25), ((3, 7), 0.25), ((8, 30), 0.35), ((31, 90), 0.15)]
CHECK_IN_WEEKDAY = [0.10, 0.10, 0.10, 0.15, 0.20, 0.15, 0.20]     # Monday first
CLEANING_FEE = Decimal("65.00")              # charged to the guest
SERVICE_FEE_SHARE = Decimal("0.10")          # Airbnb's fee, share of gross
MONTHLY_INTEREST = Decimal(3010)             # a typical month's loan interest

# The real export's columns, in order (load_airbnb.COLUMNS)
EXPORT_COLUMNS = [
    "Date", "Arriving by date", "Type", "Confirmation Code", "Booking date",
    "Start date", "End date", "Nights", "Guest", "Listing", "Details",
    "Reference code", "Currency", "Amount", "Paid out", "Service fee",
    "Fast pay fee", "Cleaning fee", "Gross earnings", "Airbnb remitted tax",
    "Earnings year",
]

FIRST_NAMES = ["Ava", "Bodhi", "Clementine", "Dashiell", "Elowen", "Fergus",
               "Ginevra", "Hamish", "Imogen", "Jasper", "Kirra", "Lachlan",
               "Marigold", "Nico", "Odette", "Percival", "Quinlan", "Rosalind",
               "Seren", "Tobias", "Ulla", "Vaughan", "Wren", "Xanthe", "Yusuf", "Zinnia"]
FAMILY_NAMES = ["Ashgrove", "Brightwater", "Calloway", "Dunmore", "Everleigh",
                "Fairweather", "Glenrowan", "Hollowbrook", "Ironbark", "Juniper",
                "Kestrel", "Larkspur", "Merriwether", "Northcote", "Oakhurst",
                "Pemberton", "Quenby", "Redwater", "Stonebridge", "Thistlewood"]

TWO_CENTS = Decimal("0.01")


def cents(x):
    return Decimal(str(x)).quantize(TWO_CENTS, ROUND_HALF_UP)


def mdy(d):
    return d.strftime("%m/%d/%Y")


def weighted(rng, choices):
    """Pick from [(value, weight), ...]."""
    values, weights = zip(*choices)
    return rng.choices(values, weights)[0]


def example_config():
    """The demo's listing, property and loan, from the example config."""
    prop = json.loads(EXAMPLE_CONFIG.read_text())["properties"][0]
    listing, loan = prop["listings"][0], prop["loans"][0]
    return {"property_key": prop["key"], "listing_key": listing["key"],
            "airbnb_name": listing["airbnb_name"], "loan_code": loan["code"],
            "charge_day": loan.get("charge_day", 19)}


# ------------------------------------------------------------------
# Bookings
# ------------------------------------------------------------------
@dataclass
class Booking:
    code: str
    guest: str
    booked_on: date
    check_in: date
    check_out: date
    nightly_rate: Decimal        # what 50_transform.sql will derive
    payout_date: date
    arriving_by: date
    reference: str

    @property
    def nights(self):
        return (self.check_out - self.check_in).days

    @property
    def gross(self):             # Amount + Service fee
        return self.nightly_rate * self.nights + CLEANING_FEE

    @property
    def service_fee(self):
        return cents(self.gross * SERVICE_FEE_SHARE)

    @property
    def amount(self):            # what the host is paid
        return self.gross - self.service_fee


def make_bookings(seed=SEED):
    """Back-to-back stays and gaps through the year, never overlapping.

    The gap after a stay is drawn so the month's occupancy comes out near
    its target: shorter when the month so far is below it, longer when
    above. The check-in day is then nudged towards the busier weekdays.
    """
    rng = random.Random(seed)
    mean_stay = sum((lo + hi) / 2 * w for (lo, hi), w in STAY_LENGTHS)
    busiest = max(CHECK_IN_WEEKDAY)
    codes = set()
    bookings = []
    sold = set()                 # nights taken so far
    cursor = START
    while True:
        occupancy = MONTHS[cursor.month][0]
        # 0.6: the weekday nudge below lengthens gaps by about a day on average
        mean_gap = 0.6 * mean_stay * (1 - occupancy) / occupancy
        month_start = cursor.replace(day=1)
        elapsed = (cursor - month_start).days
        if elapsed >= 3:
            so_far = sum(month_start + timedelta(days=i) in sold for i in range(elapsed)) / elapsed
            mean_gap *= min(4.0, max(0.25, (so_far / occupancy) ** 3))
        check_in = cursor + timedelta(days=round(rng.uniform(0, 2 * mean_gap)))
        while rng.random() > CHECK_IN_WEEKDAY[check_in.weekday()] / busiest:
            check_in += timedelta(days=1)
        if check_in >= END:
            break
        nights = rng.randint(*weighted(rng, STAY_LENGTHS))
        check_out = min(check_in + timedelta(days=nights), END)

        prices = []
        for i in range((check_out - check_in).days):
            night = check_in + timedelta(days=i)
            average = MONTHS[night.month][1]
            # weekday price, so that a typical week averages the month's rate
            price = average / (1 + WEEKEND_PREMIUM * 2 / 7)
            if night.weekday() in (4, 5):
                price *= 1 + WEEKEND_PREMIUM
            prices.append(price * rng.uniform(0.92, 1.08))

        code = "HMDEMO" + "".join(rng.choices(string.ascii_uppercase + string.digits, k=4))
        while code in codes:
            code = "HMDEMO" + "".join(rng.choices(string.ascii_uppercase + string.digits, k=4))
        codes.add(code)
        payout_date = check_in + timedelta(days=weighted(rng, [(1, 6), (2, 2), (3, 1), (4, 1)]))
        bookings.append(Booking(
            code=code,
            guest=f"{rng.choice(FIRST_NAMES)} {rng.choice(FAMILY_NAMES)}",
            booked_on=check_in - timedelta(days=rng.randint(*weighted(rng, LEAD_DAYS))),
            check_in=check_in,
            check_out=check_out,
            nightly_rate=cents(sum(prices) / len(prices)),
            payout_date=payout_date,
            arriving_by=payout_date + timedelta(days=rng.choice([5, 7, 8])),
            reference="DEMO" + "".join(rng.choices(string.ascii_uppercase + string.digits, k=20)),
        ))
        sold.update(check_in + timedelta(days=i) for i in range((check_out - check_in).days))
        cursor = check_out
    return bookings


def export_rows(booking, listing_name):
    """The two rows Airbnb writes per paid booking: the payout, then the reservation."""
    empty = dict.fromkeys(EXPORT_COLUMNS, "")
    payout = {**empty,
              "Date": mdy(booking.payout_date), "Arriving by date": mdy(booking.arriving_by),
              "Type": "Payout", "Details": "Transfer to Demo Bank, Account 0000 (AUD)",
              "Reference code": booking.reference, "Currency": "AUD",
              "Paid out": f"{booking.amount:.2f}"}
    reservation = {**empty,
                   "Date": mdy(booking.payout_date), "Type": "Reservation",
                   "Confirmation Code": booking.code, "Booking date": mdy(booking.booked_on),
                   "Start date": mdy(booking.check_in), "End date": mdy(booking.check_out),
                   "Nights": str(booking.nights), "Guest": booking.guest,
                   "Listing": listing_name, "Currency": "AUD",
                   "Amount": f"{booking.amount:.2f}", "Service fee": f"{booking.service_fee:.2f}",
                   "Cleaning fee": f"{CLEANING_FEE:.2f}", "Gross earnings": f"{booking.gross:.2f}",
                   "Airbnb remitted tax": "0.00", "Earnings year": str(booking.payout_date.year)}
    return [payout, reservation]


def make_exports(bookings, listing_name):
    """{file name: CSV text}: one earnings export per month of payouts,
    dated the first of the next month, newest rows first like Airbnb's."""
    by_month = {}
    for b in bookings:
        by_month.setdefault((b.payout_date.year, b.payout_date.month), []).append(b)
    exports = {}
    for (year, month), month_bookings in sorted(by_month.items()):
        export_date = date(year + month // 12, month % 12 + 1, 1)
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=EXPORT_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for b in sorted(month_bookings, key=lambda b: (b.payout_date, b.code), reverse=True):
            writer.writerows(export_rows(b, listing_name))
        exports[f"airbnb_{export_date.isoformat()}.csv"] = out.getvalue()
    return exports


def write_exports(folder, seed=SEED):
    """Write the month's earnings exports into folder; return their names."""
    exports = make_exports(make_bookings(seed), example_config()["airbnb_name"])
    for name, text in exports.items():
        (Path(folder) / name).write_text(text)
    return sorted(exports)


# ------------------------------------------------------------------
# Expenses and loan interest
# ------------------------------------------------------------------
EXAMPLE_PLUMBING_ABN = "51824753556"      # the example merchant rule's ABN


def make_expenses(seed=SEED):
    """Every expense of the demo year, as dicts, in date order."""
    rng = random.Random(seed + 1)

    def some_day(start=START, end=END - timedelta(days=1)):
        return start + timedelta(days=rng.randint(0, (end - start).days))

    rows = []

    def add(day, amount, category, scope, expense_type, supplier, abn=None, invoice=None):
        rows.append({"expense_date": day, "expense_amount": cents(amount),
                     "category": category, "scope": scope, "expense_type": expense_type,
                     "supplier": supplier, "supplier_abn": abn, "invoice_number": invoice})

    # Regular bills for the whole property, shared by apportionment
    for i in range(12):
        month = date(2025 + (3 + i) // 12, (3 + i) % 12 + 1, 12)
        add(month, 105, "internet", "property", "periodic", "Gumleaf Broadband")
    # Electricity: billed quarterly in arrears, in the quarter's last month
    # (spread_anchor 'end': a bill covers its own month and the two before)
    for day, amount in ((date(2025, 6, 27), 1020), (date(2025, 9, 26), 1245),
                        (date(2025, 12, 29), 850), (date(2026, 3, 27), 1065)):
        add(day, amount, "electricity", "property", "periodic", "Wattlegrid Energy")
    for month in (date(2025, 4, 15), date(2025, 7, 15), date(2025, 10, 15), date(2026, 1, 15)):
        add(month, 745, "council_rates", "property", "periodic", "Exampleville Council")
    for amount, supplier in ((3640, "Harbourline Insurance"), (1525, "Hearthstone Landlord Cover"),
                             (2580, "Shortstay Host Protect")):
        add(date(2025, 4, 3), amount, "insurance", "property", "periodic", supplier)

    # Repairs: the property's plumber (the example merchant rule), and the suite's own
    for n, (day, amount) in enumerate(((date(2025, 6, 23), 125), (date(2025, 9, 2), 355),
                                       (date(2025, 12, 11), 135), (date(2026, 3, 5), 205)),
                                      start=1041):
        add(day, amount, "repairs", "property", "periodic", "Example Plumbing",
            EXAMPLE_PLUMBING_ABN, f"EP-{n}")
    for day, amount in ((date(2025, 5, 19), 210), (date(2025, 8, 27), 490),
                        (date(2025, 11, 4), 235), (date(2026, 2, 17), 1025)):
        add(day, amount, "repairs", "listing", "periodic", "Handyhands Maintenance")

    # The suite's own running costs
    for day, amount in ((date(2025, 6, 20), 80), (date(2025, 10, 14), 185), (date(2026, 2, 11), 290)):
        add(day, amount, "water", "listing", "periodic", "Clearbrook Water")
    for day, amount in ((date(2025, 4, 22), 140), (date(2025, 8, 12), 70), (date(2025, 9, 30), 505),
                        (date(2025, 11, 25), 210), (date(2026, 1, 20), 125)):
        add(day, amount, "garden", "listing", "periodic", "Saltbush Gardening")
    for _ in range(34):
        add(some_day(), rng.randint(16, 36) / 2, "linen", "listing", "direct", "Suds & Spin Laundromat")
    add(date(2025, 4, 4), 65, "linen", "listing", "direct", "Linen Loft")
    add(date(2025, 12, 1), 45, "linen", "listing", "direct", "Linen Loft")
    for _ in range(7):
        add(some_day(), rng.triangular(20, 50, 30), "supplies", "listing", "consumable", "Corner Mart")
    for _ in range(28):
        add(some_day(), rng.uniform(15, 35), "welcome_box", "listing", "direct", "Farmgate Grocer")
    add(date(2025, 12, 18), 80, "welcome_box", "listing", "direct", "Hillside Cellar Door")
    add(date(2026, 1, 28), 62.50, "welcome_box", "listing", "direct", "Hillside Cellar Door")

    # Capital items: spread over their life by the views
    add(date(2025, 4, 2), 2465, "furnishings", "listing", "capital", "Oakline Furniture")
    add(date(2025, 4, 2), 890, "furnishings", "listing", "capital", "Oakline Furniture")
    add(date(2025, 5, 9), 125, "furnishings", "listing", "capital", "Lumen Homewares")
    add(date(2025, 9, 16), 80, "furnishings", "listing", "capital", "Lumen Homewares")
    add(date(2025, 11, 20), 2915, "repairs", "listing", "capital", "Coolbreeze Air")
    add(date(2025, 6, 5), 245, "other", "property", "capital", "Sentinel Home Security")
    add(date(2026, 2, 24), 345, "other", "property", "capital", "Sentinel Home Security")

    return sorted(rows, key=lambda r: (r["expense_date"], r["category"], r["supplier"],
                                       r["expense_amount"]))


def make_loan_interest():
    """{month_start: amount} for April 2025 to April 2026: the April 2026
    charge covers the last days of March, so March is complete too. A charge
    covers the days since the previous one; the balance falls slowly."""
    per_day = MONTHLY_INTEREST * 12 / 365
    charges = {}
    for i in range(13):
        month = date(2025 + (3 + i) // 12, (3 + i) % 12 + 1, 1)
        previous = date(month.year - (month.month == 1), (month.month - 2) % 12 + 1, 1)
        days = (month - previous).days
        charges[month] = cents(per_day * days * (1 - Decimal("0.003") * i))
    return charges


def insert_expenses_and_interest(db):
    """Insert the demo year's expenses and loan interest (one transaction)."""
    cfg = example_config()
    cur = db.cursor()
    try:
        cur.execute("SELECT l.listing_id, l.property_id FROM LISTING l "
                    "JOIN PROPERTY p ON p.property_id = l.property_id "
                    "WHERE l.listing_key = ? AND p.property_key = ?",
                    cfg["listing_key"], cfg["property_key"])
        listing_id, property_id = cur.fetchone()
        expenses = make_expenses()
        cur.executemany(
            "INSERT INTO EXPENSE (property_id, listing_id, expense_date, expense_amount, "
            "category, expense_type, scope, supplier, supplier_abn, invoice_number, "
            "extracted_by, verified_by_user) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual', 1)",
            [(property_id, listing_id if e["scope"] == "listing" else None,
              e["expense_date"], e["expense_amount"], e["category"], e["expense_type"],
              e["scope"], e["supplier"], e["supplier_abn"], e["invoice_number"])
             for e in expenses])
        interest = make_loan_interest()
        cur.executemany(
            "INSERT INTO LOAN_INTEREST (loan_code, month_start, interest_amount, source_note) "
            "VALUES (?, ?, ?, ?)",
            [(cfg["loan_code"], month, amount, f"Demo statement, charged day {cfg['charge_day']}")
             for month, amount in interest.items()])
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()
    print(f"Inserted {len(expenses)} demo expenses and {len(interest)} months of loan interest")


def main():
    args = sys.argv[1:]
    if "--out" not in args:
        sys.exit("Usage: python3 python/make_demo_data.py --out <folder>")
    folder = Path(args[args.index("--out") + 1])
    folder.mkdir(parents=True, exist_ok=True)
    for name in write_exports(folder):
        print(f"Wrote {folder / name}")


if __name__ == "__main__":
    main()
