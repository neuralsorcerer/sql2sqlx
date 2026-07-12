MERGE smoke.merged t
USING (SELECT id, value FROM smoke.incremental_target) s
ON t.id = s.id
WHEN MATCHED THEN UPDATE SET id = s.id, value = s.value
WHEN NOT MATCHED THEN INSERT (id, value) VALUES (s.id, s.value);
