# CLI reference

```text
sql2sqlx INPUT [-o DIR] [options]
```

`INPUT` is a `.sql` file or a directory. A file with no `--output`
prints the generated SQLX to **stdout** (the summary goes to stderr, so
piping is clean). A directory requires `--output` unless `--dry-run`.

## Options

| Flag | Default | Meaning |
| --- | --- | --- |
| `-o, --output DIR` | - | Where to write `.sqlx` files (your `definitions/`) |
| `--report FILE` | - | Write the JSON conversion report |
| `--default-project ID` | - | Project assumed for unqualified paths |
| `--default-dataset ID` | - | Dataset assumed for unqualified paths |
| `--default-location LOCATION` | `US` | Location for generated workflow settings |
| `--layout {mirror,flat}` | `mirror` | Mirror the input tree, or flatten |
| `--insert-strategy {incremental,operations}` | `operations` | `INSERT ... SELECT` handling |
| `--merge-strategy {operations,incremental-when-safe}` | `operations` | `MERGE` handling |
| `--plain-create {operations,declaration}` | `operations` | `CREATE TABLE` without `AS` |
| `--if-not-exists {table,operations}` | `operations` | Guarded `CREATE TABLE/VIEW ... AS` |
| `--declare-external` | off | Declarations for referenced-but-not-produced tables |
| `--no-protected` | off | Don't mark converted incrementals `protected: true` |
| `--no-annotate` | off | Omit `-- source: file:line` provenance comments |
| `--tags A,B` | - | Extra Dataform tags on every action |
| `--include GLOB` | `*.sql` | Filename filter for directory scans |
| `--encoding ENC` | `utf-8` | Input encoding |
| `-j, --jobs N` | `0` (auto) | Parser worker processes |
| `--dry-run` | off | Convert and report, write nothing |
| `--overwrite` | off | Allow writing into a dir that has `.sqlx` files |
| `--init-project` | off | Scaffold `workflow_settings.yaml` |
| `-q / -v` | - | Quiet / verbose summary |
| `--version` | - | Print version |

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Converted successfully |
| `1` | One or more input files failed (see `FAILED` lines / report) |
| `2` | Usage error (bad arguments, output dir guard) |

## Examples

```bash
# Inspect a single file without writing anything
sql2sqlx model.sql | less

# Full migration with shape-checked MERGE conversion and source declarations
sql2sqlx ./sql -o ./definitions \
    --merge-strategy incremental-when-safe \
    --declare-external --tags migrated --report report.json

# CI-style dry run that only produces the report
sql2sqlx ./sql --dry-run --report report.json -q
```
