# Installation

`sql2sqlx` is a pure-Python package with **zero runtime dependencies**. It
runs on **Python 3.10 through 3.14** on Linux, macOS and Windows, and ships
type information (`py.typed`), so it type-checks cleanly in downstream projects.

The zero-dependency design is deliberate: the converter is meant to run in
migration scripts and CI without pulling a dependency tree, and its lexer and
parser are implemented directly on the Python standard library.

## From PyPI

```bash
pip install sql2sqlx
```

This installs two things:

- the **`sql2sqlx` console command** (see the [CLI reference](cli.md));
- the **`sql2sqlx` Python package** (see the [API reference](api.md)).

To upgrade later:

```bash
pip install --upgrade sql2sqlx
```

:::{tip}
Installing into a virtual environment (or with `pipx install sql2sqlx` for a
CLI-only install) keeps it isolated from your system Python.
:::

## From source

Clone the repository and install in editable mode. The project defines three
optional dependency groups:

:::{list-table}
:header-rows: 1
:widths: 20 80

* - Extra
  - Contents
* - `dev`
  - `pytest`, `black`, `isort`, `mypy`, `pre-commit` - the test and lint
    toolchain.
* - `docs`
  - `sphinx`, `furo`, `myst-parser`, `myst-nb` - to build this documentation.
* - `examples`
  - `jupyter`, `matplotlib` - to run the notebooks and benchmark helper under
    `examples/`.
:::

```bash
git clone https://github.com/neuralsorcerer/sql2sqlx
cd sql2sqlx
pip install -e ".[dev,docs]"
pytest                 # run the test suite
```

To build the documentation locally after installing the `docs` extra:

```bash
sphinx-build -b html docs docs/_build/html
# or: make -C docs html
```

## Verifying the install

```bash
sql2sqlx --version
python -c "import sql2sqlx; print(sql2sqlx.__version__)"
```

Both should print the same version string. A quick end-to-end smoke test
(the CLI reads a file or a directory - it does not read from a pipe):

```bash
echo "CREATE OR REPLACE TABLE ds.t AS SELECT 1 AS x;" > /tmp/smoke.sql
sql2sqlx /tmp/smoke.sql
```

which prints a `type: "table"` action to stdout.

## What gets installed where

- `sql2sqlx` - the importable package (entry points `convert_string`,
  `convert_file`, `convert_directory`, plus the data model and errors).
- `sql2sqlx` (console script) - mapped to `sql2sqlx.cli:main`.
- `python -m sql2sqlx` - the same CLI, runnable as a module.

Nothing else is added to your environment: there are no transitive runtime
dependencies to audit.
