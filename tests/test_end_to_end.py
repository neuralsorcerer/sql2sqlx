# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""End-to-end tests: directory pipelines, the CLI, determinism and scale.

These tests exercise the same paths a real migration would: a small
multi-file warehouse (sources, staging, marts, incrementals, a script),
the installed console entry point via ``python -m sql2sqlx``, output
overwrite guards, JSON reports, per-file failure isolation and
deterministic re-runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from sql2sqlx import ConversionOptions, Layout, convert_directory, convert_string

SRC = Path(__file__).resolve().parents[1] / "src"


def run_cli(*args, cwd=None):
    """Run ``python -m sql2sqlx`` with the repo's src/ on PYTHONPATH."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "sql2sqlx", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=120,
    )


def make_warehouse(root: Path) -> None:
    """Write the miniature multi-file warehouse used by several tests."""
    (root / "raw").mkdir(parents=True)
    (root / "staging").mkdir()
    (root / "marts").mkdir()
    (root / "incremental").mkdir()
    (root / "scripts").mkdir()
    (root / "raw" / "sources.sql").write_text(
        "CREATE TABLE IF NOT EXISTS raw.events (id INT64, ts TIMESTAMP);\n"
    )
    (root / "staging" / "stg_orders.sql").write_text(
        "-- staged orders\n"
        "CREATE OR REPLACE TABLE staging.orders\n"
        "PARTITION BY DATE(ts)\n"
        "AS SELECT id, ts FROM raw.events;\n"
    )
    (root / "marts" / "daily.sql").write_text(
        "CREATE OR REPLACE VIEW marts.daily AS\n"
        "SELECT DATE(ts) AS d, COUNT(*) AS n\n"
        "FROM staging.orders GROUP BY 1;\n"
    )
    (root / "incremental" / "facts.sql").write_text(
        "INSERT INTO marts.facts (d, n)\n"
        "SELECT DATE(ts), COUNT(*) FROM staging.orders GROUP BY 1;\n"
    )
    (root / "scripts" / "backfill.sql").write_text(
        "DECLARE cutoff DATE DEFAULT DATE '2020-01-01';\n"
        "DELETE FROM marts.facts WHERE d < cutoff;\n"
    )


def test_directory_conversion_wires_the_full_dag(tmp_path):
    src = tmp_path / "sql"
    out = tmp_path / "definitions"
    make_warehouse(src)
    result = convert_directory(
        str(src),
        str(out),
        ConversionOptions(jobs=1, insert_strategy="incremental"),
    )
    assert not result.report.failures
    rels = {f.relpath for f in result.files}
    assert rels == {
        "raw/events.sqlx",
        "staging/orders.sqlx",
        "marts/daily.sqlx",
        "incremental/facts.sqlx",
        "scripts/backfill.sqlx",
    }
    read = {rel: (out / rel).read_text() for rel in rels}
    # Ownerless plain CREATE elected as the ref-able producer.
    assert "hasOutput: true" in read["raw/events.sqlx"]
    assert "${self()}" in read["raw/events.sqlx"]
    # Downstream reads become refs; metadata mapped.
    assert '${ref("raw", "events")}' in read["staging/orders.sqlx"]
    assert 'partitionBy: "DATE(ts)"' in read["staging/orders.sqlx"]
    assert "-- staged orders" in read["staging/orders.sqlx"]
    assert '${ref("staging", "orders")}' in read["marts/daily.sqlx"]
    assert 'type: "view"' in read["marts/daily.sqlx"]
    # INSERT converted with column aliasing.
    facts = read["incremental/facts.sqlx"]
    assert 'type: "incremental"' in facts
    assert "DATE(ts) AS d" in facts and "COUNT(*) AS n" in facts
    # The script writes marts.facts and is ordered after its creator.
    backfill = read["scripts/backfill.sqlx"]
    assert "DECLARE cutoff" in backfill
    assert 'dependencies: ["marts.facts"]' in backfill
    codes = {w.code for w in result.report.warnings}
    assert "ORDER_ASSUMED" in codes
    assert result.report.actions_by_type == {
        "operations": 2,
        "table": 1,
        "view": 1,
        "incremental": 1,
    }


def test_conversion_is_deterministic(tmp_path):
    src = tmp_path / "sql"
    make_warehouse(src)
    first = convert_directory(str(src), options=ConversionOptions(jobs=1))
    second = convert_directory(str(src), options=ConversionOptions(jobs=2))
    assert [(f.relpath, f.content) for f in first.files] == [
        (f.relpath, f.content) for f in second.files
    ]


def test_flat_layout(tmp_path):
    src = tmp_path / "sql"
    make_warehouse(src)
    result = convert_directory(str(src), options=ConversionOptions(jobs=1, layout=Layout.FLAT))
    assert all("/" not in f.relpath for f in result.files)


def test_failure_isolation(tmp_path):
    src = tmp_path / "sql"
    src.mkdir()
    (src / "good.sql").write_text("CREATE TABLE d.ok AS SELECT 1;\n")
    (src / "bad.sql").write_text("SELECT 'unterminated\n")
    result = convert_directory(str(src), options=ConversionOptions(jobs=1))
    assert list(result.report.failures) == ["bad.sql"]
    assert "line 1" in result.report.failures["bad.sql"]
    assert [f.action_name for f in result.files] == ["ok"]


def test_cli_file_to_stdout(tmp_path):
    sql = tmp_path / "one.sql"
    sql.write_text("CREATE TABLE d.t AS SELECT 1 AS x;\n")
    proc = run_cli(str(sql), "-q")
    assert proc.returncode == 0, proc.stderr
    assert 'type: "table"' in proc.stdout
    assert 'name: "t"' in proc.stdout


def test_cli_directory_report_and_init_project(tmp_path):
    src = tmp_path / "sql"
    make_warehouse(src)
    out = tmp_path / "proj" / "definitions"
    report = tmp_path / "report.json"
    proc = run_cli(
        str(src),
        "-o",
        str(out),
        "--report",
        str(report),
        "--init-project",
        "--default-project",
        "acme-prod",
        "--default-location",
        "EU",
        "--insert-strategy",
        "incremental",
    )
    assert proc.returncode == 0, proc.stderr
    assert (out / "staging" / "orders.sqlx").is_file()
    settings = tmp_path / "proj" / "workflow_settings.yaml"
    assert settings.is_file()
    assert 'defaultProject: "acme-prod"' in settings.read_text()
    assert 'defaultLocation: "EU"' in settings.read_text()
    assert 'dataformCoreVersion: "3.0.61"' in settings.read_text()
    data = json.loads(report.read_text())
    assert data["files_read"] == 5
    assert data["actions_by_type"]["incremental"] == 1
    assert "elapsed_seconds" in data
    # Summary goes to stderr, not stdout.
    assert "files read" in proc.stderr


def test_cli_overwrite_guard(tmp_path):
    src = tmp_path / "sql"
    src.mkdir()
    (src / "a.sql").write_text("CREATE TABLE d.a AS SELECT 1;\n")
    out = tmp_path / "out"
    assert run_cli(str(src), "-o", str(out), "-q").returncode == 0
    blocked = run_cli(str(src), "-o", str(out), "-q")
    assert blocked.returncode == 2
    assert "overwrite" in blocked.stderr
    assert run_cli(str(src), "-o", str(out), "-q", "--overwrite").returncode == 0


def test_cli_exit_code_on_failures(tmp_path):
    src = tmp_path / "sql"
    src.mkdir()
    (src / "bad.sql").write_text("SELECT 'nope\n")
    out = tmp_path / "out"
    proc = run_cli(str(src), "-o", str(out))
    assert proc.returncode == 1
    assert "FAILED bad.sql" in proc.stderr


def test_cli_version_and_dry_run(tmp_path):
    proc = run_cli("--version")
    assert proc.returncode == 0 and "sql2sqlx" in proc.stdout
    src = tmp_path / "sql"
    src.mkdir()
    (src / "a.sql").write_text("CREATE TABLE d.a AS SELECT 1;\n")
    proc = run_cli(str(src), "--dry-run", "-q")
    assert proc.returncode == 0
    assert not list(tmp_path.glob("**/*.sqlx"))


def test_scale_sanity_chained_statements():
    parts = ["CREATE TABLE d.t0 AS SELECT 1 AS x;"]
    for i in range(1, 300):
        parts.append(f"CREATE TABLE d.t{i} AS SELECT x + {i} AS x FROM d.t{i - 1};")
    result = convert_string("\n".join(parts))
    assert not result.report.failures
    assert len(result.files) == 300
    assert result.report.refs_rewritten == 299
    assert result.report.statements == 300
