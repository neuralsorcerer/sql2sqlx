# API reference

Everything in this section is generated from the package's docstrings. The
narrative below orients you before the autodoc entries.

The public surface is small and stable. Everything importable from the
top-level `sql2sqlx` package (listed in its `__all__`) is what semantic
versioning covers.

## Choosing an entry point

Three functions convert; two helpers support them.

:::{list-table}
:header-rows: 1
:widths: 26 30 44

* - Function
  - Input
  - Writes to disk?
* - `convert_string(sql, options=None, name="input.sql")`
  - a SQL string (one or many statements)
  - No - returns a result you can inspect or `write_result`.
* - `convert_file(path, options=None)`
  - one `.sql` file
  - No - returns a result.
* - `convert_directory(input_dir, output_dir=None, options=None)`
  - a directory tree (scanned by `include_glob`)
  - **Only if** `output_dir` is given.
:::

`parse_source(...)` exposes just Phase 1 (lex + split + classify) for one
source, returning a `ParsedFile`; `write_result(result, output_dir)` writes a
`ConversionResult` to disk (and refuses paths that would escape the
destination). All three converters run the same three-phase pipeline described
in [architecture](architecture.md); `convert_directory` additionally
parallelizes parsing across a process pool.

```{eval-rst}
.. autofunction:: sql2sqlx.convert_string
.. autofunction:: sql2sqlx.convert_file
.. autofunction:: sql2sqlx.convert_directory
.. autofunction:: sql2sqlx.write_result
.. autofunction:: sql2sqlx.parse_source
```

## Options and results

`ConversionOptions` holds every knob; its defaults preserve semantics (see the
[CLI reference](cli.md) for the flag-to-field mapping). Its `__post_init__`
**validates and normalizes** on construction: the string enum values
(`"incremental"`, `"mirror"`, ...) are accepted as well as the enum members, so
options are easy to build from JSON or config files, and invalid values raise
`ValueError` immediately rather than silently mis-selecting a strategy.

A conversion returns a `ConversionResult` with two attributes: `files` (the
generated `SqlxFile` objects, in deterministic sorted-path order) and `report`
(a [`ConversionReport`](report.md)). All of these are plain, picklable
dataclasses.

```{eval-rst}
.. autoclass:: sql2sqlx.ConversionOptions
   :members:

.. autoclass:: sql2sqlx.InsertStrategy
   :members:
.. autoclass:: sql2sqlx.MergeStrategy
   :members:
.. autoclass:: sql2sqlx.PlainCreateStrategy
   :members:
.. autoclass:: sql2sqlx.IfNotExistsStrategy
   :members:
.. autoclass:: sql2sqlx.Layout
   :members:

.. autoclass:: sql2sqlx.ConversionResult
   :members:
.. autoclass:: sql2sqlx.ConversionReport
   :members:
.. autoclass:: sql2sqlx.ReportWarning
   :members:
.. autoclass:: sql2sqlx.SqlxFile
   :members:
.. autoclass:: sql2sqlx.ActionType
   :members:
.. autoclass:: sql2sqlx.TableName
   :members:
```

## Errors

Every exception raised deliberately by the library derives from
`Sql2SqlxError`, so a single `except Sql2SqlxError` catches them all.
Location-aware errors (`LexError`, `SplitError`) carry 1-based `line` and
`column` attributes. Note that *unsupported-but-valid* SQL never raises - it
falls back to an `operations` action and a warning; the exceptions are for
genuinely broken input (unreadable files, lexer failures). During directory
conversion, a per-file failure is captured in `report.failures` rather than
aborting the run.

```{eval-rst}
.. automodule:: sql2sqlx.errors
   :members:
```

## Internals

The internal modules are stable enough to build on, but the public surface
above is what semantic versioning covers.

```{eval-rst}
.. automodule:: sql2sqlx.lexer
   :members: tokenize, comment_spans, unquote_identifier, Token, LineIndex

.. automodule:: sql2sqlx.splitter
   :members:

.. automodule:: sql2sqlx.parser
   :members: classify_statement

.. automodule:: sql2sqlx.refs
   :members: scan_ref_sites, parse_table_path, PathMatch

.. automodule:: sql2sqlx.emitter
   :members:

.. automodule:: sql2sqlx.cli
   :members: main, build_arg_parser
```
