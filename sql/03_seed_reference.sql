
WITH DateSeries AS (
    SELECT CAST('2022-01-01' AS DATE) AS CurrentDate
    UNION ALL
    SELECT DATEADD(DAY, 1, CurrentDate)
    FROM DateSeries
    WHERE CurrentDate < '2040-12-31'
)
INSERT INTO CALENDAR (
    date_key,
    year_number,
    quarter_number,
    month_number,
    month_name,
    day_of_month,
    day_of_week,
    day_name,
    week_of_year,
    is_weekend,
    season
) SELECT CurrentDate AS date_key,
YEAR(CurrentDate) AS year_number,
DATEPART(QUARTER, CurrentDate) AS quarter_number,
MONTH(CurrentDate) AS month_number,
DATENAME(MONTH, CurrentDate) AS month_name,
DAY(CurrentDate) AS day_of_month,
DATEPART(WEEKDAY, CurrentDate) AS day_of_week,
DATENAME(WEEKDAY, CurrentDate) AS day_name,
DATEPART(WEEK, CurrentDate) AS week_of_year,
CASE
    WHEN DATENAME(WEEKDAY, CurrentDate) = 'Saturday' THEN 1
    WHEN DATENAME(WEEKDAY, CurrentDate) = 'Sunday' THEN 1
    ELSE 0 END AS is_weekend,
CASE
    WHEN MONTH(CurrentDate) <= 5 AND MONTH(CurrentDate) >= 3 THEN 'autumn'
    WHEN MONTH(CurrentDate) <= 8 AND MONTH(CurrentDate) >= 6  THEN 'winter'
    WHEN MONTH(CurrentDate) <= 11 AND MONTH(CurrentDate) >= 9 THEN 'spring'
    ELSE 'summer'
END AS season
FROM DateSeries
OPTION (MAXRECURSION 0);

GO

-- Expense categories: the application's vocabulary. Codes are referenced by
-- EXPENSE, APPORTIONMENT_RULE and MERCHANT_RULE; descriptions are also the
-- definitions given to the LLM.
INSERT INTO CATEGORY (category_code, description, default_expense_type) VALUES
    (N'electricity',   N'Electricity bills', N'periodic'),
    (N'water',         N'Water supply, water deliveries, and filters and parts for the suite''s water system; not bottled drinking water', N'periodic'),
    (N'internet',      N'Internet service', N'periodic'),
    (N'council_rates', N'Council rates', N'periodic'),
    (N'insurance',     N'Building and landlord insurance', N'periodic'),
    (N'linen',         N'Laundromat and linen services, and purchases of sheets, towels and other linen', N'direct'),
    (N'furnishings',   N'Furniture, homewares, appliances and fittings for the listing', N'capital'),
    (N'garden',        N'Garden maintenance, plants and gardening supplies', N'periodic'),
    (N'repairs',       N'Repair and maintenance work, parts and hardware for repairs', N'periodic'),
    (N'supplies',      N'Non-food consumables for the suite: cleaning products, laundry detergent, toiletries, toilet paper, bin bags', N'consumable'),
    (N'welcome_box',   N'Food and drink for guests: wine, chocolates, snacks, milk, breakfast items, coffee and tea', N'direct'),
    (N'other',         N'Anything not covered by another category', N'periodic');

UPDATE CATEGORY SET spread_months = 3,  spread_anchor = N'start' WHERE category_code = N'council_rates';
UPDATE CATEGORY SET spread_months = 12, spread_anchor = N'start' WHERE category_code = N'insurance';
UPDATE CATEGORY SET spread_months = 3,  spread_anchor = N'end'   WHERE category_code = N'electricity';

