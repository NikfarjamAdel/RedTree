/* ============================================================================
   10_analysis.sql
   RedTree analytical query library.

   Conventions
     - Revenue means PAYOUT.net_amount: what actually reached the bank after
       Airbnb's service fee. The platform fee is never revenue and never an
       expense; it simply does not appear.
     - Cleaning costs live in CLEANING.cleaning_cost, NOT in EXPENSE.
       Any total-cost figure must add both.
     - vw_available_night is the denominator for anything occupancy-shaped:
       one row per listing per night that was genuinely for sale.

   Each query stands alone: run them one at a time. build_db.py runs the
   whole file once, to check every query still works.
   ============================================================================ */


/* ---------------------------------------------------------------------------
   Q1. Occupancy rate by listing and month.
   Booked nights / nights actually available for sale.
   The view supplies the denominator, so blocked periods and pre-listing
   dates are already excluded.
   --------------------------------------------------------------------------- */

SELECT v.listing_id,
       v.year_number,
       v.month_name,
       COUNT(v.date_key)                                   AS available_nights,
       COUNT(s.stay_night_id)                              AS booked_nights,
       CAST(COUNT(s.stay_night_id) AS DECIMAL(6,4))
           / COUNT(v.date_key)                             AS occupancy_rate
FROM vw_available_night v
LEFT JOIN STAY_NIGHT s
       ON s.night_date = v.date_key
      AND s.listing_id = v.listing_id
GROUP BY v.listing_id, v.year_number, v.month_number, v.month_name
ORDER BY v.listing_id, v.year_number, v.month_number;


/* ---------------------------------------------------------------------------
   Q2. Revenue by listing and month, decomposed.
   Driven from a CALENDAR x LISTING spine so months with no bookings still
   appear as zero rather than vanishing.
   must_match exists only as a fan-out check: it has to equal total_net,
   because net_amount is a computed column. If it doesn't, a join is
   duplicating rows.
   --------------------------------------------------------------------------- */
SELECT l.listing_id,
       c.year_number,
       c.month_name,
       COALESCE(SUM(p.gross_amount - b.cleaning_fee), 0)   AS gross_accommodation,
       COALESCE(SUM(b.cleaning_fee), 0)                    AS gross_cleaning_fees,
       COALESCE(SUM(p.platform_fee), 0)                    AS platform_fees,
       COALESCE(SUM(p.net_amount), 0)                      AS total_net,
       COALESCE(SUM(p.gross_amount - p.platform_fee), 0)   AS must_match
FROM CALENDAR c
CROSS JOIN LISTING l
LEFT JOIN BOOKING b
       ON b.check_in   = c.date_key
      AND b.listing_id = l.listing_id
LEFT JOIN PAYOUT p
       ON p.booking_id = b.booking_id
WHERE c.date_key >= l.listed_from
GROUP BY l.listing_id, c.year_number, c.month_number, c.month_name
ORDER BY l.listing_id, c.year_number, c.month_number;


/* ---------------------------------------------------------------------------
   Q3. Average achieved nightly rate by season and weekend/weekday.
   Only booked nights matter here, so no calendar spine and no view --
   an inner join from STAY_NIGHT is the honest shape.
   booked_nights is shown because some buckets are thin and a mean over
   twelve nights deserves less trust than one over two hundred.
   --------------------------------------------------------------------------- */
SELECT s.listing_id,
       c.season,
       c.is_weekend,
       COUNT(*)              AS booked_nights,
       AVG(s.rate_night)     AS avg_nightly_rate
FROM STAY_NIGHT s
JOIN CALENDAR c ON c.date_key = s.night_date
GROUP BY s.listing_id, c.season, c.is_weekend
ORDER BY s.listing_id, c.season, c.is_weekend;


/* ---------------------------------------------------------------------------
   Q4. Booking lead time distribution (days between booking and arrival).
   Bucketed rather than averaged: lead time is heavily right-skewed and a
   handful of six-month-ahead bookings drag the mean well above typical.
   --------------------------------------------------------------------------- */
SELECT SUM(CASE WHEN lead_days <= 3                  THEN 1 ELSE 0 END) AS d0_3,
       SUM(CASE WHEN lead_days BETWEEN 4  AND 7      THEN 1 ELSE 0 END) AS d4_7,
       SUM(CASE WHEN lead_days BETWEEN 8  AND 14     THEN 1 ELSE 0 END) AS d8_14,
       SUM(CASE WHEN lead_days BETWEEN 15 AND 30     THEN 1 ELSE 0 END) AS d15_30,
       SUM(CASE WHEN lead_days BETWEEN 31 AND 90     THEN 1 ELSE 0 END) AS d31_90,
       SUM(CASE WHEN lead_days > 90                  THEN 1 ELSE 0 END) AS d90_plus
