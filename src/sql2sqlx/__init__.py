# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""sql2sqlx - convert BigQuery SQL into Dataform SQLX.

A zero-dependency, robust converter that turns plain
``.sql`` pipelines (DDL, DML, scripts) into a Dataform project:
``type: "table"|"view"|"incremental"|"operations"|"declaration"``
actions with ``${ref(...)}`` dependency wiring, metadata mapping
(``PARTITION BY``, ``CLUSTER BY``, ``OPTIONS``), and a detailed
machine-readable conversion report.

Quick start::

    from sql2sqlx import convert_string

    result = convert_string(
        "CREATE OR REPLACE TABLE analytics.daily AS "
        "SELECT * FROM raw.events;"
    )
    print(result.files[0].content)

See :func:`convert_string`, :func:`convert_file` and
:func:`convert_directory` for the three entry points, and
:class:`ConversionOptions` for every knob. Full documentation lives in
the project's ``docs/`` (Sphinx) tree.
"""

from sql2sqlx.converter import (
    convert_directory,
    convert_file,
    convert_string,
    parse_source,
    write_result,
)
from sql2sqlx.errors import ConversionError, LexError, SplitError, Sql2SqlxError
from sql2sqlx.model import (
    ActionType,
    ConversionOptions,
    ConversionReport,
    ConversionResult,
    IfNotExistsStrategy,
    InsertStrategy,
    Layout,
    MergeStrategy,
    PlainCreateStrategy,
    ReportWarning,
    SqlxFile,
    TableName,
)
from sql2sqlx.version import __version__

__all__ = [
    "__version__",
    # entry points
    "convert_string",
    "convert_file",
    "convert_directory",
    "write_result",
    "parse_source",
    # options & enums
    "ConversionOptions",
    "InsertStrategy",
    "MergeStrategy",
    "PlainCreateStrategy",
    "IfNotExistsStrategy",
    "Layout",
    # results
    "ConversionResult",
    "ConversionReport",
    "ReportWarning",
    "SqlxFile",
    "ActionType",
    "TableName",
    # errors
    "Sql2SqlxError",
    "LexError",
    "SplitError",
    "ConversionError",
]
