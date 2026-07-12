# References and the dependency graph

Dataform executes actions as a DAG: a node runs only after the nodes it
depends on. `sql2sqlx` reconstructs that graph from plain SQL by answering two
questions for every statement:

1. **Which tables does it read**, and are any of those produced elsewhere in
   the corpus? Each such read becomes a `${ref(...)}` call.
2. **Which tables does it write**, and in what order relative to other
   statements? That ordering becomes `dependencies` edges.

This page explains exactly how those edges are derived, and how to read the
warnings that flag the cases where SQL text alone is not enough to be sure.

## Producers, writers and readers

Every statement is classified by its relationship to a target table:

Producer (creator)
: A statement that *creates or replaces* a table or view -
  `CREATE [OR REPLACE] TABLE/VIEW ... AS`, and an `INSERT`/`MERGE` you have
  opted to convert to `incremental`. Dataform allows exactly **one owner per
  target**; the producer is that owner.

Writer (mutator)
: A statement that *changes* an existing table without owning it -
  `UPDATE`, `DELETE`, `TRUNCATE`, `MERGE`/`INSERT` kept as `operations`,
  `DROP`, `ALTER`, `LOAD DATA`, and so on. These stay verbatim `operations`
  actions but are still tracked as writers of their target so ordering is
  preserved.

Reader
: A statement that reads a table in a `FROM`/`JOIN`/`USING` position.

## Reference rewriting

