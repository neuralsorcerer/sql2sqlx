-- A BigQuery script: variables span statements, so the whole file
-- becomes a single operations action.
DECLARE cutoff DATE DEFAULT DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY);

DELETE FROM marts.order_facts WHERE order_date < cutoff;

INSERT INTO marts.order_facts (order_date, orders, revenue)
SELECT DATE(ts), COUNT(*), SUM(amount)
FROM staging.orders
WHERE DATE(ts) >= cutoff
GROUP BY 1;


