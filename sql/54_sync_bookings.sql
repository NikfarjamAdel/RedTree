-- 54_sync_bookings.sql
--
-- Brings bookings and their stay nights into line with everything known:
-- corrections first, then stay nights matched to each booking's current
-- dates (STAY_NIGHT is derived: regenerated, never edited),
-- then the 'completed' history. Runs on every load and is idempotent.
-- Order matters: nights are removed before any are added, so a corrected
-- booking frees its nights before another booking takes them.

-- 1. Apply corrections: for each booking and field, the latest one wins.
WITH latest AS (
    SELECT confirmation_code, field, corrected_value,
           ROW_NUMBER() OVER (PARTITION BY confirmation_code, field
                              ORDER BY created_at DESC, correction_id DESC) AS rn
    FROM BOOKING_CORRECTION
)
UPDATE b
SET check_in  = COALESCE(ci.corrected_value, b.check_in),
    check_out = COALESCE(co.corrected_value, b.check_out)
FROM BOOKING b
LEFT JOIN latest ci ON ci.confirmation_code = b.confirmation_code
                   AND ci.field = 'check_in'  AND ci.rn = 1
LEFT JOIN latest co ON co.confirmation_code = b.confirmation_code
                   AND co.field = 'check_out' AND co.rn = 1
WHERE ci.confirmation_code IS NOT NULL OR co.confirmation_code IS NOT NULL;

-- 2. Remove nights that no longer belong: those of cancelled bookings,
--    and those outside a booking's (corrected) dates.
DELETE s
FROM STAY_NIGHT s
JOIN BOOKING b ON b.booking_id = s.booking_id
WHERE b.booking_data_source = 'real'
  AND (   s.night_date <  b.check_in
       OR s.night_date >= b.check_out
       OR EXISTS (SELECT 1 FROM BOOKING_HISTORY h
                  WHERE h.booking_id = b.booking_id AND h.action_type = 'cancelled'));

-- 3. Add any night a current, not-cancelled booking is missing. Checked per
--    night, so an extended stay gains its new nights too.
INSERT INTO STAY_NIGHT (booking_id, listing_id, night_date, rate_night)
SELECT b.booking_id, b.listing_id, c.date_key, b.nightly_rate
FROM BOOKING b
JOIN CALENDAR c ON c.date_key >= b.check_in AND c.date_key < b.check_out
WHERE b.booking_data_source = 'real'
  AND NOT EXISTS (SELECT 1 FROM BOOKING_HISTORY h
                  WHERE h.booking_id = b.booking_id AND h.action_type = 'cancelled')
  AND NOT EXISTS (SELECT 1 FROM STAY_NIGHT s
                  WHERE s.booking_id = b.booking_id AND s.night_date = c.date_key);

-- 4. Completed: inferred once the (corrected) check-out date has passed.
INSERT INTO BOOKING_HISTORY (booking_id, action_type, action_date, reason)
SELECT b.booking_id, 'completed',
       DATEADD(HOUR, 11, CAST(b.check_out AS DATETIME)),
       'Inferred: check-out date has passed'
FROM BOOKING b
WHERE b.booking_data_source = 'real'
  AND b.check_out < CAST(GETDATE() AS DATE)
  AND NOT EXISTS (SELECT 1 FROM BOOKING_HISTORY h
                  WHERE h.booking_id = b.booking_id AND h.action_type = 'completed')
  AND NOT EXISTS (SELECT 1 FROM BOOKING_HISTORY h
                  WHERE h.booking_id = b.booking_id AND h.action_type = 'cancelled');
-- 5. A completed date inferred before a correction moves to the corrected
--    check-out (e.g. a guest who left early).
UPDATE h
SET action_date = DATEADD(HOUR, 11, CAST(b.check_out AS DATETIME))
FROM BOOKING_HISTORY h
JOIN BOOKING b ON b.booking_id = h.booking_id
WHERE h.action_type = 'completed'
  AND h.reason = 'Inferred: check-out date has passed'
  AND h.action_date <> DATEADD(HOUR, 11, CAST(b.check_out AS DATETIME));
