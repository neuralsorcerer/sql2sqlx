# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""GoogleSQL reserved keywords and related token classification sets.

:data:`RESERVED` is the official GoogleSQL reserved-keyword list. It is
used to decide whether a bare identifier can be a table alias (reserved
words cannot be used as unquoted aliases), which keeps the reference
scanner from mistaking clause keywords for aliases.

:data:`FROM_CLAUSE_ENDERS` are keywords that terminate the table list of
a ``FROM`` clause at the same nesting depth; encountering one switches
the reference scanner out of "expecting a table path" mode.
"""

from __future__ import annotations

from typing import FrozenSet

#: Official GoogleSQL reserved keywords (case-insensitive).
RESERVED: FrozenSet[str] = frozenset(
    {
        "ALL",
        "AND",
        "ANY",
        "ARRAY",
        "AS",
        "ASC",
        "ASSERT_ROWS_MODIFIED",
        "AT",
        "BETWEEN",
        "BY",
        "CASE",
        "CAST",
        "COLLATE",
        "CONTAINS",
        "CREATE",
        "CROSS",
        "CUBE",
        "CURRENT",
        "DEFAULT",
        "DEFINE",
        "DESC",
        "DISTINCT",
        "ELSE",
        "END",
        "ENUM",
        "ESCAPE",
        "EXCEPT",
        "EXCLUDE",
        "EXISTS",
        "EXTRACT",
        "FALSE",
        "FETCH",
        "FOLLOWING",
        "FOR",
        "FROM",
        "FULL",
        "GRAPH_TABLE",
        "GROUP",
        "GROUPING",
        "GROUPS",
        "HASH",
        "HAVING",
        "IF",
        "IGNORE",
        "IN",
        "INNER",
        "INTERSECT",
        "INTERVAL",
        "INTO",
        "IS",
        "JOIN",
        "LATERAL",
        "LEFT",
        "LIKE",
        "LIMIT",
        "LOOKUP",
        "MERGE",
        "NATURAL",
        "NEW",
        "NO",
        "NOT",
        "NULL",
        "NULLS",
        "OF",
        "ON",
        "OR",
        "ORDER",
        "OUTER",
        "OVER",
        "PARTITION",
        "PRECEDING",
        "PROTO",
        "QUALIFY",
        "RANGE",
        "RECURSIVE",
        "RESPECT",
        "RIGHT",
        "ROLLUP",
        "ROWS",
        "SELECT",
        "SET",
        "SOME",
        "STRUCT",
        "TABLESAMPLE",
        "THEN",
        "TO",
        "TREAT",
        "TRUE",
        "UNBOUNDED",
        "UNION",
        "UNNEST",
        "USING",
        "WHEN",
        "WHERE",
        "WINDOW",
        "WITH",
        "WITHIN",
    }
)

#: Keywords that end the table list of a FROM clause at the same depth.
FROM_CLAUSE_ENDERS: FrozenSet[str] = frozenset(
    {
        "WHERE",
        "GROUP",
        "HAVING",
        "QUALIFY",
        "WINDOW",
        "ORDER",
        "LIMIT",
        "UNION",
        "INTERSECT",
        "EXCEPT",
        "SET",
        "WHEN",
        "THEN",
        "RETURNING",
    }
)

#: Date/time part identifiers; presence at top level of a select-list item
#: makes implicit-alias detection ambiguous (``INTERVAL 1 DAY``), so items
#: containing INTERVAL trigger a safe fallback instead of a guess.
DATE_PARTS: FrozenSet[str] = frozenset(
    {
        "YEAR",
        "QUARTER",
        "MONTH",
        "WEEK",
        "ISOWEEK",
        "DAY",
        "DAYOFWEEK",
        "DAYOFYEAR",
        "HOUR",
        "MINUTE",
        "SECOND",
        "MILLISECOND",
        "MICROSECOND",
        "ISOYEAR",
        "DATE",
        "TIME",
        "DATETIME",
    }
)
