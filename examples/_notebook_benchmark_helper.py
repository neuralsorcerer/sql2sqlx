# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""Shared benchmark-data generator used by the example notebooks."""

from pathlib import Path

STMT = """\
-- rollup step {i} for shard {shard}
CREATE OR REPLACE TABLE wh_{shard}.t{i}
PARTITION BY DATE(ts)
CLUSTER BY customer_id
OPTIONS(description = "step {i}", labels = [("shard", "{shard}")])
AS
SELECT customer_id, ts, amount * {i} AS amount, 'step-{i}' AS tag
FROM wh_{shard}.t{prev}
WHERE ts >= TIMESTAMP '2024-01-01 00:00:00'
  AND amount IS NOT NULL;
"""

HEAD = """\
CREATE OR REPLACE TABLE wh_{shard}.t0 AS
SELECT '' AS customer_id, CURRENT_TIMESTAMP() AS ts, 0 AS amount;
INSERT INTO wh_{shard}.appended (customer_id, total)
SELECT customer_id, SUM(amount) FROM wh_{shard}.t0 GROUP BY customer_id;
"""


def generate(root: Path, files: int, statements: int) -> int:
    """Write a synthetic SQL corpus and return its line count."""
    lines = 0
    for shard in range(files):
        parts = [HEAD.format(shard=shard)]
        for i in range(1, statements + 1):
            parts.append(STMT.format(i=i, prev=i - 1, shard=shard))
        text = "\n".join(parts)
        path = root / f"shard_{shard:04d}" / "pipeline.sql"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        lines += text.count("\n") + 1
    return lines
