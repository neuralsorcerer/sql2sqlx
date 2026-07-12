# Core concepts

This page explains the shape of what `sql2sqlx` produces and the vocabulary
the rest of the documentation uses. Read it once and the
[conversion rules](conversion_rules.md), [dependency model](dependencies.md)
and [report reference](report.md) will read much faster.

## From `.sql` statements to Dataform actions

`sql2sqlx` reads BigQuery SQL - DDL (`CREATE`), DML (`INSERT`, `UPDATE`,
`DELETE`, `MERGE`), and BigQuery scripts (`BEGIN ... END`, `DECLARE`,
`EXECUTE IMMEDIATE`, ...) - and rewrites each top-level statement into a
Dataform **action**: a single `.sqlx` file that Dataform compiles into one
node of its execution graph.

Every action has a **type**, carried in a leading `config { ... }` block.
`sql2sqlx` emits exactly five of the Dataform action types:

:::{list-table}
:header-rows: 1
:widths: 18 82

* - Action type
  - What it is, and when `sql2sqlx` emits it
* - `table`
  - A table rebuilt from a query on every run. Emitted for
    `CREATE [OR REPLACE] TABLE x AS query`.
* - `view`
  - A logical view. Emitted for `CREATE [OR REPLACE] VIEW x AS query`, and
    for `CREATE MATERIALIZED VIEW` (with `materialized: true`).
* - `incremental`
  - A table that Dataform *appends* to on normal runs and *rebuilds* on a
    full refresh. Only emitted for `INSERT`/`MERGE` when you explicitly
    opt in (see [conversion rules](conversion_rules.md)).
* - `operations`
  - One or more raw SQL statements Dataform runs verbatim in a BigQuery
    script context. This is the **safe fallback**: anything that cannot be
    converted *provably* without changing behavior becomes an `operations`
    action, so the SQL that runs is exactly the SQL you wrote.
* - `declaration`
  - A reference to a table that already exists and is managed outside
    Dataform (a *source*). Declarations carry no SQL body.
:::

The governing rule is worth stating up front, because it explains almost
every decision the tool makes:

> **Convert when it is provably safe; otherwise fall back to a verbatim
> `operations` action and record a warning.**

An `operations` action runs the original statement unchanged, so the fallback
path can never alter behavior - it only forgoes idiomatic Dataform structure.
When the tool *does* choose a typed action, or when you opt into a strategy
that changes first-run behavior, it records a [warning](report.md) explaining
the trade-off.

## Anatomy of a generated `.sqlx` file

A converted statement produces a file with up to three parts, in this order:

1. the `config { ... }` block;
2. a provenance comment (unless disabled) plus the statement's own leading
   comments;
3. the SQL body (absent for `declaration` actions).

```text
config {
  type: "table",
  schema: "analytics",
  name: "daily_sales",
  description: "Daily sales rollup",
  bigquery: {
    partitionBy: "DATE(order_ts)",
    clusterBy: ["region", "product_id"],
    requirePartitionFilter: true
  }
}

-- source: input.sql:1 (CREATE TABLE converted by sql2sqlx v0.1.0)
SELECT region, product_id, SUM(amount) AS total
FROM raw.orders
GROUP BY region, product_id
```

### The `config { ... }` block

The config block is JavaScript object-literal syntax (Dataform's format), not
JSON: keys are bare identifiers where possible and quoted only when they are
not valid JavaScript identifiers. `sql2sqlx` emits keys in a fixed canonical
order so that output is deterministic and diff-friendly, regardless of the
order metadata was discovered in. The top-level order is:

```text
type, database, schema, name, materialized, protected, hasOutput,
uniqueKey, description, columns, dependencies, tags, bigquery, disabled
```

and inside the nested `bigquery` object:

```text
partitionBy, clusterBy, updatePartitionFilter, labels,
partitionExpirationDays, requirePartitionFilter, additionalOptions
```

Any key not in these lists is emitted after the known keys, in insertion
order. The most important fields:

`type`
: The Dataform action type (one of the five above). Always present.

`database` / `schema` / `name`
: The target's project / dataset / table. `name` is always present.
  `schema` (dataset) and `database` (project) are emitted **only when the
  source SQL qualified them explicitly** - `sql2sqlx` never invents a
  project or dataset in the target identity from a `--default-*` value.
  (Defaults are still used to *resolve references*; see below.)

`dependencies`
: Ordering edges to other actions. See the [dependency model](dependencies.md).

`bigquery`
: BigQuery-specific physical options mapped from `PARTITION BY`,
  `CLUSTER BY` and `OPTIONS(...)`.

Booleans such as `protected: true`, `materialized: true`, `hasOutput: true`
and `uniqueKey: [...]` appear only when the corresponding rule applies.

### The provenance comment

Unless you pass `--no-annotate` (CLI) or `annotate=False` (API), every action
body is preceded by a single provenance line:

```text
-- source: <source-path>:<line> (<ORIGINAL KIND> converted by sql2sqlx v<version>)
```

- `<source-path>` is the input file path, relative to the conversion root.
- `<line>` is the 1-based line of the original statement.
- `<ORIGINAL KIND>` is the leading keyword(s) of the original statement, e.g.
  `CREATE TABLE`, `MERGE`, `INSERT`, or `SCRIPT` for a whole-file script.

Declarations carry no body and therefore no provenance comment.

### The SQL body and source fidelity

The single most important guarantee `sql2sqlx` makes about the body is:

> **Untouched SQL is preserved character-for-character after decoding.**

Bodies are not re-serialized from a parse tree. Instead the emitter takes a
slice of the **original source text** and applies a small set of *span edits*
- surgical `(start, end, replacement)` replacements at exact character
offsets. The only things that change are:

- table references rewritten to `${ref(...)}`;
- the statement's own target rewritten to `${self()}` (only for elected
  `hasOutput` operations);
