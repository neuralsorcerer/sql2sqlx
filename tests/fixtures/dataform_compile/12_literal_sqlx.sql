-- A literal SQLX marker in a SQL comment must remain inert.
-- ${comment_marker}
CREATE TABLE smoke.literal_markers AS
SELECT '${string_marker}' AS string_value,
       1 AS `${identifier_marker}`;
