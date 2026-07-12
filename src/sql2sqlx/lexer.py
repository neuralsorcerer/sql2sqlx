# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""A precise, linear-time lexer for GoogleSQL (BigQuery Standard SQL).

This module is the correctness foundation of sql2sqlx: every later stage
(statement splitting, classification, reference rewriting) operates on the
token stream produced here, so semicolons, keywords and table names that
appear *inside* strings or comments can never be misinterpreted.

The lexer recognizes, per the GoogleSQL lexical specification:

* line comments (``-- ...`` and ``# ...``);
* block comments (``/* ... */``, **non-nested**, exactly as GoogleSQL
  defines them - the comment ends at the first ``*/``);
* single- and double-quoted string literals with backslash escape
  sequences (``'it\\'s'``);
* triple-quoted string literals (three single or double quotes) which
  may span lines and contain quotes;
* raw and bytes literal prefixes in any valid combination and case
  (``r'...'``, ``B\"...\"``, ``rb'''...'''``, ``bR\"...\"``, ...). In raw
  literals escape sequences are not *interpreted* (the backslash stays in
  the value), but a backslash still consumes the character after it, so
  ``r'a\\'b'`` is one literal and a raw literal can never end in an odd
  number of backslashes - exactly GoogleSQL's rules;
* backtick-quoted identifiers, including escape sequences and embedded
  dots (``project.dataset.table`` wrapped in backticks);
* numeric literals (integers, decimals, exponents);
* named and positional query parameters (``@param``, ``?``) and system
  variables (``@@var``);
* identifiers/keywords and all GoogleSQL operators and punctuation.

Design notes
------------
The scanner is a single compiled alternation executed by CPython's C
regex engine, which gives 20-60 MB/s throughput while remaining exactly
character-accurate. Every alternative in the pattern is written so that
matching is strictly linear (no ambiguous nested quantifiers, hence no
catastrophic backtracking). Anything the master pattern cannot match is
diagnosed by a small fallback that raises :class:`~sql2sqlx.errors.LexError`
with an exact line/column.

Tokens store ``(kind, text, start, end)`` where ``start``/``end`` are
character offsets into the original text; downstream stages rewrite SQL
by *span edits* on the original string, guaranteeing that untouched SQL
is preserved character-for-character after decoding.
"""

from __future__ import annotations

import bisect
import re
from typing import List, Optional, Tuple

from sql2sqlx.errors import LexError

# ---------------------------------------------------------------------------
# Token kinds (module-level string constants; cheap identity comparisons).
# ---------------------------------------------------------------------------

IDENT = "IDENT"  #: unquoted identifier or keyword
BACKTICK = "BACKTICK"  #: backtick-quoted identifier, text includes backticks
STRING = "STRING"  #: any string/bytes literal, text includes quotes/prefix
NUMBER = "NUMBER"  #: numeric literal
PARAM = "PARAM"  #: @named, @@system or ? positional parameter
OP = "OP"  #: operator or punctuation (one token per symbol)
COMMENT = "COMMENT"  #: line or block comment (excluded from significant stream)
EOF = "EOF"  #: synthetic end-of-input marker


class Token:
    """A single lexical token.

    Attributes:
        kind: One of :data:`IDENT`, :data:`BACKTICK`, :data:`STRING`,
            :data:`NUMBER`, :data:`PARAM`, :data:`OP`, :data:`COMMENT`,
            :data:`EOF`.
        text: The exact source text of the token (including quotes,
            prefixes and backticks where applicable).
        start: Character offset of the first character in the source.
        end: Character offset one past the last character in the source.
    """

    __slots__ = ("kind", "text", "start", "end")

    def __init__(self, kind: str, text: str, start: int, end: int) -> None:
        """Initialize a token; see class attribute docs for parameters."""
        self.kind = kind
        self.text = text
        self.start = start
        self.end = end

    @property
    def upper(self) -> str:
        """Uppercased token text, used for case-insensitive keyword tests."""
        return self.text.upper()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Token({self.kind}, {self.text!r}, {self.start}:{self.end})"


# ---------------------------------------------------------------------------
# Master pattern.
#
# Order matters:
#   * comments before operators (so `--` is not two minus tokens);
#   * string literals (with optional r/b prefixes) before IDENT (so the
#     prefix letter is consumed as part of the literal);
#   * triple-quoted before single-quoted (longest match);
#   * NUMBER before OP (so `.5` is one number, and `1.5e-3` is one token).
#
# Escape pairs are recognized broadly by the master scanner, then validated
# against GoogleSQL's exact escape table. This produces precise errors for
# forbidden backslash-newline pairs and incomplete numeric escapes. Raw
# literal bodies also consume backslash+character pairs - the escape is
# never *interpreted* (the backslash stays in the value), but per GoogleSQL
# it still cannot terminate the literal, so r'a\'b' is one literal and
# r'a\' is unterminated.
# ---------------------------------------------------------------------------

_RAW_PREFIX = r"(?:[rR][bB]?|[bB][rR])"  # any prefix combination containing r/R
_BYTES_ONLY = r"[bB]?"  # optional b/B (escapes still active)

_TOKEN_RE = re.compile(
    r"""
      (?P<WS>\s+)
    | (?P<COMMENT>--[^\r\n]* | \#[^\r\n]* | /\*(?:[^*]|\*(?!/))*\*/)
    | (?P<STRING>
          # raw (and raw-bytes) triple-quoted: escapes stay uninterpreted
          # but still consume the next character
          {raw}(?:'''(?:[^'\\]|\\[\s\S]|'(?!''))*''' | \"\"\"(?:[^"\\]|\\[\s\S]|"(?!""))*\"\"\")
          # raw (and raw-bytes) single-line quoted
        | {raw}(?:'(?:[^'\\\r\n]|\\[^\r\n])*' | "(?:[^"\\\r\n]|\\[^\r\n])*")
          # (bytes-)triple-quoted with escapes
        | {b}(?:'''(?:[^'\\]|\\[\s\S]|'(?!''))*''' | \"\"\"(?:[^"\\]|\\[\s\S]|"(?!""))*\"\"\")
          # (bytes-)single-line quoted with escapes
        | {b}(?:'(?:[^'\\\r\n]|\\[\s\S])*' | "(?:[^"\\\r\n]|\\[\s\S])*")
      )
    | (?P<BACKTICK>`(?:[^`\\\r\n]|\\[\s\S])*`)
    | (?P<IDENT>[A-Za-z_][A-Za-z_0-9]*)
    | (?P<NUMBER>0[xX][0-9A-Fa-f]+ |
                 \d+\.(?:\d+(?:[eE][+-]?\d+)? | [eE][+-]?\d+ |
                         (?![A-Za-z_])) |
                 \.\d+(?:[eE][+-]?\d+)? |
                 \d+(?:[eE][+-]?\d+)?)
    | (?P<PARAM>@@[A-Za-z_][A-Za-z_0-9.]* |
                @`(?:[^`\\\r\n]|\\[\s\S])*` |
                @[A-Za-z_][A-Za-z_0-9]* | \?)
    | (?P<OP>\|> | <=> | <> | <= | >= | != | \|\| | << | >> | => | ->>? |
             [+\-*/%,;()\[\]{{}}<>=.:|&^~@])
    """.format(raw=_RAW_PREFIX, b=_BYTES_ONLY),
    re.VERBOSE,
)

# Group index lookup computed once: groupindex maps name -> group number.
_GROUP_KIND: List[str] = [""] * (_TOKEN_RE.groups + 1)
for _name, _num in _TOKEN_RE.groupindex.items():
    _GROUP_KIND[_num] = _name


_NEWLINE_RE = re.compile(r"\r\n?|\n")


def _line_col(text: str, pos: int) -> Tuple[int, int]:
    """Compute the 1-based ``(line, column)`` of a character offset.

    Args:
        text: The full source text.
        pos: Character offset into ``text``.

    Returns:
        Tuple of 1-based line and column numbers.
    """
    line = 1
    line_start = 0
    for match in _NEWLINE_RE.finditer(text, 0, pos):
        line += 1
        line_start = match.end()
    return line, pos - line_start + 1


_LITERAL_HEAD_RE = re.compile(r"([rRbB]{0,2})('''|\"\"\"|'|\")")
_SIMPLE_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "?": "?",
    '"': '"',
    "'": "'",
    "`": "`",
}


def _escape_value(body: str, i: int, bytes_literal: bool = False) -> Tuple[str, int]:
    """Decode one GoogleSQL escape beginning at ``body[i]``.

    Returns the decoded character and the first index after the escape.
    ``ValueError`` means the escape is lexically invalid.
    """
    if i + 1 >= len(body) or body[i] != "\\":
        raise ValueError("trailing backslash")
    nxt = body[i + 1]
    if nxt in "\r\n":
        raise ValueError("a backslash immediately before a newline is not allowed")
    if nxt in _SIMPLE_ESCAPES:
        return _SIMPLE_ESCAPES[nxt], i + 2
    if nxt in "01234567":
        digits = body[i + 1 : i + 4]
        if len(digits) != 3 or any(ch not in "01234567" for ch in digits):
            raise ValueError("octal escapes require exactly three digits")
        value = int(digits, 8)
        if bytes_literal and value > 0xFF:
            raise ValueError("a bytes escape must be in the range 0x00-0xFF")
        return chr(value), i + 4
    if nxt in "xX":
        digits = body[i + 2 : i + 4]
        if len(digits) != 2 or any(ch not in "0123456789abcdefABCDEF" for ch in digits):
            raise ValueError("hex escapes require exactly two digits")
        return chr(int(digits, 16)), i + 4
    if nxt in "uU":
        if bytes_literal:
            raise ValueError("Unicode escapes are not allowed in bytes literals")
        width = 4 if nxt == "u" else 8
        digits = body[i + 2 : i + 2 + width]
        if len(digits) != width or any(ch not in "0123456789abcdefABCDEF" for ch in digits):
            raise ValueError(f"Unicode escapes require exactly {width} hex digits")
        value = int(digits, 16)
        if 0xD800 <= value <= 0xDFFF or value > 0x10FFFF:
            raise ValueError("Unicode escape is not a valid scalar value")
        return chr(value), i + 2 + width
    raise ValueError(f"invalid escape sequence \\{nxt}")


def _validate_escaped_body(
    body: str, source: str, body_start: int, bytes_literal: bool = False
) -> None:
    """Validate every escape in a non-raw literal or quoted identifier."""
    i = 0
    while i < len(body):
        if body[i] != "\\":
            i += 1
            continue
        try:
            _, i = _escape_value(body, i, bytes_literal)
        except ValueError as exc:
            line, col = _line_col(source, body_start + i)
            raise LexError(str(exc), line, col) from None


def _validate_string_literal(literal: str, source: str, start: int) -> None:
    """Enforce GoogleSQL's literal-prefix and backslash restrictions."""
    m = _LITERAL_HEAD_RE.match(literal)
    if m is None:  # The master pattern guarantees this shape.
        return
    prefix = m.group(1).lower()
    quote = m.group(2)
    body = literal[m.end() : -len(quote)]
    body_start = start + m.end()
    if "r" in prefix:
        trailing = len(body) - len(body.rstrip("\\"))
        if trailing % 2:
            line, col = _line_col(source, body_start + len(body) - trailing)
            raise LexError(
                "Raw string cannot end with an odd number of backslashes",
                line,
                col,
            )
        return
    _validate_escaped_body(body, source, body_start, bytes_literal="b" in prefix)


def _validate_backtick_identifier(token_text: str, source: str, start: int) -> None:
    """Validate escape sequences in a backtick-quoted identifier."""
    body = token_text[1:-1]
    if not body:
        line, col = _line_col(source, start)
        raise LexError("Quoted identifier cannot be empty", line, col)
    _validate_escaped_body(body, source, start + 1)


def _diagnose(text: str, pos: int) -> "LexError":
    """Build a precise :class:`LexError` for an unmatchable position.

    The master pattern fails only for a handful of well-defined reasons;
    this helper distinguishes them so users get an actionable message.

    Args:
        text: The full source text.
        pos: Offset at which the master pattern failed to match.

    Returns:
        A ready-to-raise :class:`LexError`.
    """
    line, col = _line_col(text, pos)
    ch = text[pos]
    rest = text[pos : pos + 2]
    if rest == "/*":
        return LexError("Unterminated block comment", line, col)
    if ch == "`":
        return LexError("Unterminated backtick-quoted identifier", line, col)
    if ch in "'\"" or (ch in "rRbB" and re.match(r"[rRbB]{1,2}['\"]", text[pos : pos + 3] or "")):
        return LexError("Unterminated string literal", line, col)
    return LexError(f"Unexpected character {ch!r}", line, col)


def tokenize(
    text: str,
    keep_comments: bool = False,
    comment_spans_out: Optional[List[Tuple[int, int]]] = None,
) -> List[Token]:
    """Tokenize GoogleSQL text into a list of :class:`Token` objects.

    Whitespace is always dropped. Comments are dropped unless
    ``keep_comments`` is true (statement splitting only needs significant
    tokens); their spans can be captured in the same pass via
    ``comment_spans_out``, which the conversion pipeline uses so files
    are lexed exactly once.

    Args:
        text: SQL source text.
        keep_comments: If ``True``, ``COMMENT`` tokens are included in the
            returned stream (in source order).
        comment_spans_out: Optional list that receives every comment's
            ``(start, end)`` span, regardless of ``keep_comments``.

    Returns:
        List of significant tokens in source order, terminated by a
        synthetic :data:`EOF` token whose span is ``(len(text), len(text))``.

    Raises:
        LexError: If the text contains an unterminated string, unterminated
            backtick identifier, unterminated block comment, or a character
            that is not valid in GoogleSQL.

    Example:
        >>> [t.text for t in tokenize("SELECT 'a;b' -- c\\n;")][:-1]
        ["SELECT", "'a;b'", ';']
    """
    tokens: List[Token] = []
    append = tokens.append
    match = _TOKEN_RE.match
    pos = 0
    n = len(text)
    kinds = _GROUP_KIND
    while pos < n:
        m = match(text, pos)
        if m is None:
            raise _diagnose(text, pos)
        # A lone "/" where "/*" begins means the COMMENT alternative could
        # not match, i.e. the block comment is unterminated.
        if m.group() == "/" and text.startswith("/*", pos):
            raise _diagnose(text, pos)
        end = m.end()
        kind = kinds[m.lastindex]  # type: ignore[index]
        if kind == "WS":
            pos = end
            continue
        if kind == "COMMENT":
            if comment_spans_out is not None:
                comment_spans_out.append((pos, end))
            if not keep_comments:
                pos = end
                continue
        elif kind == "STRING":
            _validate_string_literal(m.group(), text, pos)
        elif kind == "BACKTICK":
            _validate_backtick_identifier(m.group(), text, pos)
        elif kind == "PARAM" and m.group().startswith("@`"):
            _validate_backtick_identifier(m.group()[1:], text, pos + 1)
        append(Token(kind, m.group(), pos, end))
        pos = end
    append(Token(EOF, "", n, n))
    return tokens


def comment_spans(text: str) -> List[Tuple[int, int]]:
    """Return the ``(start, end)`` spans of every comment in ``text``.

    Used by the emitter to carry a statement's *leading* comments into the
    generated ``.sqlx`` file. Runs the same master pattern, so a ``--``
    inside a string literal is never mistaken for a comment.

    Args:
        text: SQL source text (must be lexable).

    Returns:
        List of half-open character spans, in source order.

    Raises:
        LexError: Propagated from :func:`tokenize` failure conditions.
    """
    spans: List[Tuple[int, int]] = []
    match = _TOKEN_RE.match
    pos = 0
    n = len(text)
    kinds = _GROUP_KIND
    while pos < n:
        m = match(text, pos)
        if m is None:
            raise _diagnose(text, pos)
        if m.group() == "/" and text.startswith("/*", pos):
            raise _diagnose(text, pos)
        kind = kinds[m.lastindex]  # type: ignore[index]
        if kind == "STRING":
            _validate_string_literal(m.group(), text, pos)
        elif kind == "BACKTICK":
            _validate_backtick_identifier(m.group(), text, pos)
        elif kind == "PARAM" and m.group().startswith("@`"):
            _validate_backtick_identifier(m.group()[1:], text, pos + 1)
        if kind == "COMMENT":
            spans.append((pos, m.end()))
        pos = m.end()
    return spans


class LineIndex:
    """Fast character-offset to ``(line, column)`` mapping for one text.

    Builds the newline offset table once (O(n)) and answers each query in
    O(log n) via binary search. Used to attach accurate source locations
    to report warnings without rescanning the file per warning.
    """

    __slots__ = ("_starts",)

    def __init__(self, text: str) -> None:
        """Index ``text``.

        Args:
            text: The source text to index.
        """
        self._starts = [0] + [match.end() for match in _NEWLINE_RE.finditer(text)]

    def locate(self, pos: int) -> Tuple[int, int]:
        """Return the 1-based ``(line, column)`` for character offset ``pos``.

        Args:
            pos: Character offset into the indexed text.

        Returns:
            Tuple of 1-based line and column numbers.
        """
        i = bisect.bisect_right(self._starts, pos) - 1
        return i + 1, pos - self._starts[i] + 1


def unquote_identifier(text: str) -> str:
    """Return the logical name of an identifier token's text.

    For backtick-quoted identifiers the surrounding backticks are removed
    and GoogleSQL escape sequences are decoded (``\\``` -> `` ` ``,
    ``\\\\`` -> ``\\``, plus the standard C-style escapes). Unquoted
    identifiers are returned unchanged.

    Args:
        text: Raw token text, e.g. ``"`my table`"`` or ``"orders"``.

    Returns:
        The decoded identifier, e.g. ``"my table"`` or ``"orders"``.
    """
    if not (len(text) >= 2 and text[0] == "`" and text[-1] == "`"):
        return text
    body = text[1:-1]
    if "\\" not in body:
        return body
    out: List[str] = []
    i = 0
    while i < len(body):
        if body[i] != "\\":
            out.append(body[i])
            i += 1
            continue
        try:
            decoded, i = _escape_value(body, i)
            out.append(decoded)
        except ValueError:
            # ``unquote_identifier`` is also a public convenience helper;
            # keep it total for arbitrary caller-provided text.  Tokens
            # produced by ``tokenize`` have already been strictly validated.
            if i + 1 < len(body):
                out.append(body[i + 1])
                i += 2
            else:
                out.append("\\")
                i += 1
    return "".join(out)