When a reader reads a table that some producer in the corpus owns, the read
site is rewritten to a `${ref(...)}` call carrying the **producer's own
qualification** (see [core concepts](concepts.md#reference-and-self-forms)).
Dataform derives an implicit dependency on the owner from the `ref()` itself,
so no explicit `dependencies` entry is needed just to depend on the owner.

`${ref(...)}` is emitted **only** for tables the corpus actually produces.
A read of a table nobody produces is left as a literal path (it is an external
source). Pass `--declare-external` to turn such sources into `declaration`
actions and rewrite those reads to `ref()` too (see below).

### What is never rewritten

The reference scanner tracks query scope so that only genuine physical table
paths are rewritten. The following are deliberately left untouched, because
they are not references to a corpus table:

- **CTE names** and their uses (`WITH t AS (...) SELECT * FROM t`);
- **table aliases** and alias-qualified column paths (`a.col` where `a` is a
  range variable);
- **`UNNEST(...)`** operands and their aliases;
- **table-valued function** calls and `TABLE table_path` arguments inside
  table functions (e.g. `ML.PREDICT(MODEL m, TABLE d.t)`);
- **`EXTRACT(part FROM col)`** - the `FROM` here is not a table source;
- a statement's **own target** (that is handled by `${self()}`, not `ref()`);
- **`INFORMATION_SCHEMA`** views, **`region-*`** meta tables, **wildcard
  tables** (`` `ds.events_*` ``) and **table decorators** (`` `ds.t$20240101` ``).

The scanner understands recursive vs. ordered CTE visibility, correlated
scalar subqueries vs. non-lateral `FROM` subqueries, set-operation branches
(`UNION`/`INTERSECT`/`EXCEPT`), implicit aliases and dashed-project adjacency,
all without ever re-serializing the SQL.

## Ordering dependencies

`${ref()}` handles "read the current contents of an owned table". Everything
else about *ordering* is expressed with explicit `dependencies` entries, whose
values are:

- the owner's **qualified path** (`analytics.t`, or `proj.ds.t`) when the
  dependency is on a producer, matching what a `ref()` resolves to; or
- the depended-on action's **name** (`t_delete`) when it is a non-owner
  writer or a script.

Three rules build these edges. All of them operate over a stable **corpus
order**: files sorted by relative path, then statements by position.

### 1. Writer chains

Every writer of a table depends on the previous writer of that same table, so
a create/mutate/mutate sequence keeps its original order:

```sql
CREATE OR REPLACE TABLE analytics.t AS SELECT 1 AS id;
DELETE FROM analytics.t WHERE id = 0;
```

The `DELETE` becomes an `operations` action that depends on the table it
mutates:

```text
config {
  type: "operations",
  name: "t_delete",
  dependencies: ["analytics.t"]
}
```

### 2. Read-before-write ordering

Within one file, a later writer also depends on every reader that read the
table *since the previous write*. This preserves "read the old value, then
mutate" order even under Dataform's parallel scheduling:

```sql
CREATE OR REPLACE TABLE analytics.src AS SELECT 1 AS id;   -- producer
CREATE OR REPLACE VIEW  analytics.rpt AS
  SELECT * FROM analytics.src;                             -- reader
DELETE FROM analytics.src WHERE id = 0;                    -- later writer
```

The `DELETE` waits for both the producer *and* the intervening reader:

```text
config {
  type: "operations",
  name: "src_delete",
  dependencies: ["analytics.rpt", "analytics.src"]
}
```

while `rpt` reads `${ref("analytics", "src")}` and depends on `src`
implicitly through that `ref()`.

### 3. Reader dependencies on the latest writer

A reader depends (beyond the owning `ref()`) on the **latest writer that
precedes it**, so a read never floats ahead of a mutation that was written
before it. In the writer-chain example above, if a view read `analytics.t`
*after* the `DELETE`, it would gain `dependencies: ["t_delete"]` in addition
to `${ref("analytics", "t")}`.

## Edges the tool refuses to invent

SQL text cannot always prove a safe edge exists. In these cases `sql2sqlx`
chooses the conservative option - leave the reference literal and/or omit the
edge - and records a warning rather than emitting a graph that would reorder
your pipeline or fail to compile.

`FUTURE_CREATOR`
: A read occurs *before* the table's eventual owner in corpus order.
  Rewriting it to `ref()` would pull that future creator ahead of the read
  and reverse your intended order, so the read stays literal. (The owner
  still gains an ordering dependency on the earlier reader.)

  ```sql
  CREATE OR REPLACE VIEW analytics.early AS
    SELECT * FROM analytics.late;      -- read stays literal (FUTURE_CREATOR)
  CREATE OR REPLACE TABLE analytics.late AS SELECT 1 AS id;
  ```

`SELF_REFERENCE`
: A statement reads its own target inside its defining query. Dataform cannot
  `ref()` an action into itself, so the reference is left literal.

`DUPLICATE_TARGET`
: A second statement produces a table another statement already owns. The
  duplicate is demoted to a verbatim `operations` action, ordered after the
  owner, because Dataform permits only one owner per target.

`DEPENDENCY_CYCLE`
: An edge (a `ref()` or a manual ordering edge) would introduce a cycle into
  the Dataform graph. The unsafe edge is omitted and the path left literal, so
  the compiled graph stays acyclic.

`ORDER_ASSUMED`
: Several *different files* write the same table. Their relative order is
  inferred from sorted file paths - a heuristic, because cross-file execution
  order is not encoded in the SQL itself. Review these if your build order
  depended on something other than lexical file order.

## `hasOutput` election

Some statements create a real table but cannot be expressed as a typed
`table`/`view` action - for example `CREATE SNAPSHOT TABLE ... CLONE`,
`CREATE TABLE ... LIKE`, or a guarded `CREATE TABLE IF NOT EXISTS ... AS` kept
as operations. When such an `operations` action is the sole creator of its
target, it is *elected* as a Dataform producer: it gains `hasOutput: true` and
its own target path is rewritten to `${self()}`, so downstream `ref()` calls
resolve to it and it participates in the graph like any other producer.

```text
config {
  type: "operations",
  schema: "backups",
  name: "users_snap",
  hasOutput: true
}

CREATE SNAPSHOT TABLE ${self()} CLONE analytics.users
```

Mutating DML (`UPDATE`/`DELETE`/`MERGE`/`INSERT`) is **never** elected as a
producer, even when it targets an ownerless table - a mutation is not a
creator, and mis-declaring it as one would corrupt the graph.

## Name resolution and defaults

References are matched to producers by resolved identity `(project, dataset,
table)`. `--default-project` and `--default-dataset` fill in missing
qualifiers **for matching purposes only**:

- An unqualified `FROM t` matches an unqualified producer `t`, or - when
  `--default-dataset ds` is set - a producer declared as `ds.t`.
- Defaults never appear in a target's `config` identity (`database`/`schema`
  are emitted only from explicit source qualification), but they do let a
  qualified producer and an unqualified read resolve to the same table.

Physical-name ambiguity that defaults cannot resolve is left literal; supply a
more specific default or qualify the source path.

## `--declare-external`: sources

By default a table nobody in the corpus produces is left as a literal path.
With `--declare-external`, `sql2sqlx` synthesizes a `type: "declaration"`
action under `sources/` for each referenced-but-never-produced table and
rewrites those reads to `ref()` as well:

```text
config {
  type: "declaration",
  schema: "raw",
  name: "events"
}
```

Only genuinely declarable sources are emitted. `INFORMATION_SCHEMA` views,
`region-*` meta tables, wildcard tables and table decorators are excluded
(they are table *expressions*, not declarable objects). An unqualified source
is declarable only when `--default-dataset` resolves it to a concrete dataset.
