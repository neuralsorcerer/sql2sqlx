# Installation

`sql2sqlx` is a pure-Python package with **zero runtime dependencies**,
supporting Python 3.10 through 3.14 on Linux, macOS and Windows.

## From PyPI

```bash
pip install sql2sqlx
```

This installs the `sql2sqlx` console command and the `sql2sqlx` Python
package (fully typed; ships `py.typed`).

## From source

```bash
git clone https://github.com/neuralsorcerer/sql2sqlx
cd sql2sqlx
pip install -e ".[dev,docs]"
pytest        # run the test suite
```

## Verifying

```bash
sql2sqlx --version
python -c "import sql2sqlx; print(sql2sqlx.__version__)"
```

