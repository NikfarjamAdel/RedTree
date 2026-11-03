
-- 50_transform.sql
-- Staging -> operational tables. Idempotent on confirmation_code.
-- Run after 40_stage.sql and python/load_airbnb.py.

INSERT INTO GUEST (display_name)
SELECT DISTINCT s.stg_guest_name
FROM STG_AIRBNB_EARNINGS s
WHERE s.stg_type = 'Reservation'
  AND NOT EXISTS (SELECT 1 FROM GUEST g WHERE g.display_name = s.stg_guest_name);

WITH src AS (
    SELECT stg.stg_confirmation_code                                AS confirmation_code,
           stg.stg_guest_name                                       AS guest_name,
           stg.stg_listing                                          AS listing_name,
           TRY_CONVERT(DATETIME, stg.stg_booking_date, 101)         AS booked_on,
           TRY_CONVERT(DATE,     stg.stg_start_date,   101)         AS check_in,
           TRY_CONVERT(DATE,     stg.stg_end_date,     101)         AS check_out,
           TRY_CAST(stg.stg_amount       AS DECIMAL(10,2))          AS amount,
           TRY_CAST(stg.stg_service_fee  AS DECIMAL(10,2))          AS service_fee,
           TRY_CAST(stg.stg_cleaning_fee AS DECIMAL(10,2))          AS cleaning_fee
    FROM STG_AIRBNB_EARNINGS stg
    WHERE stg.stg_type = 'Reservation'
)
INSERT INTO BOOKING (confirmation_code, guest_id, listing_id, booked_on,
                     check_in, check_out, nightly_rate, cleaning_fee,
                     source, booking_data_source)
SELECT src.confirmation_code,
       g.guest_id,
       l.listing_id,
       src.booked_on,
       src.check_in,
       src.check_out,
       (src.amount + src.service_fee - src.cleaning_fee)
           / NULLIF(DATEDIFF(DAY, src.check_in, src.check_out), 0),
       src.cleaning_fee,
       'airbnb',
       'real'
FROM src
JOIN GUEST   g ON g.display_name  = src.guest_name
JOIN LISTING l ON l.airbnb_name = src.listing_name
WHERE NOT EXISTS (
        SELECT 1 FROM BOOKING b
        WHERE b.confirmation_code = src.confirmation_code
      );



WITH src AS (
  SELECT stg.stg_confirmation_code                        AS code,
         TRY_CONVERT(DATE, stg.stg_date, 101)             AS payout_date,
         TRY_CAST(stg.stg_amount      AS DECIMAL(10,2))   AS amount,
         TRY_CAST(stg.stg_service_fee AS DECIMAL(10,2))   AS platform_fee,
         stg.stg_currency                                 AS currency
  FROM STG_AIRBNB_EARNINGS stg
  WHERE stg.stg_type = 'Reservation'
  AND stg.stg_file_kind = 'paid'
)
INSERT INTO PAYOUT (booking_id, payout_date, gross_amount, platform_fee, currency)
SELECT b.booking_id, src.payout_date,
       src.amount + src.platform_fee, src.platform_fee, src.currency
FROM src
JOIN BOOKING b ON b.confirmation_code = src.code
WHERE NOT EXISTS (SELECT 1 FROM PAYOUT p WHERE p.booking_id = b.booking_id);

GO
-- STEP 3b: Booking history for real bookings.
--
-- The earnings export carries no status events, so these are inferred:
-- every loaded booking was confirmed when booked, and is completed once
-- its check-out date has passed. Cancellations are absent from the export
-- entirely and are not inferred here. Idempotent on (booking, action).

INSERT INTO BOOKING_HISTORY (booking_id, action_type, action_date, reason)
SELECT b.booking_id, 'confirmed', b.booked_on, 'Inferred from earnings export'
FROM BOOKING b
WHERE b.booking_data_source = 'real'
  AND NOT EXISTS (SELECT 1 FROM BOOKING_HISTORY h
                  WHERE h.booking_id = b.booking_id AND h.action_type = 'confirmed');


GO