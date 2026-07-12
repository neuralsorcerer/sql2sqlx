# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""Example 5: machine-readable reports for CI gates.

Converts the sample corpus, dumps the JSON report, and shows how a CI
job might fail a migration when specific warning codes appear.

Run:  python examples/05_programmatic_report.py
"""

import json
from collections import Counter
from pathlib import Path

from sql2sqlx import convert_directory

HERE = Path(__file__).parent

#: Codes your team might treat as "needs human review" in CI.
BLOCKING = {"FALLBACK_OPERATIONS", "DUPLICATE_TARGET", "SELF_REFERENCE"}


def main() -> None:
    """Convert, summarize warning codes, emit the full JSON report."""
    result = convert_directory(str(HERE / "sql"))
    counts = Counter(w.code for w in result.report.warnings)
    print("warning codes:", dict(counts))

    blockers = [w for w in result.report.warnings if w.code in BLOCKING]
    print("blocking findings:", len(blockers))
    for w in blockers:
        print(f"  {w.path}:{w.line} [{w.code}] {w.message}")

    print("--- full JSON report ---")
    print(json.dumps(result.report.to_dict(), indent=2)[:1200], "...")


if __name__ == "__main__":
    main()
