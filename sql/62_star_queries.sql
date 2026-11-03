
SELECT dl.listing_title, d.year_number, d.month_name,
       CAST(COUNT(f.date_key) AS DECIMAL(6,4)) / COUNT(a.date_key) AS occupancy_rate
FROM FACT_AVAILABLE_NIGHT a
JOIN DIM_DATE    d  ON d.date_key = a.date_key
JOIN DIM_LISTING dl ON dl.listing_key = a.listing_key
LEFT JOIN FACT_STAY_NIGHT f ON f.date_key = a.date_key AND f.listing_key = a.listing_key
GROUP BY dl.listing_title, d.year_number, d.month_number, d.month_name
ORDER BY dl.listing_title, d.year_number, d.month_number;

SELECT dl.listing_title, d.year_number, d.month_name,
       SUM(f.net_amount) AS revenue
FROM FACT_BOOKING f
JOIN DIM_DATE    d  ON d.date_key    = f.checked_in_date_key
JOIN DIM_LISTING dl ON dl.listing_key = f.listing_key
GROUP BY dl.listing_title, d.year_number, d.month_number, d.month_name
ORDER BY dl.listing_title, d.year_number, d.month_number;

SELECT fs.listing_key,  dd.season, dd.is_weekend, COUNT(*) AS bookded_nights, AVG(fs.rate_night) AS avg_nightly_rate
FROM FACT_STAY_NIGHT fs 
JOIN DIM_DATE dd ON fs.date_key = dd.date_key
GROUP BY fs.listing_key, dd.season, dd.is_weekend
ORDER BY fs.listing_key, dd.season, dd.is_weekend;