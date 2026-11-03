-- 52_impute_cleaning.sql
--
-- Insourced cleans carry an imputed cost, so profit is honest
-- and outsourcing can be compared fairly. One clean per completed,
-- not-cancelled real booking, dated at check-out, at its listing's
-- cleaning_cost_per_turnover (from the config). A listing without a rate
-- gets no imputed cleans. Idempotent: bookings with a cleaning row are skipped.

INSERT INTO CLEANING (booking_id, cleaning_date, cleaning_type, cleaner_name, cleaning_cost)
SELECT b.booking_id, b.check_out, 'insourced', N'owner', l.cleaning_cost_per_turnover
FROM BOOKING b
JOIN LISTING l ON l.listing_id = b.listing_id
WHERE b.booking_data_source = 'real'
  AND l.cleaning_cost_per_turnover IS NOT NULL
  AND b.check_out <= CAST(GETDATE() AS DATE)
  AND NOT EXISTS (SELECT 1 FROM CLEANING c WHERE c.booking_id = b.booking_id)
  AND NOT EXISTS (SELECT 1 FROM BOOKING_HISTORY h
                  WHERE h.booking_id = b.booking_id AND h.action_type = 'cancelled');