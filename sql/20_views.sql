CREATE OR ALTER VIEW vw_available_night AS
   SELECT c.date_key, c.month_name, c.month_number, c.year_number, c.season, c.is_weekend, l.listing_id FROM CALENDAR c 
    CROSS JOIN LISTING l WHERE NOT EXISTS (SELECT 1 FROM BLOCKED_PERIOD bp WHERE l.listing_id = bp.listing_id AND c.date_key >= bp.blocked_start_date AND c.date_key <= bp.blocked_end_date)
    AND c.date_key >= l.listed_from;

GO

CREATE OR ALTER VIEW vw_receipt_fields AS
SELECT
    r.receipt_id,
    r.extraction_model,

    -- total: field name depends on the model
    TRY_CAST(
        CASE r.extraction_model
            WHEN 'prebuilt-invoice'
                THEN JSON_VALUE(r.extraction_json, '$.documents[0].fields.InvoiceTotal.valueCurrency.amount')
            ELSE     JSON_VALUE(r.extraction_json, '$.documents[0].fields.Total.valueCurrency.amount')
        END AS DECIMAL(10,2))                                   AS total,
    TRY_CAST(
        CASE r.extraction_model
            WHEN 'prebuilt-invoice'
                THEN JSON_VALUE(r.extraction_json, '$.documents[0].fields.InvoiceTotal.confidence')
            ELSE     JSON_VALUE(r.extraction_json, '$.documents[0].fields.Total.confidence')
        END AS DECIMAL(4,3))                                    AS total_confidence,

        TRY_CAST(
        CASE r.extraction_model
            WHEN 'prebuilt-invoice'
                THEN JSON_VALUE(r.extraction_json, '$.documents[0].fields.InvoiceDate.valueDate')
            ELSE     JSON_VALUE(r.extraction_json, '$.documents[0].fields.TransactionDate.valueDate')
        END AS DATE)                                            AS transaction_date,

    TRY_CAST(
        CASE r.extraction_model
            WHEN 'prebuilt-invoice'
                THEN JSON_VALUE(r.extraction_json, '$.documents[0].fields.InvoiceDate.confidence')
            ELSE     JSON_VALUE(r.extraction_json, '$.documents[0].fields.TransactionDate.confidence')
        END AS DECIMAL(4,3))                                    AS transaction_date_confidence,
    TRY_CAST(
        JSON_VALUE(r.extraction_json, '$.documents[0].fields.TotalTax.valueCurrency.amount') AS DECIMAL(10, 2)
    )                                                           AS gst,
    CASE r.extraction_model
        WHEN 'prebuilt-invoice'
            THEN JSON_VALUE(r.extraction_json, '$.documents[0].fields.InvoiceId.valueString')
        ELSE     NULL
    END                              AS invoice_number,
    CASE r.extraction_model
        WHEN 'prebuilt-invoice'
            THEN REPLACE(JSON_VALUE(r.extraction_json,
                 '$.documents[0].fields.VendorTaxId.valueString'), ' ', '')
    END                                                     AS abn,

    CASE r.extraction_model
        WHEN 'prebuilt-invoice'
            THEN REPLACE(JSON_VALUE(r.extraction_json, '$.documents[0].fields.VendorName.valueString'), CHAR(10), ' ')
        ELSE     REPLACE(JSON_VALUE(r.extraction_json, '$.documents[0].fields.MerchantName.valueString'), CHAR(10), ' ')
    END                              AS merchant_name,
    TRY_CAST(
        CASE r.extraction_model
            WHEN 'prebuilt-invoice'
                THEN JSON_VALUE(r.extraction_json, '$.documents[0].fields.VendorName.confidence')
            ELSE     JSON_VALUE(r.extraction_json, '$.documents[0].fields.MerchantName.confidence')
        END AS DECIMAL(4,3))                               AS merchant_name_confidence,

    TRY_CAST(
        CASE r.extraction_model
            WHEN 'prebuilt-invoice'
                THEN JSON_VALUE(r.extraction_json, '$.documents[0].fields.SubTotal.valueCurrency.amount')
            ELSE     JSON_VALUE(r.extraction_json, '$.documents[0].fields.Subtotal.valueCurrency.amount')
        END AS DECIMAL(10,2))                                   AS subtotal,
    -- the date exactly as printed, for reparsing month-first senders
    CASE r.extraction_model
        WHEN 'prebuilt-invoice'
            THEN JSON_VALUE(r.extraction_json, '$.documents[0].fields.InvoiceDate.content')
        ELSE     JSON_VALUE(r.extraction_json, '$.documents[0].fields.TransactionDate.content')
    END                                                         AS transaction_date_text,

    -- confidence of a second document, if the model found one
    TRY_CAST(JSON_VALUE(r.extraction_json, '$.documents[1].confidence')
             AS DECIMAL(4,3))                                   AS second_doc_confidence,
    
    TRY_CAST(JSON_VALUE(r.extraction_json, '$.documents[0].fields.TransactionTime.valueTime')
             AS TIME(0))                                        AS transaction_time
          
