# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""Example 4: the option knobs.

Demonstrates safe-MERGE conversion, external-source declarations, flat
layout, tags, and turning provenance annotations off - then prints the
MERGE-derived incremental and the synthesized declarations.

Run:  python examples/04_custom_options.py
"""

from pathlib import Path

from sql2sqlx import ConversionOptions, Layout, MergeStrategy, convert_directory

HERE = Path(__file__).parent


def main() -> None:
    """Convert the sample corpus with non-default options."""
    options = ConversionOptions(
        merge_strategy=MergeStrategy.INCREMENTAL_WHEN_SAFE,
        declare_external=True,  # declarations for raw.* references
        layout=Layout.FLAT,
        tags=["migrated"],
        annotate=False,
    )
    result = convert_directory(str(HERE / "sql"), options=options)

    for sqlx in result.files:
        if sqlx.action_name == "customer_attrs":
            print("--- MERGE proven safe -> incremental + uniqueKey ---")
            print(sqlx.content)
    # The sample corpus produces every table it reads, so nothing is
    # external there. This snippet references a table nobody creates:
    from sql2sqlx import convert_string

    ext = convert_string(
        "CREATE TABLE marts.fx AS " "SELECT * FROM ext_finance.currency_rates;",
        ConversionOptions(declare_external=True),
    )
    declarations = [f for f in ext.files if f.action_type.value == "declaration"]
    print("declarations synthesized:", [(d.relpath, d.action_name) for d in declarations])
    print(next(f for f in ext.files if f.action_name == "fx").content)


if __name__ == "__main__":
    main()
