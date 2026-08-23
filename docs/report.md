# The conversion report

Every conversion produces a `ConversionReport` alongside the generated files.
It is the machine-readable record of *what happened and why* - the artifact
you triage after a migration and the one you gate CI on. This page documents
its structure, how to obtain it, and how to act on it.

## Getting the report

From the CLI, write it to a file with `--report`:

```bash
sql2sqlx ./sql -o ./definitions --report report.json
```

From Python it is always attached to the result:

```python
from sql2sqlx import convert_directory

result = convert_directory("sql", "definitions")
report = result.report
print(report.actions_by_type)
report_dict = report.to_dict()   # JSON-serializable
```

## Report fields

`ConversionReport` is a dataclass with the following fields (see
[`ConversionReport`](api.md) for the autodoc entry):

:::{list-table}
:header-rows: 1
:widths: 26 74

* - Field
  - Meaning
* - `files_read`
  - Number of input files parsed, **including** files that failed.
* - `statements`
  - Number of top-level statements found across all files, counted before
    any whole-file script collapsing.
* - `actions_by_type`
  - Mapping of Dataform action type to count, e.g.
    `{"table": 12, "view": 4, "operations": 3}`.
* - `refs_rewritten`
  - Number of table reference sites rewritten to `${ref(...)}`.
* - `refs_unresolved`
  - Distinct referenced-but-not-produced table paths that could be
    dataset-resolved and were left as literals. These are your external
    sources; `--declare-external` turns them into declarations.
* - `warnings`
  - The list of every non-fatal finding (see below).
* - `failures`
  - Mapping of `source path -> error message` for files that could not be
    parsed at all (e.g. a lexer error). Other files are unaffected.
* - `elapsed_seconds`
  - Wall-clock duration of the run, rounded to milliseconds.
* - `input_bytes`
  - Total size of the SQL read, in bytes.
:::

Each entry in `warnings` is a `ReportWarning` with four fields:

`code`
: A stable, machine-readable identifier such as `FALLBACK_OPERATIONS` or
  `MERGE_INCREMENTAL`. Gate and filter on this, not on the message text.

`message`
: A human-readable explanation, including the specific table or reason.

`path`
: The source file the finding relates to (empty for synthesized actions).

`line`
: The 1-based source line (`0` when not applicable).

## JSON shape

`report.to_dict()` (and the CLI's `--report` file) produces this structure:

```json
{
  "files_read": 1,
  "statements": 3,
  "actions_by_type": {
    "table": 1,
    "operations": 2
  },
  "refs_rewritten": 1,
  "refs_unresolved": [
    "raw.src"
  ],
  "warnings": [
    {
      "code": "INSERT_VALUES",
      "message": "INSERT ... VALUES has no query body; kept as operations.",
      "path": "input.sql",
      "line": 2
    },
    {
      "code": "ORPHAN_SELECT",
      "message": "Standalone query converted to an operations action; review whether it should be a table, view or assertion instead.",
      "path": "input.sql",
      "line": 3
    }
  ],
  "failures": {},
  "elapsed_seconds": 0.0,
  "input_bytes": 124
}
```

Key ordering in the JSON is stable (it follows the dataclass field order, not
alphabetical), so the file diffs cleanly between runs.

## The CLI summary

Unless you pass `--quiet`, the CLI prints a compact summary to **stderr**
(generated SQLX for a single-file input goes to stdout, so piping stays
clean):

```text
sql2sqlx v0.1.0
  files read:     1
  statements:     3
  actions:        operations=2 table=1
  refs rewritten: 1 (1 external left as literals)
  warnings:       2
  failures:       0
  elapsed:        0.0s (0.0 MB)
```

Add `-v/--verbose` to also list every warning (`[CODE] path:line: message`)
and every generated file. Any failures are always listed, regardless of
verbosity.

## Triage workflow

The warnings fall into three broad severities. None of them stop a build - the
tool never emits SQL that changes behavior on its own - but they tell you where
human review pays off.

**1. Review the choice you opted into.** When you select a non-default
strategy, the tool tells you exactly what changed:

- `INSERT_INCREMENTAL`, `MERGE_INCREMENTAL`, `TARGET_SCHEMA_REQUIRED` - a
  typed incremental was produced; verify the existing target's schema matches
  the query's output before the first incremental run.
- `CREATE_REPLACE_SEMANTICS` - a non-`OR REPLACE` create became a typed action
  that rebuilds on later runs instead of erroring on an existing object.
- `IF_NOT_EXISTS`, `DECLARATION_DROPPED_DDL` - a guard was dropped or DDL was
  replaced by a declaration per your option.

**2. Understand a fallback.** These explain *why* a statement stayed verbatim,
so you can decide whether to restructure the SQL or accept the operations
action:

- `FALLBACK_OPERATIONS`, `FALLBACK_SELECT_ALIAS`, `MERGE_FALLBACK`,
  `COLUMN_DDL`, `INSERT_VALUES`, `TEMP_TABLE`, `EXTERNAL_TABLE`,
  `SNAPSHOT_TABLE`, `SCRIPT_FILE`, `PROCEDURE_PRESERVED`.

**3. Check an inferred edge or an ambiguous shape.** These flag places where
static analysis was conservative:

- `ORDER_ASSUMED`, `FUTURE_CREATOR`, `DEPENDENCY_CYCLE`, `SELF_REFERENCE`,
  `DUPLICATE_TARGET` - ordering/reference decisions to sanity-check.
- `ORPHAN_SELECT`, `DYNAMIC_SIDE_EFFECTS`, `RERUN_RISK`, `SCRIPT_WRITES` -
  statements whose intent or hidden effects need a human eye.
- `COMMENT_DROPPED` - a typed action emits only its query body, so a comment
  written in the surrounding DDL (or in a converted MERGE's `WHEN` clauses)
  has nowhere to land. The generated SQL is unaffected; check whether the
  comment recorded intent worth carrying over by hand.

The full meaning of every code lives in the
[warning code reference](conversion_rules.md#warning-codes).

## Gating CI

`failures` and specific `code`s make good CI gates. For example, fail a build
on any parse failure and on a code you have decided to disallow:

```python
import sys
from sql2sqlx import convert_directory

result = convert_directory("sql", "definitions")
report = result.report

blocking = {"DEPENDENCY_CYCLE"}
hits = [w for w in report.warnings if w.code in blocking]

if report.failures or hits:
    for path, err in report.failures.items():
        print(f"FAILED {path}: {err}", file=sys.stderr)
    for w in hits:
        print(f"[{w.code}] {w.path}:{w.line}: {w.message}", file=sys.stderr)
    sys.exit(1)
```

The CLI already exits `1` when any file failed to convert (and `2` on a usage
error), so `sql2sqlx ... --report report.json` is itself a usable gate; parse
`report.json` for finer-grained policy.

:::{tip}
`dataform compile` is the final gate. `sql2sqlx` produces a project that is
*intended* to compile, but only Dataform can confirm the whole graph resolves
against your warehouse's real schemas. Run it after every conversion.
:::