FROM (
    SELECT DATEDIFF(DAY, b.booked_on, b.check_in) AS lead_days
    FROM BOOKING b
) t;


/* ---------------------------------------------------------------------------
   Q4b. Lead time quartiles, all bookings.
   PERCENTILE_CONT is a window function in SQL Server -- it needs OVER() and
   returns a value per row, hence the DISTINCT.
   --------------------------------------------------------------------------- */
SELECT DISTINCT
       PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY lead_days) OVER () AS p25,
       PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY lead_days) OVER () AS median,
       PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY lead_days) OVER () AS p75,
       PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY lead_days) OVER () AS p90
FROM (
    SELECT DATEDIFF(DAY, b.booked_on, b.check_in) AS lead_days
    FROM BOOKING b
) t;


/* ---------------------------------------------------------------------------
   Q4c. Lead time quartiles for bookings that were later CANCELLED.
   Different population from Q4b -- do not compare the two directly without
   saying so. The question underneath: do last-minute bookings cancel more?
   --------------------------------------------------------------------------- */
SELECT DISTINCT
       PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY lead_days) OVER () AS p25,
       PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY lead_days) OVER () AS median,
       PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY lead_days) OVER () AS p75,
       PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY lead_days) OVER () AS p90
FROM (
    SELECT DATEDIFF(DAY, b.booked_on, b.check_in) AS lead_days
    FROM BOOKING b
    JOIN BOOKING_HISTORY h ON h.booking_id = b.booking_id
    WHERE h.action_type = 'cancelled'
) t;


/* ---------------------------------------------------------------------------
   Q5. Cancellation rate, and how much notice cancellations gave.
   Denominator is the count of 'confirmed' rows: every booking has exactly
   one, so it counts bookings without needing a separate join.
   NOTE: notice_days is cancellation notice, NOT booking lead time (Q4).
   --------------------------------------------------------------------------- */
SELECT CAST(SUM(CASE WHEN h.action_type = 'cancelled' THEN 1 ELSE 0 END) AS DECIMAL(8,4))
           / SUM(CASE WHEN h.action_type = 'confirmed' THEN 1 ELSE 0 END)  AS cancellation_rate,
       AVG(CASE WHEN h.action_type = 'cancelled'
                THEN DATEDIFF(DAY, CAST(h.action_date AS DATE), b.check_in)
           END)                                                            AS avg_notice_days,
       MIN(CASE WHEN h.action_type = 'cancelled'
                THEN DATEDIFF(DAY, CAST(h.action_date AS DATE), b.check_in)
           END)                                                            AS min_notice_days
FROM BOOKING b
JOIN BOOKING_HISTORY h ON h.booking_id = b.booking_id;


/* ---------------------------------------------------------------------------
   Q6. Rank each booking by net revenue within its listing and month.
   RANK, not ROW_NUMBER: ties should share a rank rather than being
   separated arbitrarily.
   Partition by what defines the competition; order by what decides it.
   --------------------------------------------------------------------------- */
SELECT b.listing_id,
       YEAR(b.check_in)   AS check_in_year,
       MONTH(b.check_in)  AS check_in_month,
       b.booking_id,
       b.confirmation_code,
       p.net_amount       AS revenue,
       RANK() OVER (PARTITION BY b.listing_id, YEAR(b.check_in), MONTH(b.check_in)
                    ORDER BY p.net_amount DESC) AS rank_in_month
FROM PAYOUT p
JOIN BOOKING b ON b.booking_id = p.booking_id
ORDER BY b.listing_id, check_in_year, check_in_month, rank_in_month;


/* ---------------------------------------------------------------------------
   Q7. Running (cumulative) revenue by listing over time.
   PARTITION BY listing only -- adding year/month to the partition would make
   every partition one row and no accumulation would happen.
   The default frame for SUM() OVER (ORDER BY ...) is
   RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW, which is what a running
   total needs.
   --------------------------------------------------------------------------- */
SELECT t.listing_id,
       t.year_number,
       t.month_name,
       t.total_net,
       SUM(t.total_net) OVER (PARTITION BY t.listing_id
                              ORDER BY t.year_number, t.month_number) AS cumulative_net
