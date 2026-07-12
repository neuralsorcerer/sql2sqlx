INSERT INTO smoke.incremental_target (`${value}`, id)
SELECT '${row_marker}', id
FROM smoke.source;

