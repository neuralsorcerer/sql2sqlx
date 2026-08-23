# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""Exception hierarchy for :mod:`sql2sqlx`.

All exceptions raised deliberately by this library derive from
:class:`Sql2SqlxError`, so callers can catch a single base class.
Location-aware errors (lexing/splitting problems) carry a 1-based
``line`` and ``column`` pointing at the offending character in the
original SQL text.
"""

from __future__ import annotations

from typing import Optional


class Sql2SqlxError(Exception):
    """Base class for all errors raised by sql2sqlx."""


class LexError(Sql2SqlxError):
    """Raised when the SQL text cannot be tokenized.

    Typical causes are unterminated string literals, unterminated
    backtick-quoted identifiers, or unterminated ``/* ... */`` block
    comments.

    Attributes:
        message: Human-readable description of the problem.
        line: 1-based line number of the offending character.
        column: 1-based column number of the offending character.
    """

    def __init__(self, message: str, line: int, column: int) -> None:
        """Initialize the error.

        Args:
            message: Human-readable description of the problem.
            line: 1-based line number in the source text.
            column: 1-based column number in the source text.
        """
        super().__init__(f"{message} (line {line}, column {column})")
        self.message = message
        self.line = line
        self.column = column


class SplitError(Sql2SqlxError):
    """Reserved for a statement-splitting failure. **Never raised today.**

    :mod:`sql2sqlx.splitter` has no failure mode: a mismatched ``END`` pops
    the innermost frame, and unbalanced parentheses simply keep the
    following text attached to the current statement. Both degrade into
    "statements stay together", which the classifier then handles safely as
    a verbatim ``operations`` action - a strictly better outcome than
    rejecting a file. Nothing in the package raises this exception, so a
    ``except SplitError`` handler is dead code; it stays exported for
    API compatibility and as the slot a future strict mode would use.
    """

    def __init__(self, message: str, line: int = 0, column: int = 0) -> None:
        """Initialize the error.

        Args:
            message: Human-readable description of the problem.
            line: 1-based line number in the source text (0 if unknown).
            column: 1-based column number in the source text (0 if unknown).
        """
        loc = f" (line {line}, column {column})" if line else ""
        super().__init__(f"{message}{loc}")
        self.message = message
        self.line = line
        self.column = column


class ConversionError(Sql2SqlxError):
    """Raised when a statement or file cannot be converted at all.

    Note that *unsupported-but-valid* SQL never raises: it falls back to
    a Dataform ``operations`` action and a warning is recorded in the
    :class:`~sql2sqlx.model.ConversionReport`. This exception is for
    genuinely broken input (e.g. unreadable files, lexer failures).
    """

    def __init__(self, message: str, path: Optional[str] = None) -> None:
        """Initialize the error.

        Args:
            message: Human-readable description of the problem.
            path: Path of the file being converted, if applicable.
        """
        super().__init__(f"{path}: {message}" if path else message)
        self.message = message
        self.path = path
