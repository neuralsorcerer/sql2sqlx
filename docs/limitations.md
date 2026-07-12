# Limitations and guarantees

## Guarantees

1. **Semantics-preserving defaults.** Statements that cannot be converted
   provably safely become verbatim `operations` actions - the SQL that runs
   is the SQL you wrote. Explicitly selected migration strategies report
   their changed first-run/guard behavior and runtime preconditions.
2. **Character-for-character source fidelity** for all SQL outside explicit rewrites
   (references, select-list aliases, `${self()}`, and constant SQLX
   placeholders used to preserve literal `${` text). Generated SQLX is UTF-8.
3. **Deterministic output**: identical inputs and options yield
   byte-identical trees, whatever the worker count.
4. **Failure isolation**: a broken file is reported and skipped; the
   rest of the corpus converts.

## Known limitations

- **Input must be valid GoogleSQL.** The converter is not a validator;
  clearly broken input either raises a located `LexError` (reported per
  file) or passes through inside `operations` bodies.
- **Cross-file writer ordering is heuristic.** When several files write
  one table, their chain follows sorted file paths (`ORDER_ASSUMED`).
  Within a file, statically identified read/write conflicts preserve original
  statement order; hidden effects from dynamic SQL or procedures cannot.
- **Single-part name resolution.** Unqualified `FROM t` matches an
  unqualified producer `t` (or a qualified one via `--default-dataset`).
  Query-scoped CTE and range-variable analysis removes relational aliases;
  unresolved physical-name ambiguity still requires an appropriate default
  dataset or a qualified source path.
- **`TABLE table_path` arguments inside table functions**
  (e.g. `ML.PREDICT(MODEL m, TABLE d.t)`) are not rewritten.
- **Scripts stay whole.** Files that use transactions, temporary objects,
  variables, procedural control flow, calls or dynamic SQL become one
  operations action; their internal statements are not individually lifted
  (Dataform runs the body in one BigQuery context).
- **Dynamic side effects require review.** SQL inside `EXECUTE IMMEDIATE` and
  writes performed by a called procedure cannot be inferred statically. The
  statements remain verbatim, but their hidden read/write dependencies are not
  synthesized.
- **`CREATE TABLE AS` column lists** and `DEFAULT COLLATE` have no
  Dataform config equivalent and fall back to operations.
- **Opt-in incrementals require schema review.** Dataform derives incremental
  INSERT/MERGE projections from the existing target metadata. SQL text alone
  cannot prove that a pre-existing target's complete column set matches the
  converted query, so `INSERT_INCREMENTAL` and `TARGET_SCHEMA_REQUIRED`
  explicitly flag that operator check. Defaults keep both statements
  verbatim.
- **Wildcard tables and decorators** (for example `` `ds.events_*` `` and
  `` `ds.events$20240101` ``) are table expressions, not declarable sources,
  and are left as literals.
- **Future owners never reverse source order.** Earlier reads stay literal and
  report `FUTURE_CREATOR`; any remaining cycle-producing edge is also omitted
  with `DEPENDENCY_CYCLE`.
- Dataform compiles and validates the final project; run
  `dataform compile` after conversion as the last gate.