FROM (
    SELECT l.listing_id,
           c.year_number,
           c.month_number,
           c.month_name,
           COALESCE(SUM(p.net_amount), 0) AS total_net
    FROM CALENDAR c
    CROSS JOIN LISTING l
    LEFT JOIN BOOKING b ON b.check_in = c.date_key AND b.listing_id = l.listing_id
    LEFT JOIN PAYOUT  p ON p.booking_id = b.booking_id
    WHERE c.date_key >= l.listed_from
    GROUP BY l.listing_id, c.year_number, c.month_number, c.month_name
) t
ORDER BY t.listing_id, t.year_number, t.month_number;


/* ---------------------------------------------------------------------------
   Q8. Month-over-month revenue change.
   No default value on LAG: the first month per listing correctly returns
   NULL. Supplying 0 would imply revenue grew from nothing, which is a
   claim the data does not support.
   --------------------------------------------------------------------------- */
SELECT z.listing_id,
       z.year_number,
       z.month_name,
       z.total_net,
       LAG(z.total_net) OVER (PARTITION BY z.listing_id
                              ORDER BY z.year_number, z.month_number)    AS prev_month_net,
       z.total_net - LAG(z.total_net) OVER (PARTITION BY z.listing_id
                              ORDER BY z.year_number, z.month_number)    AS change_net
FROM (
    SELECT l.listing_id,
           c.year_number,
           c.month_number,
           c.month_name,
           COALESCE(SUM(p.net_amount), 0) AS total_net
    FROM CALENDAR c
    CROSS JOIN LISTING l
    LEFT JOIN BOOKING b ON b.check_in = c.date_key AND b.listing_id = l.listing_id
    LEFT JOIN PAYOUT  p ON p.booking_id = b.booking_id
    WHERE c.date_key >= l.listed_from
    GROUP BY l.listing_id, c.year_number, c.month_number, c.month_name
) z
ORDER BY z.listing_id, z.year_number, z.month_number;


/* ---------------------------------------------------------------------------
   Q9. Gap analysis: nights that were for sale and did not sell.
   Only answerable with a calendar -- an empty night has no row anywhere in
   the operational tables, so it can only be found by comparing against a
   complete list of dates.
   --------------------------------------------------------------------------- */
SELECT v.listing_id,
       v.date_key,
       v.year_number,
       v.month_name
FROM vw_available_night v
WHERE NOT EXISTS (
        SELECT 1
        FROM STAY_NIGHT s
        WHERE s.listing_id = v.listing_id
          AND s.night_date = v.date_key
      )
  AND v.year_number = 2025
ORDER BY v.listing_id, v.date_key;


/* ---------------------------------------------------------------------------
   Q10. Current stock level per item, with reorder flag.
   A plain aggregate -- "current level" has no date, it is the sum of every
   movement ever. No window function needed.
   --------------------------------------------------------------------------- */
SELECT i.item_id,
       i.item_name,
       i.item_unit,
       COALESCE(m.current_level, 0)  AS current_level,
       i.reorder_level,
       CASE
         WHEN i.reorder_level IS NULL                      THEN 'no threshold set'
         WHEN COALESCE(m.current_level, 0) <= i.reorder_level THEN 'reorder'
         ELSE 'ok'
       END                            AS reorder_status
FROM ITEM i
LEFT JOIN (
    SELECT item_id, SUM(quantity_change) AS current_level
    FROM STOCK_MOVEMENT
    GROUP BY item_id
) m ON m.item_id = i.item_id
ORDER BY i.item_id;


/* ---------------------------------------------------------------------------
   Q11. Stock balance over time, and where it went negative.
   The movement_id tiebreaker matters: several movements share a date, and
   without it the running total is non-deterministic.
   A negative balance is physically impossible, so any row here is a data
   error -- see 30_data_quality.sql, check 1.
   Window functions are computed after WHERE, so the filter needs the
   subquery wrapper.
   --------------------------------------------------------------------------- */
SELECT t.*
FROM (
    SELECT sm.item_id,
           sm.movement_id,
           sm.movement_date,
           sm.movement_type,
           sm.quantity_change,
           SUM(sm.quantity_change) OVER (PARTITION BY sm.item_id
                                         ORDER BY sm.movement_date, sm.movement_id) AS balance
    FROM STOCK_MOVEMENT sm
) t
WHERE t.balance < 0
ORDER BY t.item_id, t.movement_date;
