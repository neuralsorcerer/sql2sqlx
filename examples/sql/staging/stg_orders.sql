-- Orders staged from the raw event stream.
CREATE OR REPLACE TABLE staging.orders
PARTITION BY DATE(ts)
CLUSTER BY customer_id
OPTIONS(
  description = "One row per order event",
  labels = [("layer", "staging"), ("owner", "data-eng")],
  partition_expiration_days = 365,
  require_partition_filter = true
)
AS
SELECT
  event_id AS order_id,
  customer_id,
  amount,
  ts
FROM raw.events
WHERE event_type = 'order';