FROM RECEIPT r
WHERE r.status <> 'failed'
  AND r.extraction_json IS NOT NULL;


GO

CREATE OR ALTER VIEW vw_receipt_line_items AS
SELECT
    r.receipt_id,
    CAST(i.[key] AS INT) + 1                                    AS line_no,
    TRY_CAST(
        CASE r.extraction_model
            WHEN 'prebuilt-invoice'
                THEN JSON_VALUE(i.value, '$.valueObject.Amount.valueCurrency.amount')
            ELSE     JSON_VALUE(i.value, '$.valueObject.TotalPrice.valueCurrency.amount')
        END AS DECIMAL(10,2))                                   AS amount,
    REPLACE(JSON_VALUE(i.value, '$.valueObject.Description.valueString'), CHAR(10), ' ')
                                                                AS description,
    JSON_VALUE(i.value, '$.valueObject.ProductCode.valueString')  AS product_code,
    TRY_CAST(
        CASE r.extraction_model
            WHEN 'prebuilt-invoice'
                THEN COALESCE(JSON_VALUE(i.value, '$.valueObject.Quantity.valueNumber'),
                              JSON_VALUE(i.value, '$.valueObject.OrderQuantity.valueNumber'))
            ELSE     JSON_VALUE(i.value, '$.valueObject.Quantity.valueNumber')
        END AS DECIMAL(10,3))                                   AS quantity,
    TRY_CAST(
        CASE r.extraction_model
            WHEN 'prebuilt-invoice'
                THEN JSON_VALUE(i.value, '$.valueObject.UnitPrice.valueCurrency.amount')
            ELSE     JSON_VALUE(i.value, '$.valueObject.Price.valueCurrency.amount')
        END AS DECIMAL(10,2))                                   AS unit_price,
    TRY_CAST(JSON_VALUE(i.value, '$.confidence') AS DECIMAL(4,3)) AS line_confidence
FROM RECEIPT r
CROSS APPLY OPENJSON(r.extraction_json, '$.documents[0].fields.Items.valueArray') i
WHERE r.status <> 'failed'
  AND r.extraction_json IS NOT NULL;

GO

CREATE OR ALTER VIEW vw_expense_spread AS
-- Each expense's FULL amount placed in the months it covers, before any
-- apportionment:
--   capital          -> CATEGORY.capital_life_months (default 60), from purchase
--   spread category  -> CATEGORY.spread_months; anchor 'start' = the bill
--                       date begins the period (paid in advance), 'end' = it
--                       ends it (billed in arrears)
--   anything else    -> 1 month: the month of the expense
-- Month k gets round(amount*k/n) - round(amount*(k-1)/n), so the months
-- always add up to the exact amount. Management figures, not tax depreciation.
SELECT
    e.expense_id,
    e.scope,
    e.property_id,
    e.listing_id,
    e.category,
    e.expense_type,
    e.verified_by_user,
    c.date_key                                                       AS month_start,
    CAST(ROUND(e.expense_amount * m.k       / s.months, 2)
       - ROUND(e.expense_amount * (m.k - 1) / s.months, 2) AS DECIMAL(10,2)) AS amount
