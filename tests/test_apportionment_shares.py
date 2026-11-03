"""The shares-over-100% checks add up shares within each property: in
setup_property.py (before saving) and check 4 in 30_data_quality.sql.

These run the real queries against the LOCAL database, inside a transaction
that is rolled back, so the database is left as it was. They are skipped
when the local database isn't reachable (e.g. Docker is stopped).
"""
import uuid

import pytest
from engine_setup import REPO_ROOT
from setup_property import SHARES_OVER_100
from sql_runner import split_batches

pytestmark = pytest.mark.db      # every test in this file needs a built database

CHECK_4 = next(batch for batch in split_batches((REPO_ROOT / "sql" / "30_data_quality.sql").read_text())
               if "CHECK 4:" in batch)
CHECKS = {"setup_property.py": SHARES_OVER_100, "30_data_quality.sql check 4": CHECK_4}


@pytest.fixture
def cur(local_db):
    cursor = local_db.cursor()
    yield cursor
    cursor.close()


def add_property(cur, *electricity_rates):
    """A property with one listing per rate, each sharing electricity at that
    rate from 2030; returns the property key."""
    key = f"test-{uuid.uuid4().hex[:8]}"
    cur.execute("INSERT INTO PROPERTY (property_key, property_name) VALUES (?, ?)", key, key)
    cur.execute("SELECT property_id FROM PROPERTY WHERE property_key = ?", key)
    property_id = cur.fetchone()[0]
    for n, rate in enumerate(electricity_rates):
        cur.execute("INSERT INTO LISTING (listing_title, listing_status, listed_from, property_id) "
                    "VALUES (?, 'active', '2030-01-01', ?)", f"{key} listing {n}", property_id)
        cur.execute("SELECT MAX(listing_id) FROM LISTING WHERE property_id = ?", property_id)
        listing_id = cur.fetchone()[0]
        cur.execute("INSERT INTO APPORTIONMENT_RULE (listing_id, category, rate, valid_from) "
                    "VALUES (?, 'electricity', ?, '2030-01-01')", listing_id, rate)
    return key


def over_100(cur, sql, property_keys):
    cur.execute(sql)
    return [tuple(row) for row in cur.fetchall() if row[0] in property_keys]


@pytest.mark.parametrize("check", CHECKS)
def test_two_properties_at_50_50_each_pass(cur, check):
    keys = {add_property(cur, 0.5, 0.5), add_property(cur, 0.5, 0.5)}
    assert over_100(cur, CHECKS[check], keys) == []


@pytest.mark.parametrize("check", CHECKS)
def test_one_property_adding_to_115_fails(cur, check):
    key = add_property(cur, 0.5, 0.65)
    rows = over_100(cur, CHECKS[check], {key})
    assert len(rows) == 1
    assert rows[0][0] == key and rows[0][1] == "electricity"
    assert float(rows[0][3]) == pytest.approx(1.15)
