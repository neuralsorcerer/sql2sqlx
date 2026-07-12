-- Raw landing table, loaded by an external ingestion job.
CREATE TABLE IF NOT EXISTS raw.events (
  event_id STRING,
  customer_id STRING,
  event_type STRING,
  amount NUMERIC,
  ts TIMESTAMP
);