FROM EXPENSE e
JOIN CATEGORY cat ON cat.category_code = e.category
CROSS APPLY (SELECT
        CASE WHEN e.expense_type = 'capital' THEN ISNULL(cat.capital_life_months, 60)
             ELSE ISNULL(cat.spread_months, 1) END                   AS months,
        CASE WHEN e.expense_type = 'capital' THEN 'start'
             ELSE ISNULL(cat.spread_anchor, 'start') END              AS anchor,
        DATEFROMPARTS(YEAR(e.expense_date), MONTH(e.expense_date), 1) AS expense_month) s
CROSS APPLY (SELECT
        CASE WHEN s.anchor = 'end' THEN DATEADD(MONTH, 1 - s.months, s.expense_month)
             ELSE s.expense_month END                                 AS first_month) f
JOIN CALENDAR c
  ON  c.day_of_month = 1
  AND c.date_key >= f.first_month
  AND c.date_key <  DATEADD(MONTH, s.months, f.first_month)
CROSS APPLY (SELECT DATEDIFF(MONTH, f.first_month, c.date_key) + 1 AS k) m;

GO

CREATE OR ALTER VIEW vw_expense_monthly AS
-- Each listing's share of each expense, by month. The single place shares
-- are calculated, month by month:
--   listing scope  -> the whole monthly amount to the expense's listing
--   property scope -> each month split among the listings OF THE EXPENSE'S
--                     PROPERTY, by the APPORTIONMENT_RULE rates in force on
--                     the last day of that month; months with no rule (e.g.
--                     before a listing opened) contribute nothing
-- A property expense shared in no month at all is caught by the assertion
-- in 30_data_quality.sql.
SELECT s.expense_id, s.listing_id, s.month_start, s.category, s.expense_type,
       s.verified_by_user, s.amount, CAST(1 AS DECIMAL(5,4)) AS share_rate
FROM vw_expense_spread s
WHERE s.scope = 'listing'

UNION ALL

SELECT s.expense_id, r.listing_id, s.month_start, s.category, s.expense_type,
       s.verified_by_user, CAST(s.amount * r.rate AS DECIMAL(10,2)), r.rate
FROM vw_expense_spread s
JOIN APPORTIONMENT_RULE r
  ON  r.category   = s.category
  AND r.valid_from <= EOMONTH(s.month_start)
  AND (r.valid_to IS NULL OR r.valid_to > EOMONTH(s.month_start))
JOIN LISTING rl
  ON  rl.listing_id  = r.listing_id
  AND rl.property_id = s.property_id          -- only this property's listings
WHERE s.scope = 'property';

GO

CREATE OR ALTER VIEW vw_expense_attributed AS
-- Each listing's total share of each expense: the sum of its monthly shares
-- in vw_expense_monthly, where shares are calculated. Kept as a per-expense
-- summary for checks and reports.
SELECT m.expense_id, m.listing_id, e.expense_date, e.category, e.expense_type,
       e.scope, e.supplier, e.receipt_id, e.verified_by_user,
       e.expense_amount AS full_amount,
       SUM(m.amount)    AS attributed_amount
FROM vw_expense_monthly m
JOIN EXPENSE e ON e.expense_id = m.expense_id
GROUP BY m.expense_id, m.listing_id, e.expense_date, e.category, e.expense_type,
         e.scope, e.supplier, e.receipt_id, e.verified_by_user, e.expense_amount;

GO

