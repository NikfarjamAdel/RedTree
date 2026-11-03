"""Checking the property config before anything is written."""
from datetime import date
from decimal import Decimal

from setup_property import load_plan, ownership_errors, same

CATEGORIES = {"electricity", "internet", "repairs", "linen", "supplies", "welcome_box"}


def config(**changes):
    base = {
        "properties": [{
            "key": "home", "name": "Home",
            "listings": [{"key": "suite", "title": "Suite", "listed_from": "2026-04-06",
                          "default_for_receipts": True}],
            "loans": [{"code": "main", "active_from": "2026-04-01", "charge_day": 19}],
        }],
        "apportionment": [
            {"listing": "suite", "category": "electricity", "rate": 0.5, "valid_from": "2026-04-06"},
        ],
        "merchant_rules": [
            {"match_type": "abn", "match_value": "51824753556", "merchant_name": "Plumber",
             "category": "repairs", "scope": "property"},
        ],
    }
    base.update(changes)
    return base


def errors_of(cfg):
    return load_plan(cfg, CATEGORIES)[1]


def test_a_valid_config_has_no_errors():
    assert errors_of(config()) == []


def test_unknown_top_level_key_is_reported():
    cfg = config()
    cfg["cleaning"] = {"cost_per_turnover": 110}           # belongs under a listing
    assert any("unknown top-level key" in e for e in errors_of(cfg))


def test_unknown_category_is_reported():
    cfg = config(apportionment=[{"listing": "suite", "category": "utilities",
                                 "rate": 0.5, "valid_from": "2026-04-06"}])
    assert any("unknown category" in e for e in errors_of(cfg))


def test_rate_above_one_is_reported():
    cfg = config(apportionment=[{"listing": "suite", "category": "electricity",
                                 "rate": 1.5, "valid_from": "2026-04-06"}])
    assert any("outside" in e for e in errors_of(cfg))


def test_two_defaults_in_one_property_are_reported():
    cfg = config()
    cfg["properties"][0]["listings"].append(
        {"key": "cottage", "title": "Cottage", "listed_from": "2026-04-06",
         "default_for_receipts": True})
    assert any("more than one listing" in e for e in errors_of(cfg))


def test_invalid_abn_in_a_rule_is_reported():
    cfg = config(merchant_rules=[{"match_type": "abn", "match_value": "51824758556",
                                  "merchant_name": "X"}])
    assert any("not a valid ABN" in e for e in errors_of(cfg))


def test_a_new_period_ends_the_previous_one():
    cfg = config(apportionment=[
        {"listing": "suite", "category": "electricity", "rate": 0.5, "valid_from": "2026-04-06"},
        {"listing": "suite", "category": "electricity", "rate": 0.65, "valid_from": "2026-11-01"},
    ])
    plan, errors = load_plan(cfg, CATEGORIES)
    assert errors == []
    first, second = sorted(plan["apportionment"], key=lambda a: a["valid_from"])
    assert first["valid_to"] == date(2026, 11, 1)
    assert second["valid_to"] is None


def test_numbers_compare_by_value():
    assert same(Decimal("0.4000"), Decimal("0.4"))
    assert same(None, None)
    assert not same(Decimal("0.4"), None)

CORRECTION = {"confirmation_code": "HMEXAMPLE1", "field": "check_out",
              "corrected_value": "2026-08-14", "reason": "Left a day early"}


def test_a_booking_correction_is_planned():
    plan, errors = load_plan(config(booking_corrections=[CORRECTION]), CATEGORIES)
    assert errors == []
    assert plan["corrections"] == [{"confirmation_code": "HMEXAMPLE1", "field": "check_out",
                                    "corrected_value": date(2026, 8, 14),
                                    "reason": "Left a day early"}]


def test_booking_correction_problems_are_reported():
    bad = [{**CORRECTION, "field": "nights"},
           {**CORRECTION, "corrected_value": "14/08/2026"},
           {**CORRECTION, "reason": ""},
           {**CORRECTION, "confirmation_code": ""}]
    errors = errors_of(config(booking_corrections=bad))
    for expected in ("field must be", "not a YYYY-MM-DD date", "reason must be",
                     "confirmation_code must be"):
        assert any(expected in e for e in errors), expected


def test_two_corrections_for_the_same_booking_and_field_are_refused():
    errors = errors_of(config(booking_corrections=[CORRECTION, CORRECTION]))
    assert any("a second correction" in e for e in errors)


def plan_of(cfg):
    plan, errors = load_plan(cfg, CATEGORIES)
    assert errors == []
    return plan


def test_keys_already_owned_by_this_property_are_fine():
    plan = plan_of(config())
    assert ownership_errors(plan, {"suite": "home"}, {"main": "home"}) == []


def test_a_loan_code_owned_by_another_property_is_refused():
    errors = ownership_errors(plan_of(config()), {}, {"main": "demo"})
    assert errors == [("loan code 'main' belongs to property 'demo' in the database; "
                       "choose another code")]


def test_a_listing_key_owned_by_another_property_is_refused():
    errors = ownership_errors(plan_of(config()), {"suite": "demo"}, {})
    assert any("listing key 'suite' belongs to property 'demo'" in e for e in errors)


def test_two_properties_in_one_config_cannot_share_a_loan_code():
    second = {"key": "beach", "listings": [{"key": "cabin", "listed_from": "2026-04-06"}],
              "loans": [{"code": "main", "active_from": "2026-04-01"}]}
    cfg = config()
    cfg["properties"].append(second)
    assert "properties[1].loans[0]: duplicate loan code 'main'" in errors_of(cfg)
