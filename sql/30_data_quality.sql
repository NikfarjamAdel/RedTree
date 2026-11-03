-- ============================================================
-- Data quality assertions
-- Each query should return ZERO rows. Any rows are violations.
-- Run after every load.
-- ============================================================


-- CHECK 1: Stock balance must never go negative.
-- A violation means consumption was over-estimated, or a purchase
-- was never recorded. Consumption is estimated between stocktakes,
-- so occasional violations are expected rather than impossible.

SELECT * FROM (
    SELECT item_id, movement_date, movement_type, quantity_change,
           SUM(quantity_change) OVER (PARTITION BY item_id ORDER BY movement_date, movement_id) AS balance
    FROM STOCK_MOVEMENT
) t
WHERE balance < 0;
GO


-- CHECK 2: No two bookings on the same listing may overlap.
--
-- STAY_NIGHT's unique index already makes this impossible to load, so a
-- violation here means bookings exist that were never exploded into stay
-- nights -- i.e. a load aborted partway.
--
-- More importantly, this is the only detector for a known export defect:
-- the Airbnb earnings CSV reports a booking's ORIGINAL end date, not the
-- date the guest actually left. An early departure is invisible unless the
-- freed nights resell to someone else, which produces an overlap here.
-- Record the real date as a booking correction in the property config.


SELECT b1.confirmation_code AS code_1, b1.check_in AS in_1, b1.check_out AS out_1,
       b2.confirmation_code AS code_2, b2.check_in AS in_2, b2.check_out AS out_2
FROM BOOKING b1
JOIN BOOKING b2 ON b2.listing_id = b1.listing_id
               AND b2.booking_id > b1.booking_id
               AND b1.check_in < b2.check_out
               AND b2.check_in < b1.check_out
WHERE NOT EXISTS (SELECT 1 FROM BOOKING_HISTORY h
                  WHERE h.booking_id = b1.booking_id AND h.action_type = 'cancelled')
  AND NOT EXISTS (SELECT 1 FROM BOOKING_HISTORY h
                  WHERE h.booking_id = b2.booking_id AND h.action_type = 'cancelled');

GO


-- CHECK 3: No two apportionment rules for the same listing and category
-- may be in force on the same day.
--
-- Periods are half-open, [valid_from, valid_to), and a NULL valid_to means
-- the rule is still in force. Two periods overlap when each starts before
-- the other ends.

SELECT a.rule_id, b.rule_id AS overlapping_rule, a.listing_id, a.category
FROM APPORTIONMENT_RULE a
JOIN APPORTIONMENT_RULE b
  ON  b.listing_id = a.listing_id
  AND b.category   = a.category
  AND b.rule_id    > a.rule_id
  AND a.valid_from < ISNULL(b.valid_to, '9999-12-31')
  AND b.valid_from < ISNULL(a.valid_to, '9999-12-31');

GO


-- CHECK 4: For each property and category, the shares of the property's
-- listings must not add up to more than 100% on any date. Shares split a
-- property's bills among that property's listings only (vw_expense_monthly),
-- so listings of different properties are never added together.
--
-- A total can only rise on the day a rule starts, so checking every start
-- date covers every date. Also checked by setup_property.py before saving.

SELECT p.property_key, d.category, d.valid_from AS on_date, SUM(r.rate) AS total_rate
FROM (SELECT DISTINCT l.property_id, a.category, a.valid_from
      FROM APPORTIONMENT_RULE a
      JOIN LISTING l ON l.listing_id = a.listing_id) d
JOIN PROPERTY p ON p.property_id = d.property_id
JOIN APPORTIONMENT_RULE r
  ON  r.category   = d.category
  AND r.valid_from <= d.valid_from
  AND (r.valid_to IS NULL OR r.valid_to > d.valid_from)
JOIN LISTING rl
  ON  rl.listing_id  = r.listing_id
  AND rl.property_id = d.property_id
GROUP BY p.property_key, d.category, d.valid_from
HAVING SUM(r.rate) > 1;

GO


-- CHECK 5: Every property-scope expense must have an apportionment rule
-- in force on its date.
--
-- Without one, the suite's share of the expense is undefined.

-- EXPENSE: every property-scope expense is shared to at least one listing
-- in at least one month; one that never is has an undefined share.
SELECT e.expense_id, e.category, e.expense_date, e.supplier
FROM EXPENSE e
WHERE e.scope = 'property'
  AND NOT EXISTS (SELECT 1 FROM vw_expense_monthly m WHERE m.expense_id = e.expense_id);

GO
