# Examples and notebooks

These notebooks mirror the runnable examples shipped in the repository and are
checked into the documentation with executed outputs.
They are intended as guided, browser-friendly walkthroughs for common
`sql2sqlx` migration tasks, so readers can inspect both the code and the
rendered results on the website.

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
