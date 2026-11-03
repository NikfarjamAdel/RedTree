-- 51_corrections.sql
--
-- Manual corrections to booking dates are entered in the
-- property config and loaded into BOOKING_CORRECTION by setup_property.py;
-- 54_sync_bookings.sql applies them. No booking data lives here.
--
-- A booking with a manual correction gets one 'dates_changed' history row,
-- dated the day its first correction gives (e.g. the day the guest actually
-- left), with that correction's reason. Idempotent.
WITH first_manual AS (
    SELECT confirmation_code, corrected_value, reason,
           ROW_NUMBER() OVER (PARTITION BY confirmation_code
                              ORDER BY created_at, correction_id) AS rn
    FROM BOOKING_CORRECTION
    WHERE source = 'manual'
)
INSERT INTO BOOKING_HISTORY (booking_id, action_type, action_date, reason)
SELECT b.booking_id, 'dates_changed', CAST(c.corrected_value AS DATETIME), c.reason
FROM first_manual c
JOIN BOOKING b ON b.confirmation_code = c.confirmation_code
WHERE c.rn = 1
  AND NOT EXISTS (SELECT 1 FROM BOOKING_HISTORY h
                  WHERE h.booking_id = b.booking_id AND h.action_type = 'dates_changed');
GO
