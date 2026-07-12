# Quick start

This page gets you from a folder of `.sql` files to a Dataform project in a
few minutes, using either the CLI or the Python API. For a full migration
walkthrough see the [migration guide](migration_guide.md).

## Convert a directory (CLI)

Point the converter at your SQL tree and at your Dataform project's
`definitions/` directory:

```bash
sql2sqlx ./legacy_sql -o ./my_project/definitions \
    --report report.json \
    --init-project \
    --default-project my-gcp-project \
    --default-dataset analytics \
    --insert-strategy incremental
```

You get:

- one `.sqlx` file per statement, with the input directory structure mirrored
  under `definitions/`;
- `${ref(...)}` calls wherever a statement reads a table that another statement
  produces, plus `dependencies` entries that preserve original write ordering;
- a `workflow_settings.yaml` scaffold (from `--init-project`), written next to
  `definitions/`;
- `report.json` describing every decision, warning and fallback.

The `--default-project`/`--default-dataset` values are used to line up
unqualified references with their producers; they are not baked into the
generated target identities.

## Convert from Python

The Python API mirrors the CLI. `convert_directory` reads, converts and
(optionally) writes a whole tree:

```python
from sql2sqlx import ConversionOptions, convert_directory

result = convert_directory(
    "legacy_sql",
    "my_project/definitions",
    ConversionOptions(
        default_project="my-gcp-project",
        default_dataset="analytics",
        insert_strategy="incremental",
    ),
)

print(result.report.actions_by_type)
for warning in result.report.warnings:
    print(warning.code, warning.path, warning.line, warning.message)
```

`ConversionOptions` accepts the documented strategy strings (`"incremental"`,
`"operations"`, ...) as well as the enum members, so options are easy to build
from JSON or config files. Every field is documented under
[`ConversionOptions`](api.md).

To convert a single string in memory (nothing is written to disk):

```python
from sql2sqlx import convert_string

result = convert_string(
    "CREATE OR REPLACE TABLE analytics.daily AS SELECT * FROM raw.events;"
)
print(result.files[0].content)
```

which produces:

```text
config {
  type: "table",
  schema: "analytics",
  name: "daily"
}

-- source: input.sql:1 (CREATE TABLE converted by sql2sqlx v0.1.0)
SELECT * FROM raw.events
```

## Reading the output

That output is a complete Dataform action. Note three things, all explained in
[core concepts](concepts.md):

1. The **`config` block** names the action. `type: "table"` means Dataform
   rebuilds this table from the query on every run; `schema` is emitted
   because the source qualified `analytics`.
2. The **`-- source:` line** records where the statement came from (disable
   with `--no-annotate`).
3. The **body is your original SQL**, sliced from the source text - only
   references, `${self()}`, aliases and literal-`${` escapes are ever edited.

Add a second, dependent statement and the wiring appears automatically:

```python
result = convert_string(
    "CREATE OR REPLACE TABLE staging.customers AS SELECT * FROM raw.customers;\n"
    "CREATE OR REPLACE VIEW  marts.active AS "
    "SELECT id FROM staging.customers WHERE active;"
)
for f in result.files:
    print(f"# {f.relpath}\n{f.content}")
```

The view's read of `staging.customers` becomes
`${ref("staging", "customers")}`, and Dataform infers the dependency from that
`ref()`:

```text
# active.sqlx
config {
  type: "view",
  schema: "marts",
  name: "active"
}

-- source: input.sql:2 (CREATE VIEW converted by sql2sqlx v0.1.0)
SELECT id FROM ${ref("staging", "customers")} WHERE active
...
```

## Inspecting a single file

Running the CLI on a **file** with no `--output` prints the generated SQLX to
stdout (the summary goes to stderr, so pipes stay clean):

```bash
sql2sqlx model.sql | less
```

## What to review afterwards

The converter is deliberately conservative: anything it cannot convert
*provably safely* becomes a verbatim `operations` action plus a warning, so a
first run never changes behavior. Triage the report by warning `code` - the
full registry is in [conversion rules](conversion_rules.md#warning-codes) and
the triage workflow is in [the report reference](report.md#triage-workflow).
Finally, run `dataform compile` as the last gate.