CREATE OR ALTER VIEW vw_monthly_profit AS
-- Operating profit per listing per month:
--   revenue   = net payout, spread evenly over the booking's nights and
--               counted in the month each night was stayed. A payout for a
--               booking with no nights (e.g. a cancellation fee) counts in
--               the month it was paid.
--   expenses  = the listing's expense shares, capital spread (vw_expense_monthly)
--   cleaning  = CLEANING.cleaning_cost, in the month of the clean
-- Loan interest is applied in reports only (vw_monthly_report).
-- Months after the current one are excluded: a future month can only
-- show spread capital costs, never revenue.
WITH booking_net AS (
    SELECT booking_id, SUM(net_amount) AS net_amount, MIN(payout_date) AS payout_date
    FROM PAYOUT
    GROUP BY booking_id
),
booking_nights AS (
    SELECT booking_id, COUNT(*) AS nights
    FROM STAY_NIGHT
    GROUP BY booking_id
),
revenue AS (
    SELECT s.listing_id,
           DATEFROMPARTS(YEAR(s.night_date), MONTH(s.night_date), 1)   AS month_start,
           CAST(bn.net_amount AS DECIMAL(18,6)) / n.nights              AS amount
    FROM STAY_NIGHT s
    JOIN booking_net    bn ON bn.booking_id = s.booking_id
    JOIN booking_nights n  ON n.booking_id  = s.booking_id

    UNION ALL

    SELECT b.listing_id,
           DATEFROMPARTS(YEAR(bn.payout_date), MONTH(bn.payout_date), 1),
           bn.net_amount
    FROM booking_net bn
    JOIN BOOKING b ON b.booking_id = bn.booking_id
    WHERE NOT EXISTS (SELECT 1 FROM STAY_NIGHT s WHERE s.booking_id = bn.booking_id)
),
revenue_month AS (
    SELECT listing_id, month_start, SUM(amount) AS revenue
    FROM revenue
    GROUP BY listing_id, month_start
),
nights_month AS (
    SELECT listing_id, DATEFROMPARTS(YEAR(night_date), MONTH(night_date), 1) AS month_start,
           COUNT(*) AS nights_sold
    FROM STAY_NIGHT
    GROUP BY listing_id, DATEFROMPARTS(YEAR(night_date), MONTH(night_date), 1)
),
expense_month AS (
    SELECT listing_id, month_start,
           SUM(amount)                                               AS expenses,
           SUM(CASE WHEN verified_by_user = 0 THEN amount ELSE 0 END) AS unverified_expenses
    FROM vw_expense_monthly
    GROUP BY listing_id, month_start
),
cleaning_month AS (
    SELECT b.listing_id,
           DATEFROMPARTS(YEAR(c.cleaning_date), MONTH(c.cleaning_date), 1) AS month_start,
           SUM(c.cleaning_cost) AS cleaning
    FROM CLEANING c
    JOIN BOOKING b ON b.booking_id = c.booking_id
    GROUP BY b.listing_id, DATEFROMPARTS(YEAR(c.cleaning_date), MONTH(c.cleaning_date), 1)
),
months AS (
    SELECT listing_id, month_start FROM revenue_month
    UNION SELECT listing_id, month_start FROM nights_month
    UNION SELECT listing_id, month_start FROM expense_month
    UNION SELECT listing_id, month_start FROM cleaning_month
)
SELECT
    m.listing_id,
    m.month_start,
    ISNULL(n.nights_sold, 0)                                         AS nights_sold,
    CAST(ISNULL(r.revenue, 0) AS DECIMAL(10,2))                      AS revenue,
    ISNULL(e.expenses, 0)                                            AS expenses,
    ISNULL(c.cleaning, 0)                                            AS cleaning,
    CAST(ISNULL(r.revenue, 0) AS DECIMAL(10,2))
        - ISNULL(e.expenses, 0) - ISNULL(c.cleaning, 0)              AS operating_profit,
    ISNULL(e.unverified_expenses, 0)                                 AS unverified_expenses
