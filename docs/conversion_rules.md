# Conversion rules

The governing principle:

> **Convert when provably safe; otherwise fall back to a verbatim
> `operations` action and record a warning.**

An `operations` action executes the original SQL unchanged, so the fallback
path can never alter behavior - it only forgoes idiomatic Dataform structure.
This page is the exhaustive reference for how each statement class maps, the
proof obligations for the opt-in strategies, how metadata is translated, and
what every warning code means.

For the *shape* of what is produced (config block, provenance, source
fidelity), see [core concepts](concepts.md). For how references and ordering
are wired, see the [dependency model](dependencies.md).

## Statement mapping

| Input statement | Result | Notes |
| --- | --- | --- |
| `CREATE [OR REPLACE] TABLE x ... AS query` | `type: "table"` | `PARTITION BY` / `CLUSTER BY` / `OPTIONS` mapped to config. A non-`OR REPLACE` create still becomes a `table` and reports `CREATE_REPLACE_SEMANTICS` (Dataform rebuilds rather than erroring on an existing target). |
| `CREATE TABLE IF NOT EXISTS x AS query` | `operations` (default) | `--if-not-exists table` opts into a typed action and reports the lost guard (`IF_NOT_EXISTS`). |
| `CREATE TABLE x AS` with a column list | `operations` | Column DDL can carry types/constraints the `table` type cannot express (`COLUMN_DDL`). |
| `CREATE TEMP TABLE` | `operations` | No Dataform equivalent (`TEMP_TABLE`). |
| `CREATE TABLE ... LIKE/CLONE/COPY` | `operations` | No typed equivalent (`FALLBACK_OPERATIONS`, with the specific form in the message). |
| `CREATE TABLE x (schema)` (no `AS`) | `operations` (default) or `declaration` | `--plain-create declaration` drops the DDL and declares the source (`DECLARATION_DROPPED_DDL`); the default reports `CREATE_NO_AS` and, without a re-run guard, `RERUN_RISK`. |
| `CREATE EXTERNAL TABLE` / `SNAPSHOT` | `operations` | Tracked as a writer of the target (`EXTERNAL_TABLE` / `SNAPSHOT_TABLE`); eligible for `hasOutput`/`${self()}`. |
| `CREATE [OR REPLACE] VIEW x AS query` | `type: "view"` | Guarded views stay operations by default; view column lists are pushed into the select list as aliases. |
| `CREATE MATERIALIZED VIEW` | `type: "view"`, `materialized: true` | MV options (e.g. `enable_refresh`) pass through `bigquery.additionalOptions`. |
| `INSERT [INTO] x SELECT ...` | `operations` (default) | See below; `--insert-strategy incremental` opts into typed conversion. |
| `INSERT ... VALUES` | `operations` | No query body (`INSERT_VALUES`). |
| `MERGE` | `operations` (default) | `--merge-strategy incremental-when-safe` converts shape-proven MERGEs to `incremental` + `uniqueKey` and reports the required target-schema check. |
| `UPDATE` / `DELETE` / `TRUNCATE` | `operations` | Write target tracked for dependency chaining. |
| `DROP` / `ALTER` (table/view) | `operations` | Write target tracked. |
| `LOAD DATA INTO/OVERWRITE` | `operations` | Write target tracked. |
| Bare `SELECT` / `WITH` | `operations` + `ORPHAN_SELECT` | Review: table, view or assertion? |
| File requiring shared script context | one whole-file `operations` action | Transactions, temporary objects, variables, procedural blocks and dynamic side effects stay together (`SCRIPT_FILE`). |
| Everything else (`GRANT`, `CALL`, `EXPORT DATA`, procedures, ...) | `operations` | Verbatim. |

### Worked example: `CREATE ... AS` with metadata

```sql
CREATE OR REPLACE TABLE analytics.daily_sales
PARTITION BY DATE(order_ts)
CLUSTER BY region, product_id
OPTIONS(description = 'Daily sales rollup', require_partition_filter = true)
AS
SELECT region, product_id, SUM(amount) AS total
FROM raw.orders
GROUP BY region, product_id;
```

becomes:

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

Note how `PARTITION BY DATE(order_ts)` is preserved as the **raw expression
text**, `CLUSTER BY` becomes a string array, and the `description` option is
promoted to a top-level config key.

## `INSERT` -> `incremental`: explicit migration strategy

Dataform's incremental type appends this query's rows on normal runs, but
**creates the table from the query** on first run and may rebuild it during a
full refresh. Since that is not equivalent to an imperative `INSERT` in every
environment, the semantics-preserving default is `operations`. When the user
explicitly selects `--insert-strategy incremental`, sql2sqlx:

- emits `protected: true` by default (disable with `--no-protected`) so an
  accidental full refresh cannot drop pre-existing rows;
- records `INSERT_INCREMENTAL` suggesting `${when(incremental(), ...)}`
  around date filters;
