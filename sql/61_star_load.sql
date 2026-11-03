
-- Facts before dimensions: the facts hold the foreign keys.
DELETE FROM FACT_EXPENSE;
DELETE FROM FACT_AVAILABLE_NIGHT;
DELETE FROM FACT_BOOKING;
DELETE FROM FACT_STAY_NIGHT;
DELETE FROM DIM_CATEGORY;
DELETE FROM DIM_BOOKING;
DELETE FROM DIM_GUEST;
DELETE FROM DIM_LISTING;
DELETE FROM DIM_DATE;
GO

INSERT INTO DIM_LISTING (listing_id, listing_title, listing_address, listing_status, listed_from)
SELECT listing_id, listing_title, listing_address, listing_status, listed_from
FROM LISTING;
GO

INSERT INTO DIM_GUEST (guest_id, display_name, has_email, is_repeat_guest)
SELECT g.guest_id,
       g.display_name,
       CASE WHEN g.guest_email IS NOT NULL THEN 1 ELSE 0 END,
       CASE WHEN (SELECT COUNT(*) FROM BOOKING b WHERE b.guest_id = g.guest_id) > 1
            THEN 1 ELSE 0 END
FROM GUEST g;
GO

WITH latest AS (
    SELECT booking_id, action_type,
           ROW_NUMBER() OVER (PARTITION BY booking_id
                              ORDER BY action_date DESC, history_id DESC) AS rn
    FROM BOOKING_HISTORY
)
INSERT INTO DIM_BOOKING (booking_id, confirmation_code, source, booking_data_source,
                         current_status, is_cancelled)
SELECT b.booking_id, b.confirmation_code, b.source, b.booking_data_source,
       COALESCE(l.action_type, 'confirmed'),
       CASE WHEN l.action_type = 'cancelled' THEN 1 ELSE 0 END
FROM BOOKING b
LEFT JOIN latest l ON l.booking_id = b.booking_id AND l.rn = 1;
GO

INSERT INTO DIM_DATE (date_key, year_number, quarter_number, month_number, month_name,
                      day_of_month, day_of_week, day_name, week_of_year, is_weekend,
                      is_public_holiday, holiday_name, is_school_holiday, season)
SELECT date_key, year_number, quarter_number, month_number, month_name,
       day_of_month, day_of_week, day_name, week_of_year, is_weekend,
       is_public_holiday, holiday_name, is_school_holiday, season
FROM CALENDAR;
GO

INSERT INTO FACT_STAY_NIGHT (date_key, listing_key, guest_key, booking_key, rate_night)
SELECT s.night_date,
       dl.listing_key,
       dg.guest_key,
       db.booking_key,
       s.rate_night
FROM STAY_NIGHT s
JOIN BOOKING     b  ON b.booking_id   = s.booking_id
JOIN DIM_LISTING dl ON dl.listing_id  = s.listing_id
JOIN DIM_GUEST   dg ON dg.guest_id    = b.guest_id
JOIN DIM_BOOKING db ON db.booking_id  = s.booking_id;
GO

INSERT INTO FACT_BOOKING (booked_date_key, checked_in_date_key, checked_out_date_key,
                          listing_key, guest_key, booking_key,
                          platform_fee, net_amount, cleaning_fee, cleaning_cost,
                          number_of_guests, overall_rating)
SELECT CAST(b.booked_on AS DATE), b.check_in, b.check_out, dl.listing_key, dg.guest_key, db.booking_key, platform_fee, p.net_amount, b.cleaning_fee, c.cleaning_cost, b.number_of_guests, r.overall_rating
FROM BOOKING b
JOIN      DIM_LISTING dl ON b.listing_id = dl.listing_id
JOIN      DIM_GUEST   dg ON b.guest_id = dg.guest_id
JOIN      DIM_BOOKING db ON b.booking_id = db.booking_id
LEFT JOIN PAYOUT      p  ON b.booking_id = p.booking_id
LEFT JOIN CLEANING    c  ON b.booking_id = c.booking_id
LEFT JOIN REVIEW      r  ON b.booking_id = r.booking_id AND r.direction = 'guest_to_host';

GO

INSERT INTO FACT_AVAILABLE_NIGHT (date_key, listing_key)
SELECT d.date_key, dl.listing_key
FROM DIM_DATE d
CROSS JOIN DIM_LISTING dl
WHERE d.date_key >= dl.listed_from
  AND NOT EXISTS (
        SELECT 1
        FROM BLOCKED_PERIOD bp
        WHERE bp.listing_id = dl.listing_id
          AND d.date_key >= bp.blocked_start_date
          AND d.date_key <= bp.blocked_end_date
      );
GO

INSERT INTO DIM_CATEGORY (category_code, description, default_expense_type)
SELECT category_code, description, default_expense_type
FROM CATEGORY;

-- Capped at the current month, like FACT_AVAILABLE_NIGHT: future months of
-- a spread only appear when they arrive.
INSERT INTO FACT_EXPENSE (month_key, listing_key, category_key, expense_id,
                          expense_type, is_verified, amount)
SELECT m.month_start, l.listing_key, c.category_key, m.expense_id,
       m.expense_type, m.verified_by_user, m.amount
FROM vw_expense_monthly m
JOIN DIM_LISTING  l ON l.listing_id    = m.listing_id
JOIN DIM_CATEGORY c ON c.category_code = m.category
WHERE m.month_start <= DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1);

GO