FROM months m
LEFT JOIN revenue_month  r ON r.listing_id = m.listing_id AND r.month_start = m.month_start
LEFT JOIN nights_month   n ON n.listing_id = m.listing_id AND n.month_start = m.month_start
LEFT JOIN expense_month  e ON e.listing_id = m.listing_id AND e.month_start = m.month_start
LEFT JOIN cleaning_month c ON c.listing_id = m.listing_id AND c.month_start = m.month_start
WHERE m.month_start <= DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1);

GO


CREATE OR ALTER VIEW vw_monthly_report AS
-- Monthly profit per listing, after its share of its property's loan
-- interest. The share is LISTING.loan_interest_share and the
-- loans are LOAN rows for the listing's property, both from the config.
-- A listing carries interest only from the month it was listed: before
-- that, the property's loans weren't funding a rental.
--
-- Interest is counted in the months it accrued: a charge on day d of month
-- M covers day d of month M-1 to day d-1 of M, split between the two months
-- by days. A month is complete only when every active loan of the property
-- has its charges for this month AND next month entered (interest_missing = 0).
-- revenue_complete = 1 once an earnings export from after the month's end is loaded.
WITH charges AS (
    SELECT l.property_id,
           li.month_start                                         AS charge_month,
           DATEADD(MONTH, -1, li.month_start)                     AS previous_month,
           li.interest_amount,
           l.charge_day,
           DAY(EOMONTH(DATEADD(MONTH, -1, li.month_start)))       AS period_days
    FROM LOAN_INTEREST li
    JOIN LOAN l ON l.loan_code = li.loan_code
),
accrued AS (
    SELECT property_id, previous_month AS month_start,
           CAST(interest_amount AS DECIMAL(18,6))
             * (period_days - charge_day + 1) / period_days       AS amount
    FROM charges
    UNION ALL
    SELECT property_id, charge_month,
           CAST(interest_amount AS DECIMAL(18,6))
             * (charge_day - 1) / period_days
    FROM charges
),
interest_month AS (
    SELECT property_id, month_start, SUM(amount) AS interest
    FROM accrued
    GROUP BY property_id, month_start
),
completeness AS (
    SELECT l.property_id,
           c.date_key AS month_start,
           SUM(CASE WHEN this_month.loan_code IS NULL OR next_month.loan_code IS NULL
                    THEN 1 ELSE 0 END)                            AS loans_incomplete
    FROM CALENDAR c
    JOIN LOAN l
      ON  l.active_from <= c.date_key
      AND (l.active_to IS NULL OR l.active_to > c.date_key)
    LEFT JOIN LOAN_INTEREST this_month
      ON  this_month.loan_code = l.loan_code AND this_month.month_start = c.date_key
    LEFT JOIN LOAN_INTEREST next_month
      ON  next_month.loan_code = l.loan_code
      AND next_month.month_start = DATEADD(MONTH, 1, c.date_key)
    WHERE c.day_of_month = 1
    GROUP BY l.property_id, c.date_key
),
latest_paid AS (
    SELECT MAX(export_date) AS export_date FROM LOAD_LOG WHERE file_kind = 'paid'
)
SELECT
    p.listing_id,
    p.month_start,
    p.nights_sold,
    p.revenue,
    p.expenses,
    p.cleaning,
    p.operating_profit,
    CAST(ISNULL(im.interest * ls.loan_interest_share, 0) AS DECIMAL(10,2))      AS loan_interest,
    p.operating_profit
      - CAST(ISNULL(im.interest * ls.loan_interest_share, 0) AS DECIMAL(10,2))  AS profit_after_interest,
    CASE WHEN ls.loan_interest_share IS NOT NULL AND live.listed = 1
              AND ISNULL(cm.loans_incomplete, 0) > 0
         THEN 1 ELSE 0 END                                                     AS interest_missing,
    CASE WHEN lp.export_date > EOMONTH(p.month_start) THEN 1 ELSE 0 END        AS revenue_complete,
    p.unverified_expenses
