# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""Command-line interface: ``sql2sqlx INPUT [-o OUTPUT] [options]``.

The CLI is a thin veneer over :func:`sql2sqlx.convert_file` and
:func:`sql2sqlx.convert_directory`:

* a **file** input with no ``--output`` prints the generated ``.sqlx``
  to stdout (handy for piping and quick inspection);
* a **directory** input requires ``--output`` (unless ``--dry-run``) and
  writes the converted tree beneath it;
* ``--report FILE`` writes the full machine-readable JSON report;
* ``--init-project`` additionally scaffolds a ``workflow_settings.yaml``
  next to the output ``definitions/`` directory.

Exit codes: ``0`` success, ``1`` when any input file failed to convert,
``2`` for usage errors.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from sql2sqlx.converter import convert_directory, convert_file, write_result
from sql2sqlx.errors import Sql2SqlxError
from sql2sqlx.model import (
    ConversionOptions,
    ConversionResult,
    IfNotExistsStrategy,
    InsertStrategy,
    Layout,
    MergeStrategy,
    PlainCreateStrategy,
)
from sql2sqlx.version import __version__


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        A fully configured :class:`argparse.ArgumentParser`.
    """
    p = argparse.ArgumentParser(
        prog="sql2sqlx",
        description="Convert BigQuery SQL files into Dataform SQLX actions.",
        epilog="Docs and conversion rules: " "https://github.com/neuralsorcerer/sql2sqlx",
    )
    p.add_argument("input", help="a .sql file or a directory of .sql files")
    p.add_argument(
        "-o",
        "--output",
        metavar="DIR",
        help="output directory for generated .sqlx files "
        "(typically your Dataform 'definitions/' folder)",
    )
    p.add_argument("--report", metavar="FILE", help="write the JSON conversion report to FILE")
    p.add_argument(
        "--default-project", metavar="ID", help="project assumed for unqualified table paths"
    )
    p.add_argument(
        "--default-dataset", metavar="ID", help="dataset assumed for unqualified table paths"
    )
    p.add_argument(
        "--default-location",
        metavar="LOCATION",
        default="US",
        help="location for generated workflow settings " "(default: %(default)s)",
    )
    p.add_argument(
        "--layout",
        choices=[layout.value for layout in Layout],
        default=Layout.MIRROR.value,
        help="mirror the input tree or flatten output " "(default: %(default)s)",
    )
    p.add_argument(
        "--insert-strategy",
        choices=[s.value for s in InsertStrategy],
        default=InsertStrategy.OPERATIONS.value,
        help="how INSERT ... SELECT converts " "(default: %(default)s)",
    )
    p.add_argument(
        "--merge-strategy",
        choices=[s.value for s in MergeStrategy],
        default=MergeStrategy.OPERATIONS.value,
        help="how MERGE converts (default: %(default)s)",
    )
    p.add_argument(
        "--plain-create",
        choices=[s.value for s in PlainCreateStrategy],
        default=PlainCreateStrategy.OPERATIONS.value,
        help="how CREATE TABLE without AS converts " "(default: %(default)s)",
    )
    p.add_argument(
        "--if-not-exists",
        choices=[s.value for s in IfNotExistsStrategy],
        default=IfNotExistsStrategy.OPERATIONS.value,
        help="how guarded CREATE TABLE/VIEW ... AS converts " "(default: %(default)s)",
    )
    p.add_argument(
        "--declare-external",
        action="store_true",
        help="emit declarations for referenced-but-not-produced " "tables and ref() them too",
    )
    p.add_argument(
        "--no-protected",
        action="store_true",
        help="do not mark converted incrementals as " "protected: true",
    )
    p.add_argument(
        "--no-annotate",
        action="store_true",
        help="omit the '-- source: file:line' provenance comments",
    )
    p.add_argument(
        "--tags", metavar="TAG[,TAG...]", help="comma-separated Dataform tags added to every action"
    )
    p.add_argument(
        "--include",
        metavar="GLOB",
        default="*.sql",
        help="filename glob for directory scans " "(default: %(default)s)",
    )
    p.add_argument("--encoding", default="utf-8", help="input file encoding (default: %(default)s)")
    p.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=0,
        metavar="N",
        help="worker processes for directory conversion; " "0 = auto (default)",
    )
    p.add_argument("--dry-run", action="store_true", help="convert and report, but write nothing")
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="allow writing into an output directory that " "already contains .sqlx files",
    )
    p.add_argument(
        "--init-project",
        action="store_true",
        help="also scaffold a workflow_settings.yaml next to " "the output directory",
    )
    p.add_argument("-q", "--quiet", action="store_true", help="suppress the summary")
    p.add_argument(
        "-v", "--verbose", action="store_true", help="also list warnings and generated files"
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _options_from_args(args: argparse.Namespace) -> ConversionOptions:
    """Translate parsed CLI arguments into :class:`ConversionOptions`.

    Args:
        args: The parsed namespace.

    Returns:
        The options object.
    """
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    return ConversionOptions(
        default_project=args.default_project,
        default_dataset=args.default_dataset,
        default_location=args.default_location,
        insert_strategy=InsertStrategy(args.insert_strategy),
        merge_strategy=MergeStrategy(args.merge_strategy),
        plain_create_strategy=PlainCreateStrategy(args.plain_create),
        if_not_exists_strategy=IfNotExistsStrategy(args.if_not_exists),
        declare_external=args.declare_external,
        protect_incrementals=not args.no_protected,
        annotate=not args.no_annotate,
        tags=tags,
        layout=Layout(args.layout),
        encoding=args.encoding,
        include_glob=args.include,
        jobs=args.jobs,
    )


def _write_workflow_settings(
    output_dir: Path, opts: ConversionOptions, overwrite: bool
) -> Optional[Path]:
    """Scaffold a Dataform-core-3.x ``workflow_settings.yaml``.

    The file is written next to a ``definitions/`` output directory (or
    inside the output directory otherwise) and never silently replaced.

    Args:
        output_dir: The CLI output directory.
        opts: Conversion options (supplies project/dataset defaults).
        overwrite: Whether an existing file may be replaced.

    Returns:
        The written path, or ``None`` when skipped.
    """
    root = output_dir.parent if output_dir.name == "definitions" else output_dir
    path = root / "workflow_settings.yaml"
    if path.exists() and not overwrite:
        print(f"note: {path} already exists; not overwritten", file=sys.stderr)
        return None
    content = (
        f"defaultProject: {json.dumps(opts.default_project or 'your-gcp-project')}\n"
        f"defaultLocation: {json.dumps(opts.default_location)}\n"
        f"defaultDataset: {json.dumps(opts.default_dataset or 'dataform')}\n"
        'defaultAssertionDataset: "dataform_assertions"\n'
        'dataformCoreVersion: "3.0.61"\n'
    )
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _print_summary(result: ConversionResult, verbose: bool) -> None:
    """Print a human-readable run summary to stderr.

    Args:
        result: The conversion result.
        verbose: Also list warnings and every generated file.
    """
    r = result.report
    actions = " ".join(f"{k}={v}" for k, v in sorted(r.actions_by_type.items()))
    mb = r.input_bytes / 1_000_000
    err = sys.stderr
    print(f"sql2sqlx v{__version__}", file=err)
    print(f"  files read:     {r.files_read}", file=err)
    print(f"  statements:     {r.statements}", file=err)
    print(f"  actions:        {actions or '-'}", file=err)
    print(
        f"  refs rewritten: {r.refs_rewritten}"
        f" ({len(r.refs_unresolved)} external left as literals)",
        file=err,
    )
    print(f"  warnings:       {len(r.warnings)}", file=err)
    print(f"  failures:       {len(r.failures)}", file=err)
    print(f"  elapsed:        {r.elapsed_seconds}s ({mb:.1f} MB)", file=err)
    if verbose:
        for w in r.warnings:
            loc = f"{w.path}:{w.line}" if w.path else "-"
            print(f"  [{w.code}] {loc}: {w.message}", file=err)
        for f in result.files:
            print(f"  wrote {f.relpath} ({f.action_type.value})", file=err)
    for path, message in r.failures.items():
        print(f"  FAILED {path}: {message}", file=err)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (``0`` ok, ``1`` conversion failures,
        ``2`` usage error).
    """
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.jobs < 0:
        parser.error("--jobs must be zero (auto) or a positive integer")
    try:
        opts = _options_from_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    in_path = Path(args.input)
    try:
        if in_path.is_dir():
            if args.output is None and not args.dry_run:
                parser.error(
                    "--output is required when converting a directory " "(or pass --dry-run)"
                )
            result = convert_directory(str(in_path), None, opts)
        elif in_path.is_file():
            result = convert_file(str(in_path), opts)
        else:
            parser.error(f"input not found: {args.input}")
            return 2  # pragma: no cover - parser.error raises
    except Sql2SqlxError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.output and not args.dry_run:
            out = Path(args.output)
            if out.exists() and not args.overwrite and any(out.rglob("*.sqlx")):
                print(
                    f"error: {out} already contains .sqlx files; " "pass --overwrite to proceed",
                    file=sys.stderr,
                )
                return 2
            write_result(result, str(out))
            if args.init_project:
                _write_workflow_settings(out, opts, args.overwrite)
        elif args.output is None and in_path.is_file() and not args.dry_run:
            for i, sqlx in enumerate(result.files):
                if len(result.files) > 1:
                    if i:
                        print()
                    label = sqlx.relpath.encode("unicode_escape").decode("ascii")
                    print(f"-- ===== {label} =====")
                print(sqlx.content, end="")
        if args.report:
            Path(args.report).write_text(
                json.dumps(result.report.to_dict(), indent=2, sort_keys=False), encoding="utf-8"
            )
    except (OSError, Sql2SqlxError) as exc:
        print(f"error: could not write output: {exc}", file=sys.stderr)
        return 2
    if not args.quiet:
        _print_summary(result, verbose=args.verbose)
    return 1 if result.report.failures else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
