# ${hash_comment_marker}
CREATE TABLE smoke.sqlx_lexical_safety AS
SELECT "prefix ${double_marker} suffix" AS double_value,
       '${single_marker}' AS single_value,
       r'''${raw_triple_marker}''' AS raw_triple_value,
       1 AS id # config {
FROM UNNEST([1]);
---
  ---
-- ---
---- 
--- description
