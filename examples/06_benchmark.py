# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""Example 6: throughput benchmark on a synthetic corpus.

Generates a realistic multi-file warehouse (CTAS chains with metadata,
INSERTs, MERGEs, comments, cross-file references), converts it, and
prints throughput. Used to produce the numbers quoted in the README.

Run:  python examples/06_benchmark.py [--files N] [--statements M] [--jobs J]
"""

import argparse
import shutil
import tempfile
import time
from pathlib import Path

from sql2sqlx import ConversionOptions, convert_directory

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
    """Write the synthetic corpus; return total line count."""
    lines = 0
    for shard in range(files):
        parts = [HEAD.format(shard=shard)]
        for i in range(1, statements + 1):
            parts.append(STMT.format(i=i, prev=i - 1, shard=shard))
        text = "\n".join(parts)
        path = root / f"shard_{shard:04d}" / "pipeline.sql"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        lines += text.count("\n") + 1
    return lines


def main() -> None:
    """Generate, convert, report throughput."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--files", type=int, default=50)
    ap.add_argument(
        "--statements", type=int, default=200, help="statements per file (each ~11 lines)"
    )
    ap.add_argument("--jobs", type=int, default=0)
    args = ap.parse_args()

    root = Path(tempfile.mkdtemp(prefix="sql2sqlx_bench_"))
    try:
        lines = generate(root, args.files, args.statements)
        size = sum(p.stat().st_size for p in root.rglob("*.sql"))
        print(f"corpus: {args.files} files, {lines:,} lines, " f"{size / 1e6:.1f} MB")
        t0 = time.time()
        result = convert_directory(str(root), options=ConversionOptions(jobs=args.jobs))
        dt = time.time() - t0
        r = result.report
        assert not r.failures, r.failures
        print(
            f"converted: {sum(r.actions_by_type.values()):,} actions, "
            f"{r.refs_rewritten:,} refs rewritten"
        )
        print(f"elapsed:   {dt:.2f}s  " f"({size / 1e6 / dt:.1f} MB/s, {lines / dt:,.0f} lines/s)")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
