# API reference

Everything documented here is generated from the package's docstrings.

## Entry points

```{eval-rst}
.. autofunction:: sql2sqlx.convert_string
.. autofunction:: sql2sqlx.convert_file
.. autofunction:: sql2sqlx.convert_directory
.. autofunction:: sql2sqlx.write_result
.. autofunction:: sql2sqlx.parse_source
```

## Options and results

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

```{eval-rst}
.. automodule:: sql2sqlx.errors
   :members:
```

## Internals

The internal modules are stable enough to build on, but the public
surface above is what semantic versioning covers.

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


