-- CHECK 1:FACT_EXPENSE matches vw_expense_monthly, per listing, up to this month (expect no rows)
WITH v AS (
    SELECT listing_id, SUM(amount) AS total
    FROM vw_expense_monthly
    WHERE month_start <= DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1)
    GROUP BY listing_id),
f AS (
    SELECT l.listing_id, SUM(fe.amount) AS total
    FROM FACT_EXPENSE fe JOIN DIM_LISTING l ON l.listing_key = fe.listing_key
    GROUP BY l.listing_id)
SELECT v.listing_id, v.total AS view_total, f.total AS star_total
FROM v FULL JOIN f ON f.listing_id = v.listing_id
WHERE ISNULL(v.total, 0) <> ISNULL(f.total, 0);

GO