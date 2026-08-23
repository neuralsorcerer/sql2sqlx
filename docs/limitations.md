# Limitations and guarantees

This page states precisely what `sql2sqlx` promises, and where static analysis
of SQL text is fundamentally not enough - so you know exactly what to review.

## Guarantees

1. **Semantics-preserving defaults.** Statements that cannot be converted
   provably safely become verbatim `operations` actions - the SQL that runs
   is the SQL you wrote. Explicitly selected migration strategies report
   their changed first-run/guard behavior and runtime preconditions.
2. **Character-for-character source fidelity** for all SQL outside explicit
   rewrites (references, select-list aliases, `${self()}`, and the constant
   SQLX placeholders used to preserve literal `${` text and neutralize
   SQLX-unsafe comments). Generated SQLX is UTF-8, and files are written
   without newline translation: the structure sql2sqlx adds (the `config`
   block, blank lines, joined comments) uses `\n`, while newlines *inside*
   preserved source spans are carried through unchanged - a CRLF source
   therefore keeps CRLF inside its body, which is what character-for-character
   fidelity requires.
3. **Deterministic output**: identical inputs and options yield byte-identical
   trees, whatever the worker count.
4. **Failure isolation**: a broken file is reported and skipped; the rest of
   the corpus converts.

These follow directly from the design - see [core concepts](concepts.md) for
the span-edit model and [architecture](architecture.md) for how determinism is
maintained across parallel workers.

## Known limitations

### Input must be valid GoogleSQL

The converter is not a validator. Clearly broken input either raises a located
`LexError` (reported per file in `failures`) or, if it lexes but is not
recognized, passes through inside an `operations` body. It does not attempt to
diagnose semantic SQL errors - `dataform compile` and BigQuery are the
authorities on validity.

### Cross-file writer ordering is heuristic

When several files write one table, their chain follows sorted file paths
(`ORDER_ASSUMED`), because cross-file execution order is not encoded in the
SQL. Within a single file, statically identified read/write conflicts preserve
original statement order; hidden effects from dynamic SQL or procedures cannot
be seen and so cannot be ordered.

### Single-part name resolution

Unqualified `FROM t` matches an unqualified producer `t`, or a qualified
producer via `--default-dataset`. Query-scoped CTE and range-variable analysis
removes relational aliases, but physical-name ambiguity that a default cannot
resolve is left literal; supply an appropriate default dataset or qualify the
source path.

### `TABLE table_path` arguments inside table functions

Arguments of the form `TABLE d.t` inside a table-valued function call (e.g.
`ML.PREDICT(MODEL m, TABLE d.t)`) are not rewritten to `ref()`.

### Scripts stay whole

Files that use transactions, temporary objects, variables, procedural control
flow, calls or dynamic SQL become **one** `operations` action; their internal
statements are not individually lifted, because Dataform runs the body in a
single BigQuery script context and splitting would change scope and
control-flow semantics. Reference sites and write targets inside the script are
still harvested for dependency wiring.

### Dynamic side effects require review

SQL inside `EXECUTE IMMEDIATE`, and writes performed by a called procedure,
cannot be inferred statically (`DYNAMIC_SIDE_EFFECTS`). The statements remain
verbatim, but their hidden read/write dependencies are not synthesized - add
any needed `dependencies` by hand.

### Some DDL has no Dataform config equivalent

`CREATE TABLE AS` column lists (`COLUMN_DDL`) and `DEFAULT COLLATE` cannot be
represented in a typed action's `config` and fall back to operations. Special
create forms (`LIKE`/`CLONE`/`COPY`, temp/external/snapshot) likewise stay
verbatim.

### Opt-in incrementals require schema review

Dataform derives incremental `INSERT`/`MERGE` projections from the existing
target metadata. SQL text alone cannot prove that a pre-existing target's
complete column set matches the converted query, so `INSERT_INCREMENTAL` and
`TARGET_SCHEMA_REQUIRED` explicitly flag that operator check. The defaults keep
both statement classes verbatim.

### Wildcard tables and decorators are not sources

Wildcard tables (for example `` `ds.events_*` ``) and table decorators
(`` `ds.events$20240101` ``) are table *expressions*, not declarable objects,
and are left as literals even under `--declare-external`.

### Future owners never reverse source order

An earlier read of a table whose owner appears later in corpus order stays
literal (`FUTURE_CREATOR`) rather than reordering your pipeline; any remaining
cycle-producing edge is omitted with `DEPENDENCY_CYCLE`.

## The final gate

Dataform compiles and validates the final project. Run `dataform compile`
after conversion as the last gate - it is the only thing that can confirm the
entire dependency graph resolves against your warehouse's real schemas.
