"""The demo data generator: the same every time, never overlapping, and
exports whose amounts come out right through 50_transform.sql."""
import csv
import io
import json
from datetime import date, datetime
from decimal import Decimal
from itertools import pairwise

from load_airbnb import COLUMNS, PATTERNS
from make_demo_data import (
    CLEANING_FEE,
    END,
    EXAMPLE_CONFIG,
    START,
    make_bookings,
    make_expenses,
    make_exports,
    make_loan_interest,
)

LISTING = "The Suite"


def parse(text):
    return list(csv.DictReader(io.StringIO(text)))


def mdy(text):
    return datetime.strptime(text, "%m/%d/%Y").date()


def test_the_same_seed_gives_the_same_data():
    assert make_bookings() == make_bookings()
    assert make_exports(make_bookings(), LISTING) == make_exports(make_bookings(), LISTING)
    assert make_expenses() == make_expenses()
    assert make_loan_interest() == make_loan_interest()


def test_another_seed_gives_other_data():
    assert make_bookings(1) != make_bookings(2)


def test_stays_never_overlap_and_fit_the_year():
    bookings = sorted(make_bookings(), key=lambda b: b.check_in)
    assert bookings[0].check_in >= START
    assert bookings[-1].check_out <= END
    for earlier, later in pairwise(bookings):
        assert later.check_in >= earlier.check_out
    for b in bookings:
        assert b.check_out > b.check_in
        assert b.booked_on <= b.check_in < b.payout_date


def test_confirmation_codes_are_unique_and_marked_as_demo():
    codes = [b.code for b in make_bookings()]
    assert len(set(codes)) == len(codes)
    assert all(c.startswith("HMDEMO") and len(c) == 10 for c in codes)


def test_exports_look_like_airbnb_earnings_exports():
    exports = make_exports(make_bookings(), LISTING)
    for name, text in exports.items():
        assert PATTERNS["paid"].match(name)
        header = next(csv.reader(io.StringIO(text)))
        assert header == list(COLUMNS)
    rows = [r for text in exports.values() for r in parse(text)]
    assert {r["Type"] for r in rows} == {"Payout", "Reservation"}
    assert sum(r["Type"] == "Reservation" for r in rows) == len(make_bookings())


def test_export_amounts_match_the_transform():
    """50_transform.sql: nightly_rate = (Amount + Service fee - Cleaning fee) / nights,
    payout gross = Amount + Service fee, net = Amount (check 3 in 31)."""
    by_code = {b.code: b for b in make_bookings()}
    for text in make_exports(list(by_code.values()), LISTING).values():
        rows = parse(text)
        for payout, reservation in zip(rows[0::2], rows[1::2]):
            assert (payout["Type"], reservation["Type"]) == ("Payout", "Reservation")
            amount = Decimal(reservation["Amount"])
            fee = Decimal(reservation["Service fee"])
            cleaning = Decimal(reservation["Cleaning fee"])
            nights = (mdy(reservation["End date"]) - mdy(reservation["Start date"])).days
            booking = by_code[reservation["Confirmation Code"]]

            assert Decimal(payout["Paid out"]) == amount
            assert Decimal(reservation["Gross earnings"]) == amount + fee
            assert cleaning == CLEANING_FEE
            assert int(reservation["Nights"]) == nights == booking.nights
            # exact to the cent, so the DECIMAL(8,2) column loses nothing
            assert (amount + fee - cleaning) / nights == booking.nightly_rate
            assert booking.nightly_rate == booking.nightly_rate.quantize(Decimal("0.01"))
            assert reservation["Listing"] == LISTING
            assert mdy(reservation["Date"]) == booking.payout_date
            assert mdy(reservation["Booking date"]) <= mdy(reservation["Start date"])


def test_each_payout_is_in_the_export_after_its_month():
    for name, text in make_exports(make_bookings(), LISTING).items():
        export_date = date.fromisoformat(PATTERNS["paid"].match(name).group(1))
        for row in parse(text):
            paid = mdy(row["Date"])
            assert paid < export_date
            assert (paid.year, paid.month) == ((export_date.year, export_date.month - 1)
                                               if export_date.month > 1
                                               else (export_date.year - 1, 12))


def test_expenses_cover_every_category_and_are_shareable():
    expenses = make_expenses()
    config = json.loads(EXAMPLE_CONFIG.read_text())
    shared = {a["category"] for a in config["apportionment"]}
    assert {e["category"] for e in expenses} == {
        "electricity", "water", "internet", "council_rates", "insurance", "linen",
        "furnishings", "garden", "repairs", "supplies", "welcome_box", "other"}
    for e in expenses:
        assert e["expense_amount"] > 0
        assert e["scope"] in ("listing", "property")
        if e["scope"] == "property":          # 30_data_quality: needs an apportionment rule
            assert e["category"] in shared
        assert START <= e["expense_date"] <= END


def test_loan_interest_covers_every_demo_month_and_the_next():
    interest = make_loan_interest()
    assert list(interest) == [date(2025 + (3 + i) // 12, (3 + i) % 12 + 1, 1) for i in range(13)]
    assert all(amount > 0 for amount in interest.values())


def test_the_example_correction_shortens_a_generated_stay():
    """config/property.example.json corrects a demo booking, so --demo runs
    the correction path (51, 54) end to end."""
    by_code = {b.code: b for b in make_bookings()}
    for c in json.loads(EXAMPLE_CONFIG.read_text())["booking_corrections"]:
        booking = by_code[c["confirmation_code"]]
        assert c["field"] == "check_out"
        assert booking.check_in < date.fromisoformat(c["corrected_value"]) < booking.check_out