- when the `INSERT` has a **column list**, rewrites the select list so its
  output names match (`INSERT INTO t (a) SELECT x` -> `SELECT x AS a`) -
  and only when every item's output name is *exactly determinable* at the
  token level. `*` expansion, `SELECT AS STRUCT`, ambiguous
  `INTERVAL 1 DAY` items or arity mismatches trigger the operations
  fallback (`FALLBACK_SELECT_ALIAS`) instead of a guess;
- rejects alias changes when a later `GROUP BY`, `HAVING`, `QUALIFY`,
  `ORDER BY` or pipe stage references the old alias.
- requires every incremental query output name to be exactly derivable,
  unique (case-insensitively), and safe for Dataform's runtime backtick
  quoting. An unexpanded `*`, an anonymous expression, duplicate names, or
  an identifier containing a decoded backtick/backslash/control character
  falls back instead of relying on warehouse metadata guesses.

For example, with `--insert-strategy incremental`:

```sql
INSERT INTO analytics.events (event_id, ts)
SELECT id, created_at FROM raw.events;
```

converts to (note the select list is aliased to the target column names):

```text
config {
  type: "incremental",
  schema: "analytics",
  name: "events",
  protected: true
}

-- source: input.sql:1 (INSERT converted by sql2sqlx v0.1.0)
SELECT id AS event_id, created_at AS ts FROM raw.events
```

On later runs Dataform projects **every column in the existing target
metadata by name** from the incremental query. The `INSERT_INCREMENTAL`
warning therefore requires the operator to verify that the query output
matches the complete target schema; an original positional/subset INSERT
does not establish that fact. This strategy is explicitly opt-in because its
first-run/full-refresh behavior and this schema contract are not equivalent
to an imperative INSERT in every environment.

## `MERGE` -> `incremental` proof obligations and schema precondition

With `--merge-strategy incremental-when-safe`, a MERGE converts **only**
when *all* of the following hold (otherwise: `operations` +
`MERGE_FALLBACK` with the exact reason):

1. the source is an aliased subquery;
2. `ON` is a pure conjunction of same-named `target.k = source.k`
   equalities;
3. exactly `WHEN MATCHED THEN UPDATE SET` with only direct same-named
   `col = source.col` assignments (no conditions, no expressions), including
   every unique-key column because Dataform updates those columns too;
4. exactly `WHEN NOT MATCHED [BY TARGET] THEN INSERT` with an explicit
   matching column/value list (`INSERT ROW` is order-dependent and rejected);
5. the subquery's derivable output columns are **exactly** the key
   columns plus the updated columns, in the explicit INSERT column order;
6. every output/key name is a simple, unreserved identifier because Dataform
   emits unique-key and source-side update references without backticks.

A MERGE that satisfies all six:

```sql
MERGE analytics.dim_users T
USING (SELECT id, name, email FROM raw.users) S
ON T.id = S.id
WHEN MATCHED THEN UPDATE SET id = S.id, name = S.name, email = S.email
WHEN NOT MATCHED THEN INSERT (id, name, email) VALUES (S.id, S.name, S.email);
```

converts to an incremental with a `uniqueKey`, and emits **two** warnings -
the conversion note and the schema precondition:

```text
config {
  type: "incremental",
  schema: "analytics",
  name: "dim_users",
  protected: true,
  uniqueKey: ["id"]
}

-- source: input.sql:1 (MERGE converted by sql2sqlx v0.1.0)
SELECT id, name, email FROM raw.users
```

Under those conditions the MERGE that Dataform generates for
`type: "incremental"` + `uniqueKey` has identical row-level effects **when
the existing target's columns exactly match the source output**. Dataform
builds the generated assignments from live target metadata, which SQL text
alone cannot prove. Every conversion therefore also reports
`TARGET_SCHEMA_REQUIRED`; verify the complete target column set before its
first incremental run. The default `operations` strategy has no such
precondition.

Common reasons a MERGE stays `operations` (`MERGE_FALLBACK`): the `UPDATE SET`
omits a unique-key column, uses a condition or an expression; the `ON` clause
is not a pure same-named equality conjunction; `INSERT ROW` is used; or the
source is not an aliased subquery.

## Metadata mapping

| SQL | Dataform config |
| --- | --- |
| `PARTITION BY expr` | `bigquery.partitionBy` (raw expression text) |
| `CLUSTER BY a, b` | `bigquery.clusterBy: ["a", "b"]` |
| `OPTIONS(description = '...')` | `description` |
| `OPTIONS(labels = [("k","v")])` | `bigquery.labels` |
| `OPTIONS(partition_expiration_days = n)` | `bigquery.partitionExpirationDays` |
| `OPTIONS(require_partition_filter = b)` | `bigquery.requirePartitionFilter` |
| any other option, or a non-literal value | `bigquery.additionalOptions` with the **raw SQL text** preserved |
| view column `OPTIONS(description)` | `config.columns` |

