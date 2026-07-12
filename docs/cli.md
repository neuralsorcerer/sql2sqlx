# CLI reference

```text
sql2sqlx INPUT [-o DIR] [options]
```

The CLI is a thin veneer over [`convert_file`](api.md) and
[`convert_directory`](api.md). It can also be run as a module:
`python -m sql2sqlx ...`.

## Input and output modes

`INPUT` is either a single `.sql` **file** or a **directory** of `.sql` files.
What happens next depends on the input kind and whether `--output` is given:

:::{list-table}
:header-rows: 1
:widths: 22 20 58

* - Input
  - `--output`
  - Behavior
* - file
  - omitted
  - The generated SQLX is printed to **stdout**; the run summary goes to
    **stderr**, so `sql2sqlx model.sql > out.sqlx` is clean.
* - file
  - given
  - The generated file(s) are written beneath the output directory.
* - directory
  - given
  - The converted tree is written beneath the output directory.
* - directory
  - omitted
  - **Error**, unless `--dry-run` is passed (a directory has no meaningful
    stdout form).
:::

When a single file produces **multiple** actions and is printed to stdout, the
files are separated by a header line so the stream stays parseable:

```text
-- ===== active.sqlx =====
config { ... }
...

-- ===== customers.sqlx =====
config { ... }
...
```

## Options

:::{list-table}
:header-rows: 1
:widths: 34 14 52

* - Flag
  - Default
  - Meaning
* - `-o, --output DIR`
  - -
  - Where to write `.sqlx` files (typically your Dataform `definitions/`).
* - `--report FILE`
  - -
  - Write the full JSON [conversion report](report.md) to `FILE`.
* - `--default-project ID`
  - -
  - Project assumed for unqualified table paths (reference resolution only).
* - `--default-dataset ID`
  - -
  - Dataset assumed for unqualified paths; also resolves single-part
    references to qualified producers.
* - `--default-location LOCATION`
  - `US`
  - Location written into a scaffolded `workflow_settings.yaml`.
* - `--layout {mirror,flat}`
  - `mirror`
  - Mirror the input directory tree, or flatten all output into the root.
* - `--insert-strategy {incremental,operations}`
  - `operations`
  - How `INSERT ... SELECT` converts.
* - `--merge-strategy {operations,incremental-when-safe}`
  - `operations`
  - How `MERGE` converts.
* - `--plain-create {operations,declaration}`
  - `operations`
  - How `CREATE TABLE` **without** `AS SELECT` converts.
* - `--if-not-exists {table,operations}`
  - `operations`
  - How guarded `CREATE TABLE/VIEW IF NOT EXISTS ... AS` converts.
* - `--declare-external`
  - off
  - Emit `declaration` actions under `sources/` for referenced-but-not-produced
    tables, and `ref()` them too.
* - `--no-protected`
  - off
  - Do **not** mark converted incrementals `protected: true`.
* - `--no-annotate`
  - off
  - Omit the `-- source: file:line` provenance comments.
* - `--tags A,B`
  - -
  - Comma-separated Dataform tags added to every generated action.
* - `--include GLOB`
  - `*.sql`
  - Filename glob for directory scans (must stay within the input directory).
* - `--encoding ENC`
  - `utf-8`
  - Encoding used to read input files (must be a text codec).
* - `-j, --jobs N`
  - `0` (auto)
  - Parser worker processes; `0` = one per CPU, `1` = no multiprocessing.
* - `--dry-run`
  - off
  - Convert and report, but write no `.sqlx` files.
* - `--overwrite`
  - off
  - Allow writing into an output directory that already contains `.sqlx` files.
* - `--init-project`
  - off
  - Also scaffold a `workflow_settings.yaml`.
* - `-q, --quiet`
  - -
  - Suppress the summary.
* - `-v, --verbose`
  - -
  - Also list every warning and generated file.
* - `--version`
  - -
  - Print the version and exit.
:::

### Strategy flags in depth

The four strategy flags each default to the **semantics-preserving** option
and let you opt into more idiomatic Dataform structure where you accept the
trade-off. Their full rules, proof obligations and the warnings they emit are
documented in [conversion rules](conversion_rules.md):

- `--insert-strategy incremental` produces `type: "incremental"` from
  `INSERT ... SELECT`, marks it `protected: true` (unless `--no-protected`),
  and reports `INSERT_INCREMENTAL`.
- `--merge-strategy incremental-when-safe` converts only MERGEs whose shape is
  *provably* equivalent to an incremental upsert; everything else stays
  `operations` with a `MERGE_FALLBACK` reason.
- `--plain-create declaration` turns bodiless `CREATE TABLE (schema)` DDL into
  a `declaration` (drops the DDL; use for externally managed tables).
- `--if-not-exists table` converts guarded `... IF NOT EXISTS ... AS`
  statements to typed actions and reports the lost create-if-absent guard.

### The `--init-project` scaffold

`--init-project` writes a Dataform-core 3.x `workflow_settings.yaml`. If the
output directory is named `definitions`, the file is placed in its **parent**
(the project root); otherwise it is placed inside the output directory. It is
**never** silently overwritten - pass `--overwrite` to replace an existing one.

```yaml
defaultProject: "my-gcp-project"        # from --default-project, else "your-gcp-project"
defaultLocation: "US"                   # from --default-location
defaultDataset: "analytics"             # from --default-dataset, else "dataform"
defaultAssertionDataset: "dataform_assertions"
dataformCoreVersion: "3.0.61"
```

### The overwrite guard

When writing to a directory, if the output path already contains any `.sqlx`
files and `--overwrite` is not set, `sql2sqlx` stops with exit code `2` rather
than risk clobbering hand-written actions. Because conversion is deterministic,
re-running with `--overwrite` in CI produces the same tree and a clean diff.

## Exit codes

:::{list-table}
:header-rows: 1
:widths: 12 88

* - Code
  - Meaning
* - `0`
  - Converted successfully (there may still be warnings).
* - `1`
  - One or more input files failed to parse (see `FAILED` lines and the
    report's `failures`).
* - `2`
  - Usage error: bad arguments, a directory input without `--output`/`--dry-run`,
    the output-directory guard, or an output-write error.
:::

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

# Regenerate an existing project deterministically, verbosely
sql2sqlx ./sql -o ./definitions --overwrite -v

# Convert only staging models, single-process
sql2sqlx ./sql -o ./definitions --include "stg_*.sql" -j 1
```
