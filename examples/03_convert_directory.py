# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""Example 3: convert a whole directory tree into a Dataform project.

Mirrors the layout of ``examples/sql/`` into ``examples/output/definitions``
and prints a summary - the programmatic equivalent of::

    sql2sqlx examples/sql -o examples/output/definitions

Run:  python examples/03_convert_directory.py
"""

import shutil
from pathlib import Path

from sql2sqlx import ConversionOptions, convert_directory

HERE = Path(__file__).parent
OUT = HERE / "output" / "definitions"


def main() -> None:
    """Convert the sample corpus and list what was produced."""
    shutil.rmtree(OUT.parent, ignore_errors=True)
    options = ConversionOptions(default_project="my-gcp-project")
    result = convert_directory(str(HERE / "sql"), str(OUT), options)

    print(f"wrote {len(result.files)} files under {OUT}")
    for sqlx in result.files:
        print(f"  {sqlx.relpath:40s} {sqlx.action_type.value}")
    print("actions:", result.report.actions_by_type)
    print("warnings:", len(result.report.warnings), "| failures:", len(result.report.failures))


if __name__ == "__main__":
    main()
