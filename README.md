<h1 align="center">
sql2sqlx
</h1>
<h3 align="center">
BigQuery SQL -> Dataform SQLX migration tooling.
</h3>

---

<div align="center">

[![Current Release](https://img.shields.io/github/release/neuralsorcerer/sql2sqlx.svg)](https://github.com/neuralsorcerer/sql2sqlx/releases)
[![PyPI](https://img.shields.io/pypi/v/sql2sqlx.svg?logo=pypi&logoColor=white)](https://pypi.org/project/sql2sqlx/)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-fcbc2c.svg?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![Test Ubuntu](https://github.com/neuralsorcerer/sql2sqlx/actions/workflows/test_ubuntu.yml/badge.svg)](https://github.com/neuralsorcerer/sql2sqlx/actions/workflows/test_ubuntu.yml?query=branch%3Amain)
[![Test Windows](https://github.com/neuralsorcerer/sql2sqlx/actions/workflows/test_windows.yml/badge.svg)](https://github.com/neuralsorcerer/sql2sqlx/actions/workflows/test_windows.yml?query=branch%3Amain)
[![Test MacOS](https://github.com/neuralsorcerer/sql2sqlx/actions/workflows/test_macos.yml/badge.svg)](https://github.com/neuralsorcerer/sql2sqlx/actions/workflows/test_macos.yml?query=branch%3Amain)
[![Lints](https://github.com/neuralsorcerer/sql2sqlx/actions/workflows/lints.yml/badge.svg)](https://github.com/neuralsorcerer/sql2sqlx/actions/workflows/lints.yml?query=branch%3Amain)
[![CodeQL](https://github.com/neuralsorcerer/sql2sqlx/actions/workflows/codeql.yml/badge.svg)](https://github.com/neuralsorcerer/sql2sqlx/actions/workflows/codeql.yml?query=branch%3Amain)
[![Dataform Compile](https://github.com/neuralsorcerer/sql2sqlx/actions/workflows/dataform_compile.yml/badge.svg)](https://github.com/neuralsorcerer/sql2sqlx/actions/workflows/dataform_compile.yml?query=branch%3Amain)
[![Build](https://github.com/neuralsorcerer/sql2sqlx/actions/workflows/build.yml/badge.svg)](https://github.com/neuralsorcerer/sql2sqlx/actions/workflows/build.yml?query=branch%3Amain)
[![Documentation](https://github.com/neuralsorcerer/sql2sqlx/actions/workflows/docs.yml/badge.svg)](https://neuralsorcerer.github.io/sql2sqlx/)
[![License](https://img.shields.io/badge/License-Apache%202.0-3c60b1.svg?logo=opensourceinitiative&logoColor=white)](LICENSE)
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/sql2sqlx?period=total&units=INTERNATIONAL_SYSTEM&left_color=GRAY&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/sql2sqlx)

</div>

`sql2sqlx` converts plain BigQuery/GoogleSQL `.sql` estates into
[Dataform](https://cloud.google.com/dataform) `.sqlx` actions. It is built
for real migrations: large repositories, mixed DDL/DML/script workloads,
legacy scheduler ordering, comments and formatting that must survive review,
and teams that need a machine-readable audit trail rather than a black-box
rewrite.

At a high level it turns this:

```sql
CREATE OR REPLACE TABLE marts.customer_orders
PARTITION BY DATE(order_ts)
CLUSTER BY customer_id
OPTIONS(description = "Customer order facts") AS
SELECT customer_id, order_id, order_ts
FROM staging.orders;
```

into this:

```sqlx
config {
  type: "table",
  schema: "marts",
  name: "customer_orders",
  description: "Customer order facts",
  bigquery: {
    partitionBy: "DATE(order_ts)",
    clusterBy: ["customer_id"]
  }
}

-- source: path/to/file.sql:1 (CREATE TABLE converted by sql2sqlx v0.1.0)
SELECT customer_id, order_id, order_ts
FROM ${ref("staging", "orders")}
```

The project philosophy is deliberately conservative:

> **Convert only when the tool can prove that the generated SQLX preserves the
> intended behavior. Otherwise, keep the original SQL verbatim as a Dataform
> `operations` action and report exactly why.**

That policy is what makes the tool suitable for production migrations. It
prefers an auditable fallback over a clever but unsafe guess.

---

## What this project solves

Migrating a BigQuery warehouse to Dataform usually involves several risky,
manual steps:

1. Translating `CREATE TABLE ... AS SELECT` and `CREATE VIEW ... AS SELECT`
   statements into Dataform `config {}` blocks.
2. Replacing raw table names with `${ref(...)}` calls without accidentally
   rewriting CTEs, aliases, table functions, `UNNEST`, or self-references.
3. Preserving ordering for legacy DML such as `INSERT`, `MERGE`, `UPDATE`,
   `DELETE`, `DROP`, and `ALTER` statements.
4. Handling scripts that rely on BigQuery session state: variables,
   transactions, temporary tables, procedural blocks, procedure calls, and
   dynamic SQL.
5. Keeping enough provenance to review the migration and gate it in CI.

Regular expressions are not enough for this. Semicolons appear inside strings,
comments, and scripts. Identifiers may be backticked, escaped, dotted, or use
BigQuery dashed project syntax. Query-local names can shadow table names.
Dataform can own exactly one output per action name, while SQL files can have
many writers to the same physical table.

`sql2sqlx` addresses those problems with a tokenizer, statement splitter,
classifier, reference scanner, cross-file linker, and SQLX emitter that all
operate on source spans rather than re-serializing SQL from an AST. The result
is deterministic, reviewable SQLX with conservative fallback behavior.

---

## Core capabilities

### Safe SQLX action generation

- Converts safe `CREATE TABLE ... AS SELECT` statements to `type: "table"`.
- Converts safe `CREATE VIEW ... AS SELECT` statements to `type: "view"`.
- Preserves unsupported or unsafe statements as `type: "operations"`.
- Can emit `type: "declaration"` for externally managed plain tables when
  explicitly requested.
- Can convert eligible `INSERT ... SELECT` and shape-proven `MERGE` statements
  to `type: "incremental"` when the user opts into those strategies.

### BigQuery-aware parsing foundations

- Handles GoogleSQL strings, raw strings, bytes literals, triple-quoted
  strings, comments, backtick identifiers, parameters, numeric literals,
  operators, and dashed project identifiers.
- Splits statements without being fooled by semicolons in strings, comments,
  expressions, or scripting blocks.
- Understands BigQuery scripting constructs such as `BEGIN ... END`,
  `IF ... END IF`, `LOOP`, `WHILE`, `REPEAT`, `FOR`, scripting `CASE`, and
  transaction boundaries.

### Dependency-aware migration

- Rewrites produced table reads to `${ref(...)}`.
- Builds writer chains so mutating statements keep their corpus order.
- Adds dependencies on the latest preceding writer when a reader consumes a
  table that was mutated earlier.
- Avoids dependency edges that would reorder same-file future creators before
  earlier reads.
- Detects duplicate owners and demotes later owners to safe operations.
- Optionally creates declarations for referenced-but-not-produced external
  tables.

### Metadata preservation

- Maps `PARTITION BY` to `bigquery.partitionBy`.
- Maps `CLUSTER BY` to `bigquery.clusterBy`.
- Maps simple `OPTIONS(description=...)` to `description`.
- Maps labels and selected BigQuery table options to Dataform BigQuery config.
- Preserves unrecognized or complex options as raw `bigquery.additionalOptions`.

### Auditability and scale

- Emits provenance comments such as `-- source: file.sql:42` by default.
- Carries leading and trailing SQL comments into generated files where safe.
- Produces a JSON report with files read, statement counts, action counts,
  rewritten refs, unresolved refs, warnings, failures, and elapsed time.
- Uses stable warning codes so migrations can be reviewed and gated in CI.
- Runs per-file parsing in parallel for directory conversions.
- Has no runtime third-party dependencies.
- Ships type information via `py.typed`.

---

## Installation

Install from PyPI:

```bash
pip install sql2sqlx
```

The package provides both:

- the `sql2sqlx` command-line program, and
- the importable `sql2sqlx` Python package.

Runtime support targets Python **3.10 through 3.14**.

For local development from a repository checkout:

```bash
git clone https://github.com/neuralsorcerer/sql2sqlx.git
cd sql2sqlx
python -m pip install -e ".[dev,docs,examples]"
python -m pytest
```

If you only want to run tests from a checkout, the repository's pytest
configuration includes the `src` package root, so `python -m pytest` works
without manually setting `PYTHONPATH` or performing an editable install.

---

## Quick start

### Convert a directory into a Dataform definitions tree

```bash
sql2sqlx ./legacy_sql \
  -o ./dataform/definitions \
  --default-project my-gcp-project \
  --default-dataset analytics \
  --report ./conversion-report.json \
  --init-project
```

This command:

1. scans `./legacy_sql` for `*.sql` files,
2. converts each SQL file into one or more `.sqlx` actions,
3. writes the generated files below `./dataform/definitions`,
4. writes a JSON conversion report, and
5. scaffolds a `workflow_settings.yaml` next to the `definitions` folder.

### Inspect a single SQL file

```bash
sql2sqlx ./models/orders.sql
```

For a single input file with no `--output`, generated SQLX is printed to
stdout. The summary is printed to stderr.

### Dry-run a migration

```bash
sql2sqlx ./legacy_sql --dry-run --report report.json --verbose
```

Dry runs perform parsing, classification, linking, warning generation, and
report generation without writing SQLX files.

---

## Command-line usage

```text
sql2sqlx INPUT [OPTIONS]
```

`INPUT` may be either a `.sql` file or a directory. Directory conversions
normally require `--output`, unless `--dry-run` is supplied.

### Common options

| Flag | Default | Description |
| --- | --- | --- |
| `-o, --output DIR` | none | Output directory for generated `.sqlx` files, usually Dataform `definitions/`. |
| `--report FILE` | none | Write the machine-readable JSON conversion report. |
| `--default-project ID` | none | Project used to resolve unqualified table paths for linking. |
| `--default-dataset ID` | none | Dataset used to resolve unqualified table paths for linking. |
| `--default-location LOCATION` | `US` | Location written into generated workflow settings. |
| `--layout {mirror,flat}` | `mirror` | Preserve input directory structure or emit all SQLX files in one folder. |
| `--include GLOB` | `*.sql` | Glob used when scanning directories. |
| `--encoding ENCODING` | `utf-8` | Input file encoding. |
| `-j, --jobs N` | `0` | Parser worker processes; `0` means auto-detect. |
| `--dry-run` | off | Convert and report without writing output files. |
| `--overwrite` | off | Permit writing into an output directory that already contains `.sqlx` files. |
| `--init-project` | off | Scaffold `workflow_settings.yaml`. |
| `-q, --quiet` | off | Suppress the run summary. |
| `-v, --verbose` | off | Print warnings and generated files in the summary. |
| `--version` | n/a | Print the installed version. |

### Strategy options

The defaults are intentionally conservative.

| Flag | Default | Other values | Meaning |
| --- | --- | --- | --- |
| `--insert-strategy` | `operations` | `incremental` | Keep `INSERT ... SELECT` verbatim, or convert safe shapes to Dataform incrementals. |
| `--merge-strategy` | `operations` | `incremental-when-safe` | Keep `MERGE` verbatim, or convert shape-proven MERGEs to incrementals with `uniqueKey`. |
| `--plain-create` | `operations` | `declaration` | Keep schema-only `CREATE TABLE` DDL, or emit a declaration and drop the DDL. |
| `--if-not-exists` | `operations` | `table` | Preserve guarded CTAS/view DDL, or lift it with a warning about changed rerun behavior. |
| `--declare-external` | off | on | Emit declarations for external tables that are read but not produced by the corpus. |
| `--no-protected` | off | on | Disable `protected: true` on converted incrementals. |
| `--no-annotate` | off | on | Disable generated provenance comments. |
| `--tags TAG[,TAG...]` | none | n/a | Add Dataform tags to every action. |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Conversion completed without per-file failures. |
| `1` | Conversion completed, but one or more input files failed to read or lex. |
| `2` | Usage or output-writing error. |

---

## Python API usage

The public API mirrors the CLI.

### Convert a string

```python
from sql2sqlx import convert_string

result = convert_string("""
CREATE OR REPLACE TABLE analytics.daily AS
SELECT CURRENT_DATE() AS d;
""")

for file in result.files:
    print(file.relpath)
    print(file.content)
```

### Convert one file

```python
from sql2sqlx import convert_file

result = convert_file("legacy_sql/orders.sql")
print(result.report.to_dict())
```

### Convert a directory

```python
from sql2sqlx import ConversionOptions, convert_directory

options = ConversionOptions(
    default_project="my-gcp-project",
    default_dataset="analytics",
    insert_strategy="incremental",
    declare_external=True,
    jobs=4,
)

result = convert_directory(
    "legacy_sql",
    "dataform/definitions",
    options,
)

print(result.report.actions_by_type)
for warning in result.report.warnings:
    print(warning.code, f"{warning.path}:{warning.line}", warning.message)
```

### Write an existing result

```python
from sql2sqlx import convert_directory, write_result

result = convert_directory("legacy_sql")
write_result(result, "dataform/definitions")
```

---

## Conversion model

The converter produces Dataform actions from SQL statements. Some statements
map directly to typed Dataform actions. Others must remain operations because
Dataform cannot represent the original statement without changing semantics.

| Input SQL | Default output | Notes |
| --- | --- | --- |
| `CREATE [OR REPLACE] TABLE ... AS SELECT ...` | `table` | Metadata is mapped when safely understood. |
| `CREATE TABLE IF NOT EXISTS ... AS SELECT ...` | `operations` | `--if-not-exists table` converts with a warning because Dataform reruns rebuild targets. |
| `CREATE [MATERIALIZED] VIEW ... AS SELECT ...` | `view` | Materialized views receive `materialized: true`. |
| View column lists | `view` or `operations` | Converted by safely pushing aliases into the select list; ambiguous lists fall back. |
| `INSERT INTO t SELECT ...` | `operations` | `--insert-strategy incremental` converts eligible shapes. |
| `INSERT INTO t (cols) SELECT ...` | `operations` | Incremental conversion aliases the select list only when arity and names are provably safe. |
| `INSERT ... VALUES` | `operations` | Preserved verbatim. |
| `MERGE` | `operations` | `--merge-strategy incremental-when-safe` converts only shape-proven MERGEs. |
| Plain `CREATE TABLE t (...)` | `operations` | `--plain-create declaration` emits a source declaration instead. |
| `CREATE TEMP TABLE` | `operations` | Temporary object scope must remain inside BigQuery script/session semantics. |
| `CREATE EXTERNAL TABLE` | `operations` | External table DDL is preserved; declarations may be a better manual target. |
| `CREATE SNAPSHOT TABLE` | `operations` | Preserved verbatim. |
| `CREATE TABLE ... LIKE/CLONE/COPY` | `operations` | No typed Dataform equivalent. |
| `UPDATE`, `DELETE`, `TRUNCATE`, `DROP`, `ALTER`, `LOAD DATA` | `operations` | Write targets are still tracked for dependency ordering. |
| Standalone `SELECT` or `WITH` | `operations` | Reported with `ORPHAN_SELECT` for manual review. |
| Scripts using variables, transactions, temp objects, dynamic SQL, calls, or procedural control flow | one script `operations` action | Keeps shared BigQuery context intact. |
| Anything unsupported or unknown | `operations` | Original SQL is preserved and warning codes explain why. |

### Incremental conversion details

`INSERT ... SELECT` conversion is opt-in because an append operation is not the
same as a rebuildable table by default. When enabled, `sql2sqlx` emits a
Dataform incremental action and, unless disabled, marks it `protected: true` to
reduce full-refresh risk.

For `INSERT INTO target (a, b) SELECT x, y ...`, the converter must preserve
output column names. It rewrites the select list to `SELECT x AS a, y AS b ...`
only when each item can be reasoned about exactly. It falls back for `*`,
`SELECT AS STRUCT`, arity mismatches, unsafe aliases, and ambiguous constructs.

### MERGE conversion details

`MERGE` statements are preserved by default. With
`--merge-strategy incremental-when-safe`, only restricted MERGE shapes are
converted. The converter must be able to identify target keys, update columns,
insert columns, and a source query that Dataform can represent as an
incremental body. If any proof fails, the original MERGE remains an operation.

### Metadata mapping details

The converter maps BigQuery table metadata when Dataform has a direct field or
when a raw option can be preserved safely:

| BigQuery SQL | SQLX config |
| --- | --- |
| `PARTITION BY expr` | `bigquery.partitionBy: "expr"` |
| `CLUSTER BY a, b` | `bigquery.clusterBy: ["a", "b"]` |
| `OPTIONS(description = "...")` | `description: "..."` |
| `OPTIONS(labels = [("k", "v")])` | `bigquery.labels` |
| `OPTIONS(partition_expiration_days = N)` | `bigquery.partitionExpirationDays` |
| `OPTIONS(require_partition_filter = true)` | `bigquery.requirePartitionFilter` |
| Other options | `bigquery.additionalOptions` with raw SQL values |

---

## Dependency linking

Dataform dependency correctness is the heart of this project. The linker does
more than replace strings; it builds a corpus-level model of producers,
writers, readers, and action names.

### Producer and writer tracking

For each table identity, the linker determines:

- the action that owns the Dataform output, if any,
- all statements that write to the table,
- corpus order of those writers,
- reads that can be safely rewritten to refs, and
- reads that must remain literal to avoid changing execution order.

Corpus order is deterministic: sorted file path, then statement position within
the file.

### Reference rewriting rules

A table reference is eligible for `${ref(...)}` only when it is a real table
read and the referenced table is owned or declared in the generated project.
The scanner avoids rewriting:

- CTE names within their visibility scope,
- range variables and aliases,
- table-valued function calls,
- `UNNEST(...)`,
- target tables of mutating DML,
- self-references inside a defining query,
- future same-file creators that would be pulled before earlier reads,
- wildcard or decorated table expressions that cannot be Dataform
  declarations, and
- known metadata pseudo-schemas such as `INFORMATION_SCHEMA`.

### Ordering rules

Generated dependencies preserve the most important legacy ordering properties:

1. A reader depends on the producer of a table when a safe producer exists.
2. A reader also depends on the latest preceding writer when it reads a table
   that has been mutated earlier in corpus order.
3. Writers to the same table depend on the previous writer.
4. Later same-file writes wait for intervening readers so mutations do not race
   ahead of reads that originally came first.
5. Cross-file writer chains are flagged with `ORDER_ASSUMED` because sorted
   file-path order is deterministic but may not match the legacy scheduler.
6. Duplicate producers are demoted to operations because Dataform allows one
   owner per target.
7. Cycle-producing dependency edges are omitted conservatively and reported.

---

## Conversion report and review workflow

Every conversion returns a `ConversionReport`; the CLI can write it with
`--report report.json`.

The report includes:

- `files_read`,
- `statements`,
- `input_bytes`,
- `actions_by_type`,
- `refs_rewritten`,
- unresolved external references,
- per-file failures,
- elapsed seconds, and
- warnings with `{code, message, path, line}`.

Stable warning codes are intended for automation. A production migration can
fail CI on unexpected new warning codes while allowing reviewed warnings.

Recommended migration workflow:

1. Run with conservative defaults and generate a report.
2. Review every warning by code and source location.
3. Run `dataform compile` on the generated project.
4. Compare compiled SQL or BigQuery dry-run plans for critical jobs.
5. Opt into incremental, merge, guarded-create, or declaration strategies only
   when the documented preconditions match your warehouse.
6. Keep the report as a CI artifact for future regression checks.

Important warning categories include:

| Code | Meaning |
| --- | --- |
| `FALLBACK_OPERATIONS` | Statement could not be safely converted to a typed action. |
| `CREATE_REPLACE_SEMANTICS` | A create statement was lifted into Dataform rebuild semantics. |
| `IF_NOT_EXISTS` | Guarded create semantics were preserved or explicitly changed. |
| `INSERT_INCREMENTAL` | An `INSERT ... SELECT` was converted to incremental. |
| `MERGE_INCREMENTAL` | A safe MERGE shape was converted to incremental. |
| `TARGET_SCHEMA_REQUIRED` | Converted incremental logic assumes target schema compatibility. |
| `DUPLICATE_TARGET` | A later producer was demoted because the target already had an owner. |
| `ORDER_ASSUMED` | Multiple files write the same table; sorted-path order was used. |
| `SELF_REFERENCE` | A defining query reads its own target and was left literal. |
| `FUTURE_CREATOR` | A ref was not generated because the owner appears later in corpus order. |
| `SCRIPT_FILE` | A file was preserved as a whole script operation. |
| `DYNAMIC_SIDE_EFFECTS` | Calls or dynamic SQL may hide dependencies. |
| `ORPHAN_SELECT` | A standalone query needs manual review. |

See [`docs/conversion_rules.md`](docs/conversion_rules.md) for the detailed
rulebook.

---

## Project architecture

`sql2sqlx` is organized as a three-phase pipeline.

```text
SQL files
  |
  v
Phase 1: per-file parsing
  - lexer: tokens and comment spans
  - splitter: top-level statements
  - parser: action drafts, metadata, warnings
  - reference scanner: candidate table read sites
  |
  v
Phase 2: corpus linker
  - resolve table identities
  - elect creators and demote duplicates
  - build writer chains
  - choose hasOutput operations
  - rewrite safe refs
  - synthesize external declarations
  - assign action names and dependencies
  |
  v
Phase 3: emitter
  - render Dataform config blocks
  - apply source-span edits
  - preserve comments and annotations
  - write deterministic .sqlx files
```

### Source fidelity invariant

Generated SQL bodies are built by applying non-overlapping span edits to slices
of the original source text. The converter does not reformat or reconstruct SQL
from tokens. This preserves casing, whitespace, comments, and dialect-specific
syntax outside explicit edits.

SQLX interpolation safety is handled explicitly. Literal `${` inside SQL
strings or quoted identifiers is emitted through a constant JavaScript
placeholder so it cannot be interpreted as user-controlled SQLX code, while
converter-generated `${ref(...)}` and `${self()}` remain active.

---

## Correctness, safety, and limitations

### Guarantees

- **No silent best-effort rewrites.** Unsafe statements remain operations with
  warnings.
- **Character fidelity outside explicit edits.** Untouched SQL text is copied
  from source spans.
- **Deterministic output.** File order, action naming, and path collision
  handling are deterministic.
- **Per-file failure isolation.** One bad file is reported without aborting an
  entire directory conversion.
- **Typed API surface.** The package includes type hints and `py.typed`.
- **No runtime dependencies.** Deployment into migration tooling remains simple.

### Important limitations

- Input is assumed to be BigQuery Standard SQL / GoogleSQL. Other dialects may
  lex or convert incorrectly.
- Some valid GoogleSQL forms have no Dataform typed equivalent and intentionally
  remain operations.
- Whole-file scripts stay whole when statement splitting would break shared
  BigQuery context.
- Cross-file writer order is inferred from sorted paths, not from an external
  scheduler. Review every `ORDER_ASSUMED` warning.
- `--insert-strategy incremental`, `--merge-strategy incremental-when-safe`,
  and `--if-not-exists table` are explicit migration decisions. Review their
  warnings before using them in production.
- `--declare-external` can generate Dataform declarations for many external
  tables, but it intentionally skips metadata schemas and table expressions
  that are not valid standalone declarations.
- The generated project should always be validated with `dataform compile` and,
  for critical paths, BigQuery dry runs or controlled execution.

See [`docs/limitations.md`](docs/limitations.md) for more detail.

---

## Performance and scale

The converter is designed for large repositories:

- lexing is linear-time,
- per-file phase 1 work can run in multiple processes,
- linker output is deterministic regardless of worker count,
- failures are captured per file,
- source text is retained so emission can remain span-exact.

Use `--jobs N` to control parsing parallelism. `--jobs 0` selects an automatic
worker count based on available CPUs.

A benchmark script is included:

```bash
python examples/06_benchmark.py --files 120 --statements 800
```

Benchmark results vary by hardware, Python version, and corpus shape. Use the
included script with representative SQL to size migration runs for your own
environment.

---

## Development

Install development dependencies:

```bash
python -m pip install -e ".[dev,docs,examples]"
```

Run the main checks:

```bash
python -m pytest
python -m mypy src/sql2sqlx
python -m black --check .
python -m isort --check-only .
```

Build documentation locally:

```bash
python -m pip install -e ".[docs]"
make -C docs html
```

Contribution expectations:

- preserve the conservative conversion policy,
- add or update tests for every conversion rule change,
- keep warning codes stable unless a breaking change is intentional,
- prefer source-span edits over SQL reserialization,
- document new strategies and warnings in both README and `docs/`, and
- run the full local check suite before opening a PR.

---

## Citation

If you use `sql2sqlx` in migration work, publications, benchmarks, or internal
technical reports, please cite the project metadata in [`CITATION.cff`](CITATION.cff).

```bibtex
@software{sarkar_sql2sqlx,
  title = {sql2sqlx: Conservative BigQuery SQL to Dataform SQLX migration tooling},
  author = {Soumyadip Sarkar},
  url = {https://github.com/neuralsorcerer/sql2sqlx},
  license = {Apache-2.0}
}
```

## License and trademarks

`sql2sqlx` is licensed under the [Apache License 2.0](LICENSE).

This project is not affiliated with or endorsed by Google. BigQuery,
GoogleSQL, and Dataform are trademarks of Google LLC.

## References

- [Dataform documentation](https://cloud.google.com/dataform/docs)
- [GoogleSQL for BigQuery reference](https://cloud.google.com/bigquery/docs/reference/standard-sql)