- select-list aliases inserted for a column-list `INSERT -> incremental`
  rewrite;
- constant SQLX placeholders that neutralize literal `${` text and
  SQLX-unsafe comments (see below).

Everything else - your capitalization, indentation, inline comments, string
literals, trailing whitespace inside the statement - survives exactly. One
cosmetic normalization is applied to the body as a whole: **exactly one
trailing semicolon is stripped** (Dataform runs the body as a script, and a
trailing empty statement is at best noise).

Generated files are always written as UTF-8 with `\n` newlines.

### Reference and self forms

A rewritten reference carries exactly the qualification the *producer* was
declared with:

:::{list-table}
:header-rows: 1
:widths: 45 55

* - Producer declared as
  - Emitted reference
* - `CREATE TABLE t ...`
  - `${ref("t")}`
* - `CREATE TABLE ds.t ...`
  - `${ref("ds", "t")}`
* - ``CREATE TABLE `proj`.ds.t ...``
  - `${ref({database: "proj", schema: "ds", name: "t"})}`
:::

An elected `hasOutput` operation has its own target path replaced with
`${self()}`, so the verbatim DDL still writes to the Dataform-managed object:

```text
CREATE SNAPSHOT TABLE ${self()} CLONE analytics.users
```

## Literal `${` and SQLX-unsafe comments

Dataform interprets `${ ... }` in a `.sqlx` file as a JavaScript placeholder.
If your SQL contains a **literal** `${` - inside a string literal, a
backtick-quoted identifier, or a quoted parameter - it would be
mis-interpreted by the Dataform compiler, and Dataform provides no SQL-level
backslash escape for it. `sql2sqlx` therefore rewrites such literals through a
constant placeholder that evaluates back to the exact original text:

```text
-- input:   SELECT '${literal}' AS v
-- output:  SELECT ${"'${literal}'"} AS v
```

The replacement `${"'${literal}'"}` is a placeholder whose JavaScript value is
the original string `'${literal}'`, so the compiled SQL is unchanged.
References inserted *by* `sql2sqlx` (`${ref(...)}`, `${self()}`) are of course
left active.

Two GoogleSQL comment forms are also lexical hazards in a `.sqlx` file, and
are normalized without changing BigQuery semantics:

- **`#` line comments** are rewritten to `--`. GoogleSQL treats `#` and `--`
  as equivalent single-line comments, but the Dataform SQLX lexer does not
  recognize `#`, so `# ...` text could be mis-parsed.
- **Separator-looking comments** - a line whose comment text is exactly `---`
  (with only surrounding horizontal whitespace) - are rewritten to `-- ---`.
  Dataform treats a line of exactly three dashes as an *operations statement
  separator*; prefixing `-- ` keeps it a comment in both engines.

These rewrites are semantics-preserving in BigQuery and only exist so the
generated file compiles correctly under Dataform.

## Output file names and layout

Each action is written to `<stem>.sqlx`, where `<stem>` is derived from the
target table name (or, for scripts and unnamed actions, the source file
stem). Names are sanitized to the character class `[A-Za-z0-9_]`; Windows
reserved device names are prefixed with `_`, and very long names are
truncated with a short hash suffix to stay filesystem-safe. Collisions within
a run are resolved deterministically by appending `_2`, `_3`, ...

For **directory** conversions the `--layout` option controls placement:

- `mirror` (default) reproduces the input directory structure under the output
  root, so `staging/a.sql` becomes `staging/a.sqlx`;
- `flat` writes every `.sqlx` directly into the output root.

Synthesized declarations (from `--declare-external`) are always written under
`sources/`.

## Determinism

For a given input tree and a given set of options, `sql2sqlx` produces
**byte-identical** output, no matter how many worker processes ran the parse
phase. Action ordering, name-collision resolution and cross-file writer chains
are all resolved from a stable corpus order (sorted relative path, then
statement position), so re-running the tool in CI never produces a spurious
diff.
