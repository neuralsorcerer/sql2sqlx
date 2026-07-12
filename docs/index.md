# sql2sqlx

**Convert BigQuery SQL into Dataform SQLX with conservative, dependency-aware
migration tooling.**

`sql2sqlx` turns plain `.sql` pipelines - DDL, DML and BigQuery scripts - into
a complete Dataform project:

- **typed actions** - `table`, `view`, `incremental`, `operations` and
  `declaration`, chosen per statement;
- **`${ref(...)}` dependency wiring** - every read of a table another
  statement produces is rewritten, and ordering is preserved with
  `dependencies` edges;
- **metadata mapping** - `PARTITION BY`, `CLUSTER BY` and `OPTIONS(...)` map to
  the Dataform `config` block;
- **a machine-readable report** - every decision, fallback and warning, ready
  to triage or gate CI on.

SQL that is not explicitly rewritten is retained **character-for-character**
after decoding, and the defaults never change what your SQL does.

```bash
pip install sql2sqlx
sql2sqlx ./legacy_sql -o ./dataform/definitions --report report.json --init-project
```

## Why it is safe to run

The whole tool is built on one rule: **convert only when it is provably safe,
otherwise keep the statement verbatim and record a warning.** A verbatim
`operations` action runs your original SQL unchanged, so a first conversion
never alters behavior - it gives you a working Dataform project on day one,
which you then make more idiomatic by opting into typed strategies you have
validated. See [core concepts](concepts.md) for the output anatomy and
[limitations & guarantees](limitations.md) for the precise contract.

## Where to go next

- New here? Start with [installation](installation.md) and the
  [quick start](quickstart.md).
- Migrating a real codebase? Follow the [migration guide](migration_guide.md).
- Want to know exactly how a statement converts? See the
  [conversion rules](conversion_rules.md).
- Building on the Python API? See the [API reference](api.md).

```{toctree}
:maxdepth: 2
:caption: Using sql2sqlx

installation
quickstart
migration_guide
cli
examples
```

```{toctree}
:maxdepth: 2
:caption: Concepts & rules

concepts
conversion_rules
dependencies
report
```

```{toctree}
:maxdepth: 2
:caption: Under the hood

architecture
limitations
api
```

```{toctree}
:maxdepth: 1
:caption: Project

contributing
```
