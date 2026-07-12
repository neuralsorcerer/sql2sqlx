# sql2sqlx examples

Every script is self-contained and runnable from the repository root
after `pip install -e .` (or `pip install sql2sqlx`):

| Script | Shows |
| --- | --- |
| `01_convert_string.py` | Minimal in-memory conversion, metadata mapping, automatic `${ref()}` wiring |
| `02_convert_file.py` | Single-file conversion and reading warnings from the report |
| `03_convert_directory.py` | Full directory tree -> Dataform `definitions/` (mirrored layout) |
| `04_custom_options.py` | Safe-MERGE mode, external declarations, flat layout, tags |
| `05_programmatic_report.py` | Using the JSON report as a CI review gate |
| `06_benchmark.py` | Throughput measurement on a generated million-line corpus |

`sql/` contains a miniature but realistic BigQuery warehouse (raw DDL,
partitioned/clustered CTAS, a documented view, an appending INSERT, an
upsert MERGE, a variable-scoped script, and DML maintenance) that the
scripts convert. The equivalent CLI invocation for `03` is:

```bash
sql2sqlx examples/sql -o examples/output/definitions --init-project
```

## Notebooks

`notebooks/` provides a guided Jupyter version of the examples:

| Notebook | Shows |
| --- | --- |
| `01_in_memory_and_file_conversion.ipynb` | In-memory conversion, single-file conversion, SQLX inspection, and action-count charts |
| `02_directory_options_and_dependencies.ipynb` | Directory conversion, option strategies, dependency behavior, external declarations, and action-mix charts |
| `03_reports_benchmark_and_review_gates.ipynb` | Report JSON, warning-code review, small benchmark runs, and Matplotlib throughput charts |

Install notebook extras with `pip install -e ".[examples]"` before opening
them from the repository root.
