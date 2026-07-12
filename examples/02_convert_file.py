# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""Example 2: convert a single .sql file and inspect the report.

Run:  python examples/02_convert_file.py
"""

from pathlib import Path

from sql2sqlx import convert_file

HERE = Path(__file__).parent


def main() -> None:
    """Convert the staged-orders example file and show its warnings."""
    result = convert_file(str(HERE / "sql" / "staging" / "stg_orders.sql"))
    sqlx = result.files[0]
    print(sqlx.content)
    print("--- report ---")
    print("action type :", sqlx.action_type.value)
    print("statements  :", result.report.statements)
    for warning in result.report.warnings:
        print(f"[{warning.code}] line {warning.line}: {warning.message}")


if __name__ == "__main__":
    main()
