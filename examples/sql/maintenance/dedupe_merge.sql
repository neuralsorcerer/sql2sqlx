-- Upsert latest customer attributes; provably convertible with
-- --merge-strategy incremental-when-safe.
MERGE staging.customer_attrs t
USING (
  SELECT customer_id, last_seen FROM staging.customers
) s
ON t.customer_id = s.customer_id
WHEN MATCHED THEN UPDATE SET customer_id = s.customer_id, last_seen = s.last_seen
WHEN NOT MATCHED THEN INSERT (customer_id, last_seen)
  VALUES (s.customer_id, s.last_seen);

