# Migration guide

This is the end-to-end playbook for moving an existing BigQuery SQL codebase
into a Dataform project with `sql2sqlx`. The [quick start](quickstart.md)
shows the shortest path; this guide covers the full workflow, the decisions
you will make along the way, and how to verify the result.

The philosophy to keep in mind: `sql2sqlx` is **conservative by default**. Out
of the box it changes no behavior - unconvertible statements become verbatim
`operations` actions. You then opt into more idiomatic Dataform structure
(typed incrementals, dropped guards, external declarations) statement class by
statement class, guided by the [report](report.md).

## 1. Inventory your SQL

Gather the `.sql` files you want to migrate into one tree. `sql2sqlx` scans a
directory recursively (default glob `*.sql`), so mirror your existing layout -
the `mirror` output layout will reproduce it under `definitions/`.

Each file may contain one statement or many; statement boundaries are found by
a real lexer, so semicolons inside strings, comments and scripting blocks are
handled correctly. Files that use transactions, temporary tables, variables or
procedural control flow are kept whole (see
[limitations](limitations.md#scripts-stay-whole)).

## 2. Do a dry run and read the report

Before writing anything, convert with `--dry-run` and capture the report:

```bash
sql2sqlx ./legacy_sql --dry-run --report report.json \
    --default-project my-gcp-project \
    --default-dataset analytics
```

`--default-project`/`--default-dataset` describe how unqualified table names in
your SQL should be resolved so that references line up with producers across
files. They do **not** get baked into target identities - only explicit
qualification in your SQL does that.

Open `report.json` (or add `-v` for a console listing) and look at:

- `failures` - files that could not be parsed at all. Fix these first; a
  located `LexError` points at the exact character.
- `actions_by_type` - a sanity check on how many tables/views/operations you
  expect.
- `refs_unresolved` - your external sources (tables read but never produced).
- `warnings` - triage by `code` using the
  [report triage workflow](report.md#triage-workflow).

## 3. Choose your conversion strategies

The defaults preserve semantics exactly. Opt into typed conversions where you
understand and accept the trade-off. Each flag is covered in depth in the
[CLI reference](cli.md) and [conversion rules](conversion_rules.md).

:::{list-table}
:header-rows: 1
:widths: 34 66

* - If your SQL has...
  - Consider
* - `INSERT ... SELECT` appends you want as native incrementals
  - `--insert-strategy incremental` (emits `protected: true`; review
    `INSERT_INCREMENTAL` / `TARGET_SCHEMA_REQUIRED`)
* - `MERGE` upserts with a simple same-named key/update shape
  - `--merge-strategy incremental-when-safe` (converts only provable shapes;
    others stay operations with `MERGE_FALLBACK`)
* - `CREATE TABLE` DDL for tables managed outside Dataform
  - `--plain-create declaration`
* - `CREATE TABLE/VIEW IF NOT EXISTS ... AS` you want typed
  - `--if-not-exists table` (records the lost create-if-absent guard)
* - Reads of upstream tables you want as declared sources
  - `--declare-external`
:::

Two more that affect every action:

- `--no-protected` - drop `protected: true` from converted incrementals (only
  if you *want* full refreshes to rebuild them).
- `--tags a,b` - stamp Dataform tags onto every generated action.
- `--no-annotate` - omit the `-- source:` provenance comments.

## 4. Scaffold the Dataform project

Point `--output` at your project's `definitions/` directory and add
`--init-project` to scaffold a `workflow_settings.yaml` next to it:

```bash
sql2sqlx ./legacy_sql -o ./my_dataform_project/definitions \
    --report report.json \
    --init-project \
    --default-project my-gcp-project \
    --default-dataset analytics \
    --insert-strategy incremental \
    --merge-strategy incremental-when-safe
```

`--init-project` writes a Dataform-core 3.x settings file (it is never
silently overwritten):

```yaml
defaultProject: "my-gcp-project"
defaultLocation: "US"
defaultDataset: "analytics"
defaultAssertionDataset: "dataform_assertions"
dataformCoreVersion: "3.0.61"
```

Set the location with `--default-location` if you are not in `US`. If the
output directory already contains `.sqlx` files, `sql2sqlx` refuses to write
unless you pass `--overwrite` - a guard against clobbering hand-written actions.

## 5. Compile with Dataform

`sql2sqlx` produces a project that is *designed* to compile, but only Dataform
can confirm the whole dependency graph resolves against your real schemas.
This is the authoritative final gate:

```bash
cd my_dataform_project
npx @dataform/cli@3.0.61 compile
```

Fix any compile errors (usually an unresolved source you want to
`--declare-external`, or an `operations` action that needs a manual
dependency), then re-run the conversion. Conversion is deterministic and
idempotent, so re-running over the same input with the same options yields the
same tree - safe to run repeatedly in CI with `--overwrite`.

## 6. Review the operations actions

Everything the tool could not convert provably lives in an `operations`
action, flagged in the report. Walk these with intent:

- **`SCRIPT_FILE`** - a whole file kept together for transaction/temp/variable
  scope. Decide whether it should stay a script or be refactored into
  separate models. Its `SCRIPT_WRITES` warning lists the tables it writes, and
  downstream readers are already ordered after it.
- **`MERGE_FALLBACK` / `FALLBACK_SELECT_ALIAS`** - the SQL shape was outside
  the provable set. Often a small rewrite (naming the key column in
  `UPDATE SET`, or aliasing an anonymous expression) lets a re-run convert it.
- **`DYNAMIC_SIDE_EFFECTS`** - `CALL`/`EXECUTE IMMEDIATE` whose reads and
  writes cannot be seen statically. Add any needed `dependencies` by hand.
- **`ORPHAN_SELECT`** - a bare `SELECT`. Decide whether it should be a table,
  a view, or a Dataform assertion.

## 7. Commit and iterate

Commit the generated `definitions/` tree and the `report.json` together. On
subsequent source changes, re-run the same command with `--overwrite`; the
deterministic output means version control shows only genuine changes.

A practical CI recipe:

```bash
# Regenerate, gate on failures, then let Dataform have the final word.
sql2sqlx ./legacy_sql -o ./definitions --overwrite --report report.json -q
python ci/check_report.py report.json      # your policy on warning codes
npx @dataform/cli@3.0.61 compile
```

See [the report reference](report.md#gating-ci) for a ready-made
`check_report.py` skeleton.

## What to expect over time

A first pass typically lands most `CREATE ... AS` statements as clean
`table`/`view` actions with wired `${ref()}` dependencies, and parks the
genuinely imperative parts (scripts, procedures, ad-hoc DML) as `operations`.
That is the intended outcome: a working, behavior-preserving project on day
one, which you incrementally make more idiomatic as you validate each strategy
against your data.
