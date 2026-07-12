DELETE FROM staging.orders WHERE amount IS NULL;
UPDATE staging.customers SET last_seen = CURRENT_TIMESTAMP() WHERE FALSE;


