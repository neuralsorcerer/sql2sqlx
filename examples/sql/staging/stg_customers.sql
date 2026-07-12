CREATE OR REPLACE TABLE staging.customers AS
SELECT
  customer_id,
  MIN(ts) AS first_seen,
  MAX(ts) AS last_seen
FROM raw.events
GROUP BY customer_id;


