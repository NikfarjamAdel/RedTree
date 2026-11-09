"""The dashboard shows the report's figures, never a second calculation of them."""
import pytest
from dashboard_data import check_title, dashboard_payload


def test_check_title_is_the_first_sentence_of_the_comment():
    batch = ("-- CHECK 7: Every widget has a sprocket. A violation means\n"
             "-- the load stopped early.\nSELECT 1")
    assert check_title("x.sql", 7, batch) == "Every widget has a sprocket"


def test_check_title_joins_a_sentence_across_comment_lines():
    batch = ("-- CHECK 2: For each property, the shares must not add\n"
             "-- up to more than 100%. Shares split a property's bills.\n--\nSELECT 1")
    assert check_title("x.sql", 2, batch) == \
        "For each property, the shares must not add up to more than 100%"


def test_check_title_drops_the_expect_note():
    batch = "-- CHECK 1:Totals agree, per listing (expect no rows)\nSELECT 1"
    assert check_title("x.sql", 1, batch) == "Totals agree, per listing"


def test_known_checks_use_plain_names():
    assert check_title("30_data_quality.sql", 2, "-- CHECK 2: anything") == \
        "No two stays overlap on a listing"


@pytest.mark.db
def test_monthly_figures_are_the_reports(local_db):
    payload = dashboard_payload(local_db)
    cur = local_db.cursor()
    cur.execute("SELECT COUNT(*), SUM(revenue), SUM(profit_after_interest) FROM vw_monthly_report")
    rows, revenue, kept = cur.fetchone()
    assert len(payload["months"]) == rows
    assert sum(m["revenue"] for m in payload["months"]) == pytest.approx(float(revenue or 0))
    assert sum(m["profit_after_interest"] for m in payload["months"]) == pytest.approx(float(kept or 0))


@pytest.mark.db
def test_spread_fees_add_back_to_the_payouts(local_db):
    payload = dashboard_payload(local_db)
    cur = local_db.cursor()
    cur.execute("SELECT ISNULL(SUM(platform_fee), 0) FROM PAYOUT")
    total = float(cur.fetchone()[0])
    # rounded to the cent once per listing-month
    assert sum(f["platform_fee"] for f in payload["fees"]) == \
        pytest.approx(total, abs=0.01 * max(1, len(payload["fees"])))


@pytest.mark.db
def test_a_share_is_never_more_than_its_bill(local_db):
    payload = dashboard_payload(local_db)
    bills = {}
    for b in payload["expense_bills"]:
        key = (b["listing_id"], b["month_start"], b["category"])
        bills[key] = bills.get(key, 0) + b["amount"]
    for s in payload["expense_shares"]:
        key = (s["listing_id"], s["month_start"], s["category"])
        assert s["amount"] <= bills.get(key, 0) + 0.01, key


@pytest.mark.db
def test_every_build_check_is_reported(local_db):
    checks = dashboard_payload(local_db)["checks"]
    assert len(checks) >= 6
    assert all(c["title"] and isinstance(c["failing_rows"], int) for c in checks)
