# Quick start

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

- one `.sqlx` file per statement (input directory structure mirrored);
- `${ref(...)}` calls wherever a statement reads a table that another
  statement produces, plus `dependencies` entries that preserve original
  write ordering;
- a `workflow_settings.yaml` scaffold (from `--init-project`);
- `report.json` describing every decision, warning and fallback.

## Convert from Python

```python
from sql2sqlx import ConversionOptions, convert_directory

result = convert_directory(
    "legacy_sql",
    "my_project/definitions",
    ConversionOptions(
        default_project="my-gcp-project",
        insert_strategy="incremental",
    ),
)

print(result.report.actions_by_type)
for warning in result.report.warnings:
    print(warning.code, warning.path, warning.line, warning.message)
```

Or for a single string:

```python
from sql2sqlx import convert_string

result = convert_string(
    "CREATE OR REPLACE TABLE analytics.daily AS "
    "SELECT * FROM raw.events;"
)
print(result.files[0].content)
```

produces

```text
config {
  type: "table",
  schema: "analytics",
  name: "daily"
}

-- source: input.sql:1 (CREATE TABLE converted by sql2sqlx v0.1.0)
SELECT * FROM raw.events
```

## What to review afterwards

The converter is deliberately conservative: anything it cannot convert
*provably safely* becomes a verbatim `operations` action and a warning.
Triage the report by code - the registry lives in
[Conversion rules](conversion_rules.md).
