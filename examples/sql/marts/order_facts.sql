-- Appended daily by the scheduler; becomes a Dataform incremental.
INSERT INTO marts.order_facts (order_date, orders, revenue)
SELECT
  DATE(ts),
  COUNT(*),
  SUM(amount)
FROM staging.orders
WHERE DATE(ts) = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
GROUP BY 1;


