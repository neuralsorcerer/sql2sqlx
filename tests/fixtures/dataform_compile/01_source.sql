-- ${comment_marker} must remain inert SQL text.
CREATE OR REPLACE TABLE smoke.source AS
SELECT 1 AS id, '${string_marker}' AS marker, `${identifier_marker}` AS identifier_value;