Options that do not have a dedicated Dataform key - or whose value is a
non-literal expression - are preserved verbatim under
`bigquery.additionalOptions` as raw SQL text, so nothing is silently dropped.
For instance a materialized view:

```sql
CREATE MATERIALIZED VIEW mart.mv
OPTIONS(enable_refresh = true, refresh_interval_minutes = 60)
AS SELECT k, COUNT(*) AS n FROM raw.t GROUP BY k;
```

produces:

```text
config {
  type: "view",
  schema: "mart",
  name: "mv",
  materialized: true,
  bigquery: {
    additionalOptions: {
      enable_refresh: "true",
      refresh_interval_minutes: "60"
    }
  }
}
```

## References and dependencies

These rules are summarized here and covered in depth, with examples, in the
[dependency model](dependencies.md).

- Every read of a table that some statement **produces** becomes
  `${ref(...)}`, carrying the producer's own qualification
  (`ref("t")`, `ref("schema","t")`, or the object form with `database`).
- Reads additionally gain `dependencies` on the latest writer that precedes
  the reader, so a future mutation is never pulled before an earlier read.
- Within one source file, a later writer also depends on readers since the
  prior write, preserving read-before-write order under parallel scheduling.
- If a read occurs before the table's eventual owner, it stays literal and
  reports `FUTURE_CREATOR`; a generated `ref()` would reverse corpus order.
- Writers of the same table are chained in corpus order (sorted file
  path, then position); cross-file chains raise `ORDER_ASSUMED`.
- Only operations that actually create a table/view can be elected with
  `hasOutput: true` and `${self()}`. Mutating DML is never misdeclared as a
  producer; `--declare-external` can declare an existing ownerless target.
- Duplicate producers of one target are demoted to ordered verbatim
  operations (`DUPLICATE_TARGET`) - Dataform allows one owner per target.
- CTE names, table aliases (including `alias.column` paths), `UNNEST`,
  table-valued functions, `EXTRACT(... FROM ...)` and the statement's own
  target are never rewritten.
- `--declare-external` synthesizes `type: "declaration"` files under
  `sources/` for referenced-but-never-produced tables
  (`INFORMATION_SCHEMA`, `region-*`, wildcard tables and table decorators
  excluded) and refs them too. Unqualified sources are declarable when
  `--default-dataset` resolves them.
- A reference or manual ordering edge that would create a cycle is omitted,
  the table path remains literal, and `DEPENDENCY_CYCLE` is reported.

## Warning codes

Every warning is a stable `code` plus a specific message and source location.
Gate and filter on the `code`. See [the report reference](report.md) for the
JSON shape and a triage workflow.

| Code | Meaning |
| --- | --- |
| `FALLBACK_OPERATIONS` | Statement kept verbatim; reason in message |
| `FALLBACK_SELECT_ALIAS` | Column-list rewrite not provably safe |
| `COLUMN_DDL` | CTAS with typed column list |
| `TEMP_TABLE` / `EXTERNAL_TABLE` / `SNAPSHOT_TABLE` | Special CREATE forms kept as operations |
| `IF_NOT_EXISTS` | Create-if-absent guard lost (or preserved per option) |
| `CREATE_REPLACE_SEMANTICS` | Typed Dataform action rebuilds a non-`OR REPLACE` target on later runs |
| `CREATE_NO_AS` | Plain CREATE TABLE kept as operations |
| `RERUN_RISK` | Plain CREATE without OR REPLACE / IF NOT EXISTS fails on re-run |
| `DECLARATION_DROPPED_DDL` | Plain CREATE emitted as a declaration |
| `INSERT_VALUES` | INSERT without a query body |
| `INSERT_INCREMENTAL` | Semantics note for the incremental conversion |
| `MERGE_INCREMENTAL` / `MERGE_FALLBACK` | MERGE shape result / fallback reason |
| `TARGET_SCHEMA_REQUIRED` | Converted MERGE requires exact target/query schema compatibility |
| `PROCEDURE_PRESERVED` | Stored procedure body kept verbatim and not treated as immediately executed DML |
| `DYNAMIC_SIDE_EFFECTS` | `CALL` or dynamic SQL may need manually declared dependencies |
| `ORPHAN_SELECT` | Standalone query needs review |
| `SCRIPT_FILE` / `SCRIPT_WRITES` | Whole-file script preserved; targets it writes |
| `SELF_REFERENCE` | Statement reads its own target; left literal |
| `DUPLICATE_TARGET` | Second producer demoted to operations |
| `ORDER_ASSUMED` | Multi-file writer order inferred from sorted paths |
| `FUTURE_CREATOR` | Read left literal because its eventual owner occurs later in corpus order |
| `DEPENDENCY_CYCLE` | Dependency omitted and reference left literal to keep the Dataform graph acyclic |
