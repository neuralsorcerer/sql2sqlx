# Examples and notebooks

These notebooks mirror the runnable examples shipped in the repository and are
checked into the documentation with executed outputs. They are guided,
browser-friendly walkthroughs for common `sql2sqlx` migration tasks, so
readers can inspect both the code and the rendered results on the website.

```{toctree}
:maxdepth: 1
:caption: Notebook walkthroughs

notebooks/01_in_memory_and_file_conversion
notebooks/02_directory_options_and_dependencies
notebooks/03_reports_benchmark_and_review_gates
```

## Script examples

The repository also includes standalone scripts under [`examples/`](../examples):

- `01_convert_string.py` demonstrates in-memory conversion.
- `02_convert_file.py` converts one SQL file.
- `03_convert_directory.py` converts an input directory.
- `04_custom_options.py` shows option customization.
- `05_programmatic_report.py` inspects conversion reports.
- `06_benchmark.py` generates benchmark corpora.

## Copy-paste recipes

Short, self-contained snippets for the most common tasks. All use the public
API described in the [API reference](api.md).

### Convert one statement and print it

```python
from sql2sqlx import convert_string

result = convert_string("CREATE OR REPLACE TABLE ds.t AS SELECT 1 AS x;")
print(result.files[0].content)
```

### Convert a tree and write it, with options

```python
from sql2sqlx import ConversionOptions, convert_directory

convert_directory(
    "legacy_sql",
    "project/definitions",
    ConversionOptions(
        default_project="my-gcp-project",
        default_dataset="analytics",
        insert_strategy="incremental",
        merge_strategy="incremental-when-safe",
        declare_external=True,
        tags=["migrated"],
    ),
)
```

### Convert without writing, then inspect and write yourself

```python
from sql2sqlx import convert_directory, write_result

result = convert_directory("legacy_sql")   # output_dir omitted -> nothing written
print(result.report.actions_by_type)
for f in result.files:
    print(f.relpath, f.action_type.value, f.action_name)

write_result(result, "project/definitions")  # write when you are ready
```

### Group warnings by code

```python
from collections import Counter
from sql2sqlx import convert_directory

report = convert_directory("legacy_sql").report
by_code = Counter(w.code for w in report.warnings)
for code, n in by_code.most_common():
    print(f"{n:4}  {code}")
```

### Fail CI on parse failures or a disallowed code

```python
import sys
from sql2sqlx import convert_directory

report = convert_directory("legacy_sql").report
blocking = {"DEPENDENCY_CYCLE"}
if report.failures or any(w.code in blocking for w in report.warnings):
    sys.exit(1)
```

See [the report reference](report.md) for the full report structure and
[conversion rules](conversion_rules.md#warning-codes) for every warning code.
