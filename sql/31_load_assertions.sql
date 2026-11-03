-- 31_load_assertions.sql
--
-- Checks that a load is complete and correct. Each query returns no rows
-- when the data is right; any rows fail the load. Run immediately after
-- loading (load_airbnb.py does): checks 1, 3 and 4 read the staging table,
-- which is replaced on every load.


-- CHECK 1: Loaded payouts must reconcile against the source export.
--
-- PAYOUT.net_amount is computed as gross_amount - platform_fee, where
-- gross was derived at load time as (Amount + Service fee). So net_amount
-- must come back to the CSV's Amount exactly. Any row here means the
-- derivation is wrong or the wrong staging row was matched.
--
-- Amount is used rather than the export's own Gross earnings column,
-- which is not consistently defined across exports.
--
-- Note: only meaningful while staging still holds the export these
-- bookings came from. Staging is truncated on each load, so run this
-- immediately after loading, not weeks later.

SELECT b.confirmation_code, p.net_amount, s.stg_amount
FROM PAYOUT p
JOIN BOOKING b ON b.booking_id = p.booking_id
JOIN STG_AIRBNB_EARNINGS s ON s.stg_confirmation_code = b.confirmation_code
WHERE b.booking_data_source = 'real'
AND p.net_amount <> TRY_CAST(s.stg_amount AS DECIMAL(10,2));

GO
-- CHECK 2: Every listing named in the export must exist in LISTING.
--
-- The transform joins staging to LISTING on airbnb_name. An unmatched
-- name is silently dropped by that inner join, so a new or renamed
-- listing produces a successful load that inserted nothing.
--
-- A violation means either a genuinely new listing (add it to the
-- config) or a rename on Airbnb's side (update airbnb_name in the config).
-- Either way it is a decision, not something to auto-create. Then run
-- setup_property.py.
SELECT DISTINCT stg.stg_listing
FROM STG_AIRBNB_EARNINGS stg
WHERE stg.stg_type = 'Reservation'
  AND NOT EXISTS (SELECT 1 FROM LISTING l WHERE l.airbnb_name = stg.stg_listing);
GO

-- CHECK 3: Every reservation row in staging must have produced a booking.
--
-- Catches any cause of rows going missing during the transform, not just
-- an unmatched listing. Counts must be equal.
SELECT (SELECT COUNT(*) FROM STG_AIRBNB_EARNINGS WHERE stg_type = 'Reservation') AS staged,
       (SELECT COUNT(*) FROM BOOKING WHERE booking_data_source = 'real')         AS loaded
WHERE  (SELECT COUNT(*) FROM STG_AIRBNB_EARNINGS WHERE stg_type = 'Reservation')
    <> (SELECT COUNT(*) FROM BOOKING WHERE booking_data_source = 'real');
GO


-- CHECK 4: Every staged reservation must have become a booking.
--
-- Names the rows CHECK 3 counts. A row here means a reservation was
-- dropped, most likely because its Listing name in the CSV doesn't match
-- any LISTING.airbnb_name.
SELECT s.stg_confirmation_code, s.stg_listing
FROM STG_AIRBNB_EARNINGS s
WHERE s.stg_type = 'Reservation'
  AND NOT EXISTS (SELECT 1 FROM BOOKING b
                  WHERE b.confirmation_code = s.stg_confirmation_code);

GO

-- CHECK 5: Every real booking's stay nights match its current dates
-- exactly; a cancelled booking has none.
SELECT b.confirmation_code, b.check_in, b.check_out, COUNT(s.night_date) AS nights
FROM BOOKING b
LEFT JOIN STAY_NIGHT s ON s.booking_id = b.booking_id
WHERE b.booking_data_source = 'real'
GROUP BY b.booking_id, b.confirmation_code, b.check_in, b.check_out
HAVING COUNT(s.night_date) <>
       CASE WHEN EXISTS (SELECT 1 FROM BOOKING_HISTORY h
                         WHERE h.booking_id = b.booking_id AND h.action_type = 'cancelled')
            THEN 0
            ELSE DATEDIFF(DAY, b.check_in, b.check_out) END;
GO


