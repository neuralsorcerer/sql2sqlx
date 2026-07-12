# sql2sqlx

**Convert BigQuery SQL into Dataform SQLX with conservative, dependency-aware migration tooling.**

`sql2sqlx` turns plain `.sql` pipelines (DDL, DML and BigQuery scripts)
into a complete Dataform project: typed actions
(`table` / `view` / `incremental` / `operations` / `declaration`),
`${ref(...)}` dependency wiring, metadata mapping (`PARTITION BY`,
`CLUSTER BY`, `OPTIONS(...)`), and a machine-readable conversion report. SQL that is not explicitly rewritten is retained character-for-character after decoding.

```bash
pip install sql2sqlx
sql2sqlx ./legacy_sql -o ./dataform/definitions --report report.json --init-project
```

```{toctree}
:maxdepth: 2
:caption: Using sql2sqlx

installation
quickstart
examples
cli
conversion_rules
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
changelog
```

