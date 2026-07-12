/* Customer-level order rollup exposed to BI. */
CREATE OR REPLACE VIEW marts.customer_orders (
  customer_id,
  order_count OPTIONS(description = "Lifetime orders"),
  lifetime_value
) AS
SELECT
  c.customer_id,
  COUNT(o.order_id),
  SUM(o.amount)
FROM staging.customers c
LEFT JOIN staging.orders o USING (customer_id)
GROUP BY c.customer_id;