FROM vw_monthly_profit p
JOIN LISTING ls ON ls.listing_id = p.listing_id
-- Is this month on or after the month the listing was listed?
CROSS APPLY (SELECT CASE WHEN p.month_start >= DATEFROMPARTS(YEAR(ls.listed_from),
                                                            MONTH(ls.listed_from), 1)
                         THEN 1 ELSE 0 END AS listed) live
LEFT JOIN interest_month im ON im.property_id = ls.property_id
                           AND im.month_start = p.month_start
                           AND live.listed = 1
LEFT JOIN completeness   cm ON cm.property_id = ls.property_id AND cm.month_start = p.month_start
CROSS JOIN latest_paid lp;
GO

CREATE OR ALTER VIEW vw_ical_changes AS
-- Booking changes detected between consecutive parsed snapshots of each
-- listing's calendar, matched on confirmation code. Derived entirely from
-- ICAL_EVENT, so nothing here is stored.
--   new_booking          absent before, present now
--   cancelled            present before and not yet started; absent now
--   removed_during_stay  present before and in progress; absent now
--   dates_changed        present in both with different dates
-- A stay that has ended disappears from the feed normally and is ignored.
-- seen_on is the snapshot's date in Adelaide: fetched_at is UTC, and the
-- 10:00 run falls on the previous UTC day.
WITH snapshots AS (
    SELECT snapshot_id,
           listing_id,
           CAST(fetched_at AT TIME ZONE 'UTC'
                           AT TIME ZONE 'Cen. Australia Standard Time' AS DATE) AS seen_on,
           LAG(snapshot_id) OVER (PARTITION BY listing_id ORDER BY fetched_at)  AS previous_id
    FROM ICAL_SNAPSHOT
    WHERE parsed_at IS NOT NULL
),
compared AS (
    SELECT s.listing_id,
           s.snapshot_id,
           s.seen_on,
           COALESCE(n.confirmation_code, o.confirmation_code) AS confirmation_code,
           o.start_date AS old_start, o.end_date AS old_end,
           n.start_date AS new_start, n.end_date AS new_end
    FROM snapshots s
    CROSS APPLY (
        SELECT o.confirmation_code AS o_code, n.confirmation_code AS n_code,
               o.start_date AS o_start, o.end_date AS o_end,
               n.start_date AS n_start, n.end_date AS n_end
        FROM (SELECT confirmation_code, start_date, end_date FROM ICAL_EVENT
              WHERE snapshot_id = s.previous_id AND event_kind = 'reserved') o
        FULL JOIN
             (SELECT confirmation_code, start_date, end_date FROM ICAL_EVENT
              WHERE snapshot_id = s.snapshot_id AND event_kind = 'reserved') n
          ON n.confirmation_code = o.confirmation_code
    ) x
    CROSS APPLY (SELECT x.o_code AS confirmation_code, x.o_start AS start_date, x.o_end AS end_date) o
    CROSS APPLY (SELECT x.n_code AS confirmation_code, x.n_start AS start_date, x.n_end AS end_date) n
    WHERE s.previous_id IS NOT NULL
),
classified AS (
    SELECT c.*,
           CASE
               WHEN c.old_start IS NULL                                  THEN 'new_booking'
               WHEN c.new_start IS NULL AND c.old_start > c.seen_on      THEN 'cancelled'
               WHEN c.new_start IS NULL AND c.old_end   > c.seen_on      THEN 'removed_during_stay'
               WHEN c.new_start IS NOT NULL
                    AND (c.new_start <> c.old_start OR c.new_end <> c.old_end) THEN 'dates_changed'
           END AS change_type
    FROM compared c
)
SELECT listing_id, snapshot_id, seen_on, confirmation_code, change_type,
       old_start, old_end, new_start, new_end
FROM classified
WHERE change_type IS NOT NULL;

GO
