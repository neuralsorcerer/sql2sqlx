# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for the command-line interface.

The CLI is the primary entry point and owns behavior no other module can
be held to: the documented exit codes (``0`` success, ``1`` when any input
file failed, ``2`` for usage errors), the guard against writing into a
populated output tree, ``--dry-run`` writing nothing, and the JSON report
and project scaffolding side files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sql2sqlx.cli import main

TABLE = "CREATE OR REPLACE TABLE d.t AS SELECT 1 AS x;\n"
UNLEXABLE = "BROKEN 'unterminated\n"


def _corpus(root: Path, *, broken: bool = False) -> Path:
    src = root / "in"
    src.mkdir()
    (src / "a.sql").write_text(TABLE, encoding="utf-8")
    if broken:
        (src / "bad.sql").write_text(UNLEXABLE, encoding="utf-8")
    return src


def _usage_error(argv: list[str]) -> None:
    """Assert ``argv`` is rejected by argparse with the documented code."""
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 2


# -- usage errors (exit 2) --------------------------------------------------


def test_directory_input_requires_an_output_or_dry_run(tmp_path: Path) -> None:
    _usage_error([str(_corpus(tmp_path))])


def test_invalid_runtime_options_are_rejected(tmp_path: Path) -> None:
    src = str(_corpus(tmp_path))
    out = str(tmp_path / "out")
    _usage_error([src, "-o", out, "--jobs", "-1"])
    _usage_error([src, "-o", out, "--encoding", "no-such-codec"])
    _usage_error([src, "-o", out, "--encoding", "base64_codec"])  # not a text codec
    _usage_error([src, "-o", out, "--include", "/etc/passwd"])
    _usage_error([src, "-o", out, "--include", "../*.sql"])


def test_missing_input_path_is_a_usage_error(tmp_path: Path) -> None:
    _usage_error([str(tmp_path / "nope.sql")])


# -- exit codes -------------------------------------------------------------


def test_clean_corpus_exits_zero_and_writes_the_tree(tmp_path: Path) -> None:
    src = _corpus(tmp_path)
    out = tmp_path / "out"
    assert main([str(src), "-o", str(out), "--quiet"]) == 0
    assert (out / "t.sqlx").is_file()


def test_a_failed_file_sets_the_failure_exit_code(tmp_path: Path) -> None:
    src = _corpus(tmp_path, broken=True)
    out = tmp_path / "out"
    # The healthy file still converts; only the exit code reports the failure.
    assert main([str(src), "-o", str(out), "--quiet"]) == 1
    assert (out / "t.sqlx").is_file()


def test_populated_output_is_protected_until_overwrite_is_given(tmp_path: Path) -> None:
    src = _corpus(tmp_path)
    out = tmp_path / "out"
    assert main([str(src), "-o", str(out), "--quiet"]) == 0
    assert main([str(src), "-o", str(out), "--quiet"]) == 2
    assert main([str(src), "-o", str(out), "--overwrite", "--quiet"]) == 0


def test_dry_run_reports_without_writing_anything(tmp_path: Path) -> None:
    src = _corpus(tmp_path)
    out = tmp_path / "out"
    assert main([str(src), "-o", str(out), "--dry-run", "--quiet"]) == 0
    assert not out.exists()


# -- stdout, report and scaffolding ----------------------------------------


def test_single_file_without_output_prints_the_sqlx(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "one.sql"
    source.write_text(TABLE, encoding="utf-8")
    assert main([str(source), "--quiet", "--no-annotate"]) == 0
    printed = capsys.readouterr().out
    assert 'type: "table"' in printed
    assert "SELECT 1 AS x" in printed
    assert "-- source:" not in printed  # --no-annotate honored


def test_multiple_actions_to_stdout_are_labelled_per_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "two.sql"
    source.write_text(
        "CREATE OR REPLACE TABLE d.a AS SELECT 1;\nCREATE OR REPLACE TABLE d.b AS SELECT 2;\n",
        encoding="utf-8",
    )
    assert main([str(source), "--quiet"]) == 0
    printed = capsys.readouterr().out
    assert "-- ===== a.sqlx =====" in printed
    assert "-- ===== b.sqlx =====" in printed


def test_report_is_written_as_usable_json(tmp_path: Path) -> None:
    src = _corpus(tmp_path, broken=True)
    report = tmp_path / "report.json"
    assert main([str(src), "-o", str(tmp_path / "out"), "--report", str(report), "--quiet"]) == 1
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["actions_by_type"] == {"table": 1}
    assert payload["files_read"] == 2
    assert list(payload["failures"]) == ["bad.sql"]
    assert all({"code", "message", "path", "line"} <= set(w) for w in payload["warnings"])


def test_init_project_scaffolds_settings_beside_definitions(tmp_path: Path) -> None:
    src = _corpus(tmp_path)
    definitions = tmp_path / "df" / "definitions"
    assert (
        main(
            [
                str(src),
                "-o",
                str(definitions),
                "--init-project",
                "--default-project",
                "proj",
                "--default-dataset",
                "ds",
                "--quiet",
            ]
        )
        == 0
    )
    text = (tmp_path / "df" / "workflow_settings.yaml").read_text(encoding="utf-8")
    assert 'defaultProject: "proj"' in text
    assert 'defaultDataset: "ds"' in text


def test_existing_workflow_settings_are_not_silently_replaced(tmp_path: Path) -> None:
    src = _corpus(tmp_path)
    root = tmp_path / "df"
    root.mkdir()
    settings = root / "workflow_settings.yaml"
    settings.write_text("hand written\n", encoding="utf-8")
    assert main([str(src), "-o", str(root / "definitions"), "--init-project", "--quiet"]) == 0
    assert settings.read_text(encoding="utf-8") == "hand written\n"


def test_tags_are_split_and_emptied_entries_dropped(tmp_path: Path) -> None:
    src = _corpus(tmp_path)
    out = tmp_path / "out"
    assert main([str(src), "-o", str(out), "--tags", "one, two ,,  ", "--quiet"]) == 0
    assert 'tags: ["one", "two"]' in (out / "t.sqlx").read_text(encoding="utf-8")


def test_summary_is_printed_to_stderr_unless_quieted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = _corpus(tmp_path, broken=True)
    assert main([str(src), "-o", str(tmp_path / "out")]) == 1
    err = capsys.readouterr().err
    for line in ("files read:", "statements:", "actions:", "refs rewritten:", "warnings:"):
        assert line in err
    assert "FAILED bad.sql:" in err  # failures are always listed
    assert "wrote " not in err  # ...but the file list needs --verbose


def test_verbose_lists_every_warning_and_generated_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "in"
    src.mkdir()
    # A plain CREATE TABLE has no AS query, so it reports CREATE_NO_AS.
    (src / "a.sql").write_text("CREATE TABLE d.t (x INT64);\n", encoding="utf-8")
    assert main([str(src), "-o", str(tmp_path / "out"), "--verbose"]) == 0
    err = capsys.readouterr().err
    assert "[CREATE_NO_AS] a.sql:1:" in err
    assert "wrote t.sqlx (operations)" in err


def test_an_unwritable_report_path_is_reported_not_raised(tmp_path: Path) -> None:
    src = _corpus(tmp_path)
    unwritable = tmp_path / "no-such-dir" / "report.json"
    argv = [str(src), "-o", str(tmp_path / "out"), "--report", str(unwritable), "--quiet"]
    assert main(argv) == 2
