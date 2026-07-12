# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""Example 1: convert an in-memory SQL string.

The simplest possible use - one CTAS plus a downstream INSERT - showing
the two most important behaviors: metadata extraction into the config
block, and automatic ``${ref(...)}`` wiring between statements.

Run:  python examples/01_convert_string.py
"""

from sql2sqlx import convert_string

SQL = """
CREATE OR REPLACE TABLE analytics.daily_orders
PARTITION BY DATE(order_ts)
OPTIONS(description = "Daily order rollup")
AS
SELECT DATE(order_ts) AS d, COUNT(*) AS n
FROM raw.orders
GROUP BY 1;

INSERT INTO analytics.order_history (d, n)
SELECT d, n FROM analytics.daily_orders;
"""


def main() -> None:
    """Convert the SQL above and print every generated ``.sqlx`` file."""
    result = convert_string(SQL, name="pipeline.sql")
    for sqlx in result.files:
        print(f"===== {sqlx.relpath} ({sqlx.action_type.value}) =====")
        print(sqlx.content)
    print(f"refs rewritten: {result.report.refs_rewritten}")


if __name__ == "__main__":
    main()
