# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""Statement classification and Dataform metadata extraction.

This module decides, for every top-level statement, which Dataform action
type it becomes and extracts everything needed for the ``config { ... }``
block. The governing principle is:

    **Convert when provably safe; otherwise fall back to a verbatim
    ``operations`` action and record a warning.**

An ``operations`` action executes the original SQL unchanged, so the
fallback path can never alter behavior - it only forgoes idiomatic
Dataform structure. Every fallback carries a stable warning code and a
human-readable reason in the conversion report.

Mapping summary
---------------
====================================  =======================================
Statement                             Result
====================================  =======================================
``CREATE [OR REPLACE] TABLE .. AS``   ``type: "table"`` (+ partition/cluster/
                                      OPTIONS mapping)
``CREATE [MATERIALIZED] VIEW .. AS``  ``type: "view"`` (+ ``materialized``)
``INSERT INTO .. SELECT``             ``type: "incremental"`` (configurable)
``MERGE``                             ``operations`` by default; provably
                                      equivalent MERGEs can opt into
                                      ``incremental`` + ``uniqueKey``
``CREATE TABLE`` (no ``AS``)          ``operations`` (or ``declaration``)
UPDATE/DELETE/TRUNCATE/DROP/ALTER/    ``operations`` with write-target
LOAD                                  tracking for dependency chaining
``CREATE/DROP/ALTER SEARCH|VECTOR``   ``operations`` tracked as a writer of
``INDEX``, ``ROW ACCESS POLICY``      the ``ON`` table (ordered after it,
DDL, table-scoped ``GRANT``/          never elected as its owner)
``REVOKE``
everything else                       ``operations``
====================================  =======================================

Select-list aliasing
--------------------
``INSERT INTO t (a, b) SELECT x, y ...`` and ``CREATE VIEW v (a, b) AS
SELECT ...`` change output column names. To convert these faithfully the
select list is rewritten item-by-item (``x AS a, y AS b``) - but **only**
when every item's existing output name/alias can be determined exactly at
the token level. Constructs that make that ambiguous (``*`` expansion,
``INTERVAL 1 DAY`` implicit-alias traps, naked ``STRUCT<...>``/
``ARRAY<...>`` constructors, ``SELECT AS STRUCT``) trigger the operations
fallback instead of a guess.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from sql2sqlx.keywords import RESERVED
from sql2sqlx.lexer import (
    BACKTICK,
    EOF,
    IDENT,
    NUMBER,
    OP,
    PARAM,
    STRING,
    Token,
    unquote_identifier,
)
from sql2sqlx.model import (
    ActionDraft,
    ActionType,
    ConversionOptions,
    IfNotExistsStrategy,
    InsertStrategy,
    MergeStrategy,
    PlainCreateStrategy,
    RefSite,
    TableName,
)
from sql2sqlx.refs import parse_table_path, scan_ref_sites
from sql2sqlx.splitter import RawStatement

#: Sentinel: an OPTIONS value kept as raw SQL text (-> ``additionalOptions``).
RAW = object()

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")


def _dataform_merge_column(name: str) -> bool:
    """Whether Dataform can emit ``name`` in an incremental MERGE.

    Dataform Core 3.x quotes target/INSERT columns, but emits unique keys and
    source-side UPDATE references without quoting.  Those names must therefore
    be simple, unreserved GoogleSQL identifiers.
    """
    return bool(_IDENT_RE.fullmatch(name)) and name.upper() not in RESERVED


def _dataform_insert_column(name: str) -> bool:
    """Whether Dataform can safely backtick-quote an incremental column.

    The BigQuery adapter wraps existing-table metadata names in raw backticks
    without escaping identifier terminators or backslashes.  Control and line
    separator characters are rejected as well so the generated runtime SQL
    cannot be split or reinterpreted.
    """
    return bool(name) and all(
        character not in ("`", "\\")
        and ord(character) >= 0x20
        and not (0x7F <= ord(character) <= 0x9F)
        and ord(character) not in (0x2028, 0x2029)
        for character in name
    )


#: Reserved keywords that terminate an expression and may precede an
#: implicit alias (``SELECT x IS NULL flag`` -> alias ``flag``).
_EXPRESSION_END_KEYWORDS = frozenset({"TRUE", "FALSE", "NULL", "END"})

#: Keywords ending a select list at nesting depth 0.
_SELECT_LIST_ENDERS = frozenset(
    {
        "FROM",
        "UNION",
        "INTERSECT",
        "EXCEPT",
        "LIMIT",
        "ORDER",
        "WHERE",
        "GROUP",
        "HAVING",
        "QUALIFY",
        "WINDOW",
    }
)


# ---------------------------------------------------------------------------
# Small token utilities
# ---------------------------------------------------------------------------


def _with_eof(stmt: RawStatement) -> List[Token]:
    """Return the statement's tokens with a guaranteed trailing EOF token.

    Args:
        stmt: The raw statement.

    Returns:
        The token list, EOF-terminated (a synthetic EOF is appended when
        the splitter's slice did not include one).
    """
    toks = stmt.tokens
    if toks and toks[-1].kind == EOF:
        return list(toks)
    return list(toks) + [Token(EOF, "", stmt.end, stmt.end)]


def _kw(toks: Sequence[Token], i: int) -> str:
    """Uppercase text of ``toks[i]`` if it is an identifier, else ``""``.

    Args:
        toks: Token list.
        i: Index (out-of-range indices are safe).

    Returns:
        The uppercased identifier text, or the empty string.
    """
    if 0 <= i < len(toks) and toks[i].kind == IDENT:
        return toks[i].upper
    return ""


def _is_op(toks: Sequence[Token], i: int, text: str) -> bool:
    """True when ``toks[i]`` is the operator/punctuation ``text``."""
    return 0 <= i < len(toks) and toks[i].kind == OP and toks[i].text == text


def _skip_balanced(toks: Sequence[Token], i: int) -> int:
    """Skip a balanced parenthesis group starting at ``toks[i] == '('``.

    Args:
        toks: Token list (EOF-terminated).
        i: Index of the opening parenthesis.

    Returns:
        Index of the first token after the matching ``)``. If the group is
        unbalanced (invalid SQL), the EOF index is returned so callers
        terminate gracefully.
    """
    if not _is_op(toks, i, "("):
        return i + 1
    depth = 0
    n = len(toks)
    while i < n:
        t = toks[i]
        if t.kind == EOF:
            break
        if t.kind == OP:
            if t.text == "(":
                depth += 1
            elif t.text == ")":
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    return n - 1


def _body_head_ok(toks: Sequence[Token], i: int) -> bool:
    """True when ``toks[i]`` can start a query body.

    Accepts ``SELECT``, ``WITH``, a parenthesized query, and ``FROM``
    (BigQuery pipe-syntax queries start with ``FROM``).
    """
    if i >= len(toks):
        return False
    t = toks[i]
    if t.kind == OP and t.text == "(":
        return True
    return t.kind == IDENT and t.upper in ("SELECT", "WITH", "FROM")


def _quote_ident(name: str) -> str:
    """Render ``name`` as a safe SQL identifier (backticked if needed).

    Args:
        name: The identifier text (already decoded).

    Returns:
        ``name`` unchanged when it is a plain unreserved identifier,
        otherwise a backtick-quoted, escaped form.
    """
    if _IDENT_RE.match(name) and name.upper() not in RESERVED:
        return name
    control_escapes = {
        "\a": "\\a",
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
        "\v": "\\v",
    }
    escaped = "".join(
        (
            "\\\\"
            if char == "\\"
            else (
                "\\`"
                if char == "`"
                else control_escapes.get(
                    char,
                    (
                        f"\\x{ord(char):02X}"
                        if ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F
                        else f"\\u{ord(char):04X}" if ord(char) in (0x2028, 0x2029) else char
                    ),
                )
            )
        )
        for char in name
    )
    quoted = "`" + escaped + "`"
    # A SQLX placeholder cannot sit between BigQuery backticks. If the
    # generated identifier contains `${`, emit the entire token as a constant
    # JavaScript expression so compilation reconstructs it exactly.
    if "${" in quoted:
        return "${" + json.dumps(quoted, ensure_ascii=False) + "}"
    return quoted


# ---------------------------------------------------------------------------
# String / OPTIONS parsing
# ---------------------------------------------------------------------------

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

_STRING_HEAD_RE = re.compile(r"([rRbB]{0,2})('''|\"\"\"|'|\")")


def _decode_string(literal: str) -> Optional[str]:
    """Decode a GoogleSQL string literal token to a Python string.

    Args:
        literal: The exact token text, including quotes and any prefix.

    Returns:
        The decoded text, or ``None`` for bytes literals and malformed
        input (callers then treat the value as raw SQL).
    """
    m = _STRING_HEAD_RE.match(literal)
    if not m:
        return None
    prefix = m.group(1).lower()
    if "b" in prefix:
        return None
    quote = m.group(2)
    body = literal[m.end() : -len(quote)]
    if "r" in prefix or "\\" not in body:
        return body
    out: List[str] = []
    i, size = 0, len(body)
    while i < size:
        c = body[i]
        if c != "\\" or i + 1 >= size:
            out.append(c)
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[nxt])
            i += 2
        elif nxt in ("x", "X"):
            try:
                out.append(chr(int(body[i + 2 : i + 4], 16)))
                i += 4
            except ValueError:
                out.append(nxt)
                i += 2
        elif nxt == "u":
            try:
                out.append(chr(int(body[i + 2 : i + 6], 16)))
                i += 6
            except ValueError:
                out.append(nxt)
                i += 2
        elif nxt == "U":
            try:
                out.append(chr(int(body[i + 2 : i + 10], 16)))
                i += 10
            except ValueError:
                out.append(nxt)
                i += 2
        elif nxt in "01234567":
            j = i + 1
            while j < min(i + 4, size) and body[j] in "01234567":
                j += 1
            out.append(chr(int(body[i + 1 : j], 8)))
            i = j
        else:
            out.append(nxt)
            i += 2
    return "".join(out)


def _num(s: str) -> Any:
    """Parse a NUMBER token's text into ``int``/``float`` (RAW on failure)."""
    try:
        if re.fullmatch(r"0[xX][0-9A-Fa-f]+", s):
            return int(s, 16)
        if re.fullmatch(r"\d+", s):
            return int(s)
        value = float(s)
        return value if math.isfinite(value) else RAW
    except ValueError:  # pragma: no cover - lexer guarantees numeric shape
        return RAW


def _parse_label_array(vtoks: Sequence[Token]) -> Optional[Dict[str, str]]:
    """Strictly parse a labels value ``[("k", "v"), ...]`` into a dict.

    Args:
        vtoks: The value's tokens.

    Returns:
        The label mapping, or ``None`` if the value has any other shape
        (the caller then keeps the raw SQL in ``additionalOptions``).
    """
    if len(vtoks) < 2:
        return None
    if not (vtoks[0].kind == OP and vtoks[0].text == "["):
        return None
    if not (vtoks[-1].kind == OP and vtoks[-1].text == "]"):
        return None
    out: Dict[str, str] = {}
    j, end = 1, len(vtoks) - 1
    while j < end:
        if j + 5 > end or not (vtoks[j].kind == OP and vtoks[j].text == "("):
            return None
        k, comma, v, close = vtoks[j + 1], vtoks[j + 2], vtoks[j + 3], vtoks[j + 4]
        if k.kind != STRING or v.kind != STRING:
            return None
        if not (comma.kind == OP and comma.text == ","):
            return None
        if not (close.kind == OP and close.text == ")"):
            return None
        ks, vs = _decode_string(k.text), _decode_string(v.text)
        if ks is None or vs is None:
            return None
        out[ks] = vs
        j += 5
        if j < end:
            if not (vtoks[j].kind == OP and vtoks[j].text == ","):
                return None
            j += 1
    return out


def _parse_option_value(vtoks: Sequence[Token]) -> Any:
    """Interpret an OPTIONS value's tokens as a simple literal if possible.

    Args:
        vtoks: The value's tokens (never empty).

    Returns:
        ``str``/``int``/``float``/``bool``/``dict`` for recognized literal
        shapes, otherwise the :data:`RAW` sentinel.
    """
    if len(vtoks) == 1:
        t = vtoks[0]
        if t.kind == STRING:
            s = _decode_string(t.text)
            return s if s is not None else RAW
        if t.kind == NUMBER:
            return _num(t.text)
        if t.kind == IDENT:
            if t.upper == "TRUE":
                return True
            if t.upper == "FALSE":
                return False
        return RAW
    if len(vtoks) == 2 and vtoks[0].kind == OP and vtoks[0].text == "-" and vtoks[1].kind == NUMBER:
        v = _num(vtoks[1].text)
        return -v if isinstance(v, (int, float)) else RAW
    labels = _parse_label_array(vtoks)
    return labels if labels is not None else RAW


def _parse_options(
    toks: Sequence[Token], i: int, text: str
) -> Optional[Tuple[List[Tuple[str, str, Any]], int]]:
    """Parse an ``OPTIONS(key = value, ...)`` clause.

    Args:
        toks: Token list; ``toks[i]`` must be the ``OPTIONS`` identifier.
        i: Index of ``OPTIONS``.
        text: Full source text (for raw value capture).

    Returns:
        ``(entries, next_index)`` where each entry is
        ``(key, raw_sql_text, parsed_value_or_RAW)``, or ``None`` when the
        clause cannot be parsed (caller falls back to operations).
    """
    j = i + 1
    if not _is_op(toks, j, "("):
        return None
    j += 1
    entries: List[Tuple[str, str, Any]] = []
    n = len(toks)
    while j < n:
        if _is_op(toks, j, ")"):
            return entries, j + 1
        kt = toks[j]
        if kt.kind not in (IDENT, BACKTICK):
            return None
        key = unquote_identifier(kt.text)
        j += 1
        if not _is_op(toks, j, "="):
            return None
        j += 1
        vstart = j
        depth = 0
        while j < n and toks[j].kind != EOF:
            t = toks[j]
            if t.kind == OP:
                if t.text in ("(", "["):
                    depth += 1
                elif t.text in (")", "]"):
                    if depth == 0 and t.text == ")":
                        break
                    depth -= 1
                elif t.text == "," and depth == 0:
                    break
            j += 1
        if j >= n or toks[j].kind == EOF or j == vstart:
            return None
        vtoks = toks[vstart:j]
        raw = text[vtoks[0].start : vtoks[-1].end]
        entries.append((key, raw, _parse_option_value(vtoks)))
        if _is_op(toks, j, ","):
            j += 1
    return None


# ---------------------------------------------------------------------------
# Select-list machinery (shared by INSERT column lists, VIEW column lists
# and the safe-MERGE analyzer)
# ---------------------------------------------------------------------------


class _SelectItem:
    """One top-level select-list item and its safety flags.

    Attributes:
        toks: The item's tokens.
        interval: True when a top-level ``INTERVAL`` keyword makes
            implicit-alias detection ambiguous.
        generic: True when a naked ``STRUCT<``/``ARRAY<`` constructor makes
            comma splitting ambiguous.
        star: True when the item is a ``*`` / ``t.*`` expansion.
    """

    __slots__ = ("toks", "interval", "generic", "star")

    def __init__(self) -> None:
        """Create an empty item."""
        self.toks: List[Token] = []
        self.interval = False
        self.generic = False
        self.star = False


def _split_select_items(toks: Sequence[Token], ts: int, te: int) -> Optional[List[_SelectItem]]:
    """Split the select list of the query spanning ``toks[ts:te)``.

    Handles wrapper parentheses around the whole query, a leading ``WITH``
    clause (CTEs are skipped, not inspected), and ``DISTINCT``/``ALL``
    modifiers. BigQuery's trailing comma before ``FROM`` is accepted.

    Args:
        toks: Token list (EOF-terminated).
        ts: Index of the query's first token.
        te: Exclusive end index of the query (usually the EOF index).

    Returns:
        The list of items, or ``None`` for shapes that cannot be handled
        (missing ``SELECT``, ``SELECT AS STRUCT/VALUE``,
        ``SELECT WITH DIFFERENTIAL_PRIVACY``, malformed ``WITH``...).
    """
    n = len(toks)
    te = min(te, n)
    while (
        ts < te
        and _is_op(toks, ts, "(")
        and _skip_balanced(toks, ts) == te
        and _is_op(toks, te - 1, ")")
    ):
        ts += 1
        te -= 1
    j = ts
    if _kw(toks, j) == "WITH":
        j += 1
        if _kw(toks, j) == "RECURSIVE":
            j += 1
        while True:
            if j >= te or toks[j].kind not in (IDENT, BACKTICK):
                return None
            if toks[j].kind == IDENT and toks[j].upper in RESERVED:
                return None
            j += 1
            if _is_op(toks, j, "("):
                j = _skip_balanced(toks, j)
            if _kw(toks, j) != "AS":
                return None
            j += 1
            if not _is_op(toks, j, "("):
                return None
            j = _skip_balanced(toks, j)
            if _is_op(toks, j, ","):
                j += 1
                continue
            break
    if _kw(toks, j) != "SELECT":
        return None
    j += 1
    if _kw(toks, j) in ("DISTINCT", "ALL"):
        j += 1
    if _kw(toks, j) in ("AS", "WITH"):
        return None
    items: List[_SelectItem] = []
    cur = _SelectItem()
    depth = 0
    k = j
    while k < te:
        t = toks[k]
        if t.kind == EOF:
            break
        if t.kind == OP:
            x = t.text
            if x in ("(", "["):
                depth += 1
            elif x in (")", "]"):
                depth -= 1
                if depth < 0:
                    break
            elif depth == 0:
                if x == ",":
                    if not cur.toks:
                        return None
                    items.append(cur)
                    cur = _SelectItem()
                    k += 1
                    continue
                if x == "|>":
                    break
                if x == "*":
                    prev = cur.toks[-1] if cur.toks else None
                    if prev is None or (prev.kind == OP and prev.text == "."):
                        cur.star = True
        elif t.kind == IDENT and depth == 0:
            u = t.upper
            if u in _SELECT_LIST_ENDERS:
                break
            if u == "INTERVAL":
                cur.interval = True
            elif u in ("STRUCT", "ARRAY") and _is_op(toks, k + 1, "<"):
                cur.generic = True
        cur.toks.append(t)
        k += 1
    if cur.toks:
        items.append(cur)
    if not items:
        return None
    return items


def _item_output_info(item: _SelectItem) -> Optional[Tuple[str, Optional[str], Optional[Token]]]:
    """Determine a select-list item's output name and alias structure.

    Args:
        item: The item to analyze.

    Returns:
        ``(mode, name, alias_token)`` where mode is one of:

        * ``"explicit"`` - ``expr AS alias`` (name = alias);
        * ``"implicit"`` - ``expr alias`` (name = alias);
        * ``"bare"`` - a plain column/path (name = last segment);
        * ``"anon"`` - an expression with no derivable output name.

        Returns ``None`` when the item is *unsafe* to reason about
        (star expansion, ambiguous ``INTERVAL``/generic constructors).
    """
    if item.star:
        return None
    t = item.toks
    last = t[-1]
    if len(t) == 1:
        if last.kind == BACKTICK:
            return ("bare", unquote_identifier(last.text), None)
        if last.kind == IDENT and last.upper not in RESERVED:
            return ("bare", last.text, None)
        return ("anon", None, None)
    penult = t[-2]
    last_is_name = last.kind == BACKTICK or (last.kind == IDENT and last.upper not in RESERVED)
    if penult.kind == IDENT and penult.upper == "AS" and last_is_name:
        return ("explicit", unquote_identifier(last.text), last)
    if last_is_name:
        if penult.kind == OP and penult.text == ".":
            return ("bare", unquote_identifier(last.text), None)
        if item.interval or item.generic:
            return None
        terminal = (
            penult.kind in (BACKTICK, NUMBER, STRING, PARAM)
            or (penult.kind == OP and penult.text in (")", "]"))
            or (
                penult.kind == IDENT
                and (penult.upper not in RESERVED or penult.upper in _EXPRESSION_END_KEYWORDS)
            )
        )
        if terminal:
            return ("implicit", unquote_identifier(last.text), last)
    return ("anon", None, None)


def _alias_select_list_edits(
    toks: Sequence[Token], ts: int, te: int, cols: Sequence[str]
) -> Optional[List[Tuple[int, int, str]]]:
    """Compute span edits forcing the select list to output ``cols``.

    Args:
        toks: Token list (EOF-terminated).
        ts: Index of the query's first token.
        te: Exclusive end index of the query.
        cols: Required output column names, in order.

    Returns:
        Absolute-offset span edits, or ``None`` when the rewrite is not
        provably safe (arity mismatch, star expansion, ambiguous items).
    """
    items = _split_select_items(toks, ts, te)
    if items is None or len(items) != len(cols):
        return None
    infos = []
    for it in items:
        info = _item_output_info(it)
        if info is None:
            return None
        infos.append(info)
    changed_aliases = set()
    for (mode, name, _alias), col in zip(infos, cols):
        if name is not None and name.upper() == col.upper():
            continue  # no edit for this item
        if mode in ("explicit", "implicit") and name is not None:
            # The old alias disappears; later references to it would
            # re-resolve (or break).
            changed_aliases.add(name.upper())
        # The new alias appears; BigQuery resolves SELECT aliases in
        # preference to FROM columns inside GROUP BY/HAVING/QUALIFY/
        # ORDER BY, so a same-named column reference there would silently
        # start meaning this item's expression instead.
        changed_aliases.add(col.upper())
    if changed_aliases:
        # SELECT aliases are visible to GROUP BY, HAVING, QUALIFY, ORDER BY
        # and subsequent pipe operators. Adding, removing or replacing an
        # alias without rewriting such references would either change which
        # expression is used or make the query invalid. Keep the original
        # statement as operations instead of guessing.
        list_end = max(item.toks[-1].end for item in items)
        depth = 0
        alias_visible = False
        for token in toks:
            if token.start < list_end or token.kind == EOF or token.start >= toks[te].start:
                continue
            if token.kind == OP:
                if token.text in ("(", "["):
                    depth += 1
                elif token.text in (")", "]"):
                    depth = max(0, depth - 1)
                elif token.text == "|>" and depth == 0:
                    alias_visible = True
                continue
            if token.kind not in (IDENT, BACKTICK):
                continue
            word = token.upper if token.kind == IDENT else unquote_identifier(token.text).upper()
            if (
                depth == 0
                and token.kind == IDENT
                and word in ("GROUP", "HAVING", "QUALIFY", "ORDER")
            ):
                alias_visible = True
                continue
            if (
                depth == 0
                and token.kind == IDENT
                and word in ("LIMIT", "UNION", "INTERSECT", "EXCEPT")
            ):
                alias_visible = False
                continue
            if alias_visible and word in changed_aliases:
                return None
    edits: List[Tuple[int, int, str]] = []
    for it, (_mode, name, alias_tok), col in zip(items, infos, cols):
        if name is not None and name.upper() == col.upper():
            continue
        quoted = _quote_ident(col)
        if alias_tok is not None:
            edits.append((alias_tok.start, alias_tok.end, quoted))
        else:
            end = it.toks[-1].end
            edits.append((end, end, " AS " + quoted))
    return edits


def _select_output_names(toks: Sequence[Token], ts: int, te: int) -> Optional[List[str]]:
    """Derive the output column names of the query in ``toks[ts:te)``.

    Args:
        toks: Token list.
        ts: Index of the query's first token.
        te: Exclusive end index.

    Returns:
        The names, or ``None`` when any item's name cannot be determined
        exactly.
    """
    items = _split_select_items(toks, ts, te)
    if items is None:
        return None
    names: List[str] = []
    for it in items:
        info = _item_output_info(it)
        if info is None or info[1] is None:
            return None
        names.append(info[1])
    return names


def _parse_ident_list(toks: Sequence[Token], i: int) -> Tuple[Optional[List[str]], int]:
    """Parse a plain parenthesized name list ``(a, b, ...)``.

    Args:
        toks: Token list; ``toks[i]`` must be ``(``.
        i: Index of the opening parenthesis.

    Returns:
        ``(names, next_index)`` on success, ``(None, i)`` otherwise.
    """
    j = i + 1
    names: List[str] = []
    n = len(toks)
    while j < n:
        t = toks[j]
        if t.kind == BACKTICK:
            names.append(unquote_identifier(t.text))
        elif t.kind == IDENT and t.upper not in RESERVED:
            names.append(t.text)
        else:
            return None, i
        j += 1
        if _is_op(toks, j, ","):
            j += 1
            continue
        if _is_op(toks, j, ")"):
            return names, j + 1
        return None, i
    return None, i


# ---------------------------------------------------------------------------
# Draft helpers
# ---------------------------------------------------------------------------


def _ops_draft(
    stmt: RawStatement,
    kind: str,
    *,
    target: Optional[TableName] = None,
    writes: bool = False,
    tspan: Optional[Tuple[int, int]] = None,
    warnings: Optional[List[Tuple[str, str, int]]] = None,
) -> ActionDraft:
    """Build a verbatim ``operations`` draft for ``stmt``.

    Args:
        stmt: The raw statement.
        kind: Uppercase original statement kind (for annotation/report).
        target: Table written by the statement, if identifiable.
        writes: True when the statement mutates ``target``.
        tspan: Span of the target path (enables ``${self()}`` substitution
            if the linker elects this action as the target's producer).
        warnings: ``(code, message, offset)`` findings.

    Returns:
        The draft.
    """
    return ActionDraft(
        action_type=ActionType.OPERATIONS,
        target=target,
        writes_target=writes,
        creates_target=False,
        body_start=stmt.start,
        body_end=stmt.end,
        stmt_start=stmt.start,
        stmt_end=stmt.terminator_end,
        primary_target_span=tspan,
        original_kind=kind,
        warnings=list(warnings or []),
    )


def _finish(
    draft: ActionDraft, toks: Sequence[Token], head: str, skip_spans: Sequence[Tuple[int, int]] = ()
) -> ActionDraft:
    """Attach reference sites to a draft and return it.

    Args:
        draft: The draft under construction.
        toks: The statement's tokens.
        head: Uppercase leading keyword (``"MERGE"`` enables the USING
            table introducer in the scanner).
        skip_spans: Spans that must not yield reference sites (e.g. the
            ``DELETE FROM`` target).

    Returns:
        The same draft, with ``ref_sites`` populated.
    """
    draft.ref_sites = scan_ref_sites(toks, head, skip_spans)
    return draft


def _append_table_ref(draft: ActionDraft, toks: Sequence[Token], i: int) -> None:
    """Append a table reference introduced by a DDL-specific keyword."""
    match = parse_table_path(toks, i)
    if match is None or not (1 <= len(match.parts) <= 3) or not all(match.parts):
        return
    draft.ref_sites.append(
        RefSite(
            match.start,
            match.end,
            TableName.from_parts(list(match.parts)),
        )
    )


# ---------------------------------------------------------------------------
# Resource-attached DDL/DCL (index, row-access policy, GRANT/REVOKE)
# ---------------------------------------------------------------------------
#
# Statements such as ``CREATE SEARCH INDEX ... ON t``, ``CREATE VECTOR INDEX
# ... ON t``, ``CREATE ROW ACCESS POLICY ... ON t`` and ``GRANT ... ON TABLE
# t`` operate on a table that must already exist. They have no typed Dataform
# equivalent, so they stay verbatim as ``operations`` - but, unlike a
# free-standing operation, they are recorded as *writers* of the affected
# table. That makes the linker order them after the table's creator, and after
# any earlier metadata mutation of the same table in source order, without ever
# electing them as the target's owner: their statement kinds are deliberately
# outside the emitter's ``_ELECTABLE`` set and they carry no
# ``primary_target_span``, so hasOutput election and ``${self()}`` rewriting
# can never apply to them.


def _find_top_level_on(toks: Sequence[Token], start: int) -> Optional[int]:
    """Index of the first ``ON`` keyword at paren/bracket depth 0, or ``None``."""
    depth = 0
    for i in range(start, len(toks)):
        t = toks[i]
        if t.kind == EOF:
            break
        if t.kind == OP:
            if t.text in ("(", "["):
                depth += 1
            elif t.text in (")", "]"):
                depth = max(0, depth - 1)
        elif depth == 0 and t.kind == IDENT and t.upper == "ON":
            return i
    return None


def _skip_dcl_resource_type(toks: Sequence[Token], j: int) -> Optional[int]:
    """Skip a ``GRANT``/``REVOKE`` ``ON`` resource-type keyword.

    Returns the index of the resource name for table-like resource types
    (``TABLE``, ``VIEW``, ``EXTERNAL TABLE``, ``MATERIALIZED VIEW`` and
    ``SNAPSHOT TABLE``), or ``None`` for resources that are not Dataform
    table actions (``SCHEMA`` and the like).
    """
    kw = _kw(toks, j)
    if kw == "EXTERNAL" and _kw(toks, j + 1) == "TABLE":
        return j + 2
    if kw == "MATERIALIZED" and _kw(toks, j + 1) == "VIEW":
        return j + 2
    if kw == "SNAPSHOT" and _kw(toks, j + 1) == "TABLE":
        return j + 2
    if kw in ("TABLE", "VIEW"):
        return j + 1
    return None


def _attached_target(toks: Sequence[Token], start: int, dcl_typed: bool) -> Optional[TableName]:
    """Parse the table an ``ON``-attached statement operates on.

    Args:
        toks: Statement tokens (EOF-terminated).
        start: Index to begin scanning for the ``ON`` clause.
        dcl_typed: When ``True`` the ``ON`` clause names a resource type
            (``GRANT``/``REVOKE`` DCL); otherwise ``ON`` is followed
            directly by the table path (index / row-access-policy DDL).

    Returns:
        The affected :class:`TableName`, or ``None`` when no table-scoped
        ``ON`` target can be parsed.
    """
    on_index = _find_top_level_on(toks, start)
    if on_index is None:
        return None
    j = on_index + 1
    if dcl_typed:
        skipped = _skip_dcl_resource_type(toks, j)
        if skipped is None:
            return None
        j = skipped
    pm = parse_table_path(toks, j)
    if pm is None or not (1 <= len(pm.parts) <= 3) or not all(pm.parts):
        return None
    return TableName.from_parts(list(pm.parts))


def _classify_attached_ddl(
    stmt: RawStatement,
    toks: List[Token],
    kind: str,
    start: int,
    warn_code: str,
) -> ActionDraft:
    """Classify index / row-access-policy DDL attached to an existing table.

    The statement is preserved verbatim. When its ``ON`` table can be
    identified it becomes a writer of that table (dependency + source
    ordering, never ownership); otherwise it is kept as a plain operation
    with a warning that no table dependency could be inferred.
    """
    head = toks[0].upper if toks and toks[0].kind == IDENT else ""
    target = _attached_target(toks, start, dcl_typed=False)
    if target is None:
        draft = _ops_draft(
            stmt,
            kind,
            warnings=[
                (
                    "FALLBACK_OPERATIONS",
                    f"{kind} kept as operations, but its target table could not "
                    "be parsed from the ON clause; no dependency on that table "
                    "was inferred, so review manual dependencies.",
                    stmt.start,
                )
            ],
        )
        return _finish(draft, toks, head)
    draft = _ops_draft(
        stmt,
        kind,
        target=target,
        writes=True,
        warnings=[
            (
                warn_code,
                f"{kind} on {target.display()} kept as operations and ordered "
                "after that table; Dataform runs the DDL verbatim.",
                stmt.start,
            )
        ],
    )
    return _finish(draft, toks, head)


def _classify_attached_drop_alter(
    stmt: RawStatement, toks: List[Token], head: str, ent: str
) -> Optional[ActionDraft]:
    """Handle index / row-access-policy DROP and ALTER forms, if matched.

    Returns ``None`` when the statement is not a resource-attached form, so
    the caller falls back to ordinary DROP/ALTER table handling.
    """
    if ent in ("SEARCH", "VECTOR") and _kw(toks, 2) == "INDEX":
        label = "SEARCH INDEX" if ent == "SEARCH" else "VECTOR INDEX"
        return _classify_attached_ddl(stmt, toks, f"{head} {label}", 3, "INDEX_DDL")
    if head == "DROP":
        if ent == "ROW" and _kw(toks, 2) == "ACCESS" and _kw(toks, 3) == "POLICY":
            return _classify_attached_ddl(
                stmt, toks, "DROP ROW ACCESS POLICY", 4, "ROW_ACCESS_POLICY_DDL"
            )
        if (
            ent == "ALL"
            and _kw(toks, 2) == "ROW"
            and _kw(toks, 3) == "ACCESS"
            and _kw(toks, 4) == "POLICIES"
        ):
            return _classify_attached_ddl(
                stmt, toks, "DROP ALL ROW ACCESS POLICIES", 5, "ROW_ACCESS_POLICY_DDL"
            )
    return None


def _classify_grant_revoke(stmt: RawStatement, toks: List[Token], head: str) -> ActionDraft:
    """Classify a ``GRANT``/``REVOKE`` DCL statement.

    When the grant targets a table-like resource the statement is recorded
    as a writer of that table so the linker orders it after the table's
    creator. Grants on other resources (a schema, for example) have no
    Dataform table action to depend on and are kept as plain operations.
    """
    target = _attached_target(toks, 1, dcl_typed=True)
    if target is None:
        return _finish(_ops_draft(stmt, head), toks, head)
    draft = _ops_draft(
        stmt,
        head,
        target=target,
        writes=True,
        warnings=[
            (
                "GRANT_REVOKE_DCL",
                f"{head} on {target.display()} kept as operations and ordered "
                "after that table; Dataform applies the access change verbatim.",
                stmt.start,
            )
        ],
    )
    return _finish(draft, toks, head)


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------


def classify_statement(stmt: RawStatement, text: str, opts: ConversionOptions) -> ActionDraft:
    """Classify one top-level statement into a Dataform action draft.

    Args:
        stmt: The raw statement from the splitter.
        text: Full source text of the file (for raw span capture).
        opts: Conversion options.

    Returns:
        An :class:`~sql2sqlx.model.ActionDraft`. This function never
        raises for valid-but-unsupported SQL - such statements become
        ``operations`` drafts with explanatory warnings.
    """
    toks = _with_eof(stmt)
    first = toks[0]
    head = first.upper if first.kind == IDENT else ""
    if head == "CREATE":
        return _classify_create(stmt, toks, text, opts)
    if head == "INSERT":
        return _classify_insert(stmt, toks, text, opts)
    if head == "MERGE":
        return _classify_merge(stmt, toks, text, opts)
    if head in ("UPDATE", "DELETE", "TRUNCATE", "DROP", "ALTER", "LOAD"):
        return _classify_dml(stmt, toks, head)
    if head in ("GRANT", "REVOKE"):
        return _classify_grant_revoke(stmt, toks, head)
    if head == "CALL" or (head == "EXECUTE" and _kw(toks, 1) == "IMMEDIATE"):
        draft = _ops_draft(
            stmt,
            head,
            warnings=[
                (
                    "DYNAMIC_SIDE_EFFECTS",
                    "Called procedures and dynamic SQL can hide table reads or "
                    "writes; kept verbatim, but review manual dependencies.",
                    stmt.start,
                )
            ],
        )
        return _finish(draft, toks, head)
    if head in ("SELECT", "WITH", "FROM") or (first.kind == OP and first.text == "("):
        draft = _ops_draft(
            stmt,
            head or "SELECT",
            warnings=[
                (
                    "ORPHAN_SELECT",
                    "Standalone query converted to an operations action; review "
                    "whether it should be a table, view or assertion instead.",
                    stmt.start,
                )
            ],
        )
        return _finish(draft, toks, head or "SELECT")
    return _finish(_ops_draft(stmt, head or "STATEMENT"), toks, head)


# ---------------------------------------------------------------------------
# CREATE handling
# ---------------------------------------------------------------------------


def _classify_create(
    stmt: RawStatement, toks: List[Token], text: str, opts: ConversionOptions
) -> ActionDraft:
    """Classify a ``CREATE ...`` statement (dispatch on entity/modifiers)."""
    i = 1
    or_replace = False
    if _kw(toks, i) == "OR" and _kw(toks, i + 1) == "REPLACE":
        or_replace = True
        i += 2
    temp = _kw(toks, i) in ("TEMP", "TEMPORARY")
    if temp:
        i += 1
    materialized = _kw(toks, i) == "MATERIALIZED"
    if materialized:
        i += 1
    external = _kw(toks, i) == "EXTERNAL"
    if external:
        i += 1
    snapshot = _kw(toks, i) == "SNAPSHOT"
    if snapshot:
        i += 1
    entity = _kw(toks, i)

    if entity == "TABLE" and _kw(toks, i + 1) == "FUNCTION":
        return _finish(_ops_draft(stmt, "CREATE TABLE FUNCTION"), toks, "CREATE")
    if entity == "PROCEDURE":
        # The body is stored for later execution; DML within it is not an
        # immediate read/write dependency of the CREATE action. Keeping the
        # definition source-faithful also preserves the procedure owner's
        # default-project resolution rules.
        return _ops_draft(
            stmt,
            "CREATE PROCEDURE",
            warnings=[
                (
                    "PROCEDURE_PRESERVED",
                    "Stored procedure definition kept verbatim; its body is not "
                    "linked as an immediately executed workflow dependency.",
                    stmt.start,
                )
            ],
        )
    if entity in ("SEARCH", "VECTOR") and _kw(toks, i + 1) == "INDEX":
        label = "SEARCH INDEX" if entity == "SEARCH" else "VECTOR INDEX"
        return _classify_attached_ddl(stmt, toks, f"CREATE {label}", i + 2, "INDEX_DDL")
    if entity == "ROW" and _kw(toks, i + 1) == "ACCESS" and _kw(toks, i + 2) == "POLICY":
        return _classify_attached_ddl(
            stmt, toks, "CREATE ROW ACCESS POLICY", i + 3, "ROW_ACCESS_POLICY_DDL"
        )
    if entity not in ("TABLE", "VIEW"):
        kind = f"CREATE {entity}" if entity else "CREATE"
        return _finish(_ops_draft(stmt, kind), toks, "CREATE")
    i += 1
    ine = False
    if _kw(toks, i) == "IF" and _kw(toks, i + 1) == "NOT" and _kw(toks, i + 2) == "EXISTS":
        ine = True
        i += 3
    pm = parse_table_path(toks, i)
    if pm is None or not (1 <= len(pm.parts) <= 3) or not all(pm.parts):
        draft = _ops_draft(
            stmt,
            f"CREATE {entity}",
            warnings=[
                (
                    "FALLBACK_OPERATIONS",
                    f"Could not parse the CREATE {entity} target path; " "statement kept verbatim.",
                    stmt.start,
                )
            ],
        )
        return _finish(draft, toks, "CREATE")
    target = TableName.from_parts(list(pm.parts))
    tspan = (pm.start, pm.end)
    i = pm.next_index

    if temp:
        draft = _ops_draft(
            stmt,
            f"CREATE TEMP {entity}",
            warnings=[
                (
                    "TEMP_TABLE",
                    "Temporary objects have no Dataform action equivalent; " "kept as operations.",
                    stmt.start,
                )
            ],
        )
        return _finish(draft, toks, "CREATE")
    if external:
        draft = _ops_draft(
            stmt,
            "CREATE EXTERNAL TABLE",
            target=target,
            writes=True,
            tspan=tspan,
            warnings=[
                (
                    "EXTERNAL_TABLE",
                    f"External table DDL for {target.display()} kept as "
                    "operations; consider replacing it with a Dataform "
                    "declaration if the table is managed elsewhere.",
                    stmt.start,
                )
            ],
        )
        return _finish(draft, toks, "CREATE")
    if snapshot:
        draft = _ops_draft(
            stmt,
            "CREATE SNAPSHOT TABLE",
            target=target,
            writes=True,
            tspan=tspan,
            warnings=[("SNAPSHOT_TABLE", "Snapshot DDL kept as operations.", stmt.start)],
        )
        _finish(draft, toks, "CREATE")
        if _kw(toks, i) == "CLONE":
            _append_table_ref(draft, toks, i + 1)
        return draft

    if entity == "VIEW":
        return _classify_view_body(
            stmt, toks, text, opts, i, target, tspan, materialized, or_replace, ine
        )
    return _classify_table_body(stmt, toks, text, opts, i, target, tspan, or_replace, ine)


def _scan_pre_as_clauses(
    toks: List[Token], i: int, text: str, allow_columns: bool
) -> Union[Tuple[str, str], Dict[str, Any]]:
    """Scan the clauses between a CREATE target and ``AS``.

    Recognizes an optional column definition list (position-gated to
    directly after the target), ``PARTITION BY``, ``CLUSTER BY`` and
    ``OPTIONS(...)``. Anything else is a strict fallback: pretending to
    understand an unknown clause would risk silently changing semantics.

    Args:
        toks: Token list (EOF-terminated).
        i: Index of the first token after the target path.
        text: Full source text.
        allow_columns: Whether a column list may appear (tables only).

    Returns:
        On success, a dict with keys ``as_index`` (int or ``None``),
        ``has_columns`` (bool), ``partition`` (raw text or ``None``),
        ``cluster`` (list of raw texts) and ``options`` (entries or
        ``None``). On failure, ``("fallback", reason)``.
    """
    info: Dict[str, Any] = {
        "as_index": None,
        "has_columns": False,
        "partition": None,
        "cluster": [],
        "options": None,
    }
    first = True
    n = len(toks)
    while i < n:
        t = toks[i]
        if t.kind == EOF:
            break
        if t.kind == OP and t.text == "(":
            if allow_columns and first:
                info["has_columns"] = True
                i = _skip_balanced(toks, i)
                first = False
                continue
            return ("fallback", "unexpected '(' before AS")
        kw = _kw(toks, i)
        if kw == "AS":
            info["as_index"] = i
            break
        if kw == "PARTITION" and _kw(toks, i + 1) == "BY":
            j = i + 2
            if j >= n or toks[j].kind == EOF:
                return ("fallback", "empty PARTITION BY expression")
            start = toks[j].start
            endpos = start
            depth = 0
            while j < n and toks[j].kind != EOF:
                tj = toks[j]
                if tj.kind == OP:
                    if tj.text in ("(", "["):
                        depth += 1
                    elif tj.text in (")", "]"):
                        depth -= 1
                elif (
                    tj.kind == IDENT
                    and depth == 0
                    and tj.upper in ("CLUSTER", "OPTIONS", "AS", "DEFAULT")
                ):
                    break
                endpos = tj.end
                j += 1
            if endpos <= start:
                return ("fallback", "empty PARTITION BY expression")
            info["partition"] = text[start:endpos].strip()
            i = j
            first = False
            continue
        if kw == "CLUSTER" and _kw(toks, i + 1) == "BY":
            j = i + 2
            if j >= n or toks[j].kind == EOF:
                return ("fallback", "empty CLUSTER BY list")
            depth = 0
            item_start = toks[j].start
            last_end = item_start
            items: List[str] = []
            while j < n and toks[j].kind != EOF:
                tj = toks[j]
                if tj.kind == OP:
                    if tj.text in ("(", "["):
                        depth += 1
                    elif tj.text in (")", "]"):
                        depth -= 1
                    elif tj.text == "," and depth == 0:
                        items.append(text[item_start:last_end].strip())
                        j += 1
                        if j < n and toks[j].kind != EOF:
                            item_start = toks[j].start
                            last_end = item_start
                        continue
                elif (
                    tj.kind == IDENT
                    and depth == 0
                    and tj.upper in ("OPTIONS", "AS", "DEFAULT", "PARTITION")
                ):
                    break
                last_end = tj.end
                j += 1
            if last_end > item_start:
                items.append(text[item_start:last_end].strip())
            if not items or not all(items):
                return ("fallback", "empty CLUSTER BY list")
            info["cluster"] = items
            i = j
            first = False
            continue
        if kw == "OPTIONS":
            parsed = _parse_options(toks, i, text)
            if parsed is None:
                return ("fallback", "unparseable OPTIONS(...) clause")
            info["options"], i = parsed
            first = False
            continue
        if kw == "DEFAULT":
            return ("fallback", "DEFAULT COLLATE has no Dataform config equivalent")
        if kw in ("LIKE", "CLONE", "COPY"):
            return ("fallback", f"CREATE TABLE ... {kw} has no typed Dataform equivalent")
        return ("fallback", f"unrecognized clause {toks[i].text!r} before AS")
    return info


def _apply_table_metadata(config: Dict[str, Any], info: Dict[str, Any]) -> None:
    """Map scanned PARTITION/CLUSTER/OPTIONS metadata into a config dict.

    ``description``, ``labels``, ``partition_expiration_days`` and
    ``require_partition_filter`` map onto first-class Dataform fields when
    their values are simple literals; every other option (or a complex
    value) is preserved verbatim in ``bigquery.additionalOptions`` so the
    compiled DDL keeps it.

    Args:
        config: Draft config dict to mutate.
        info: Result of :func:`_scan_pre_as_clauses`.
    """
    bq: Dict[str, Any] = {}
    if info.get("partition"):
        bq["partitionBy"] = info["partition"]
    if info.get("cluster"):
        bq["clusterBy"] = list(info["cluster"])
    addl: Dict[str, str] = {}
    for key, raw, val in info.get("options") or []:
        lk = key.lower()
        if lk == "description" and isinstance(val, str):
            config["description"] = val
        elif lk == "labels" and isinstance(val, dict):
            bq["labels"] = val
        elif (
            lk == "partition_expiration_days"
            and isinstance(val, (int, float))
            and not isinstance(val, bool)
        ):
            bq["partitionExpirationDays"] = val
        elif lk == "require_partition_filter" and isinstance(val, bool):
            bq["requirePartitionFilter"] = val
        else:
            addl[key] = raw
    if addl:
        bq["additionalOptions"] = addl
    if bq:
        config["bigquery"] = bq


def _classify_table_body(
    stmt: RawStatement,
    toks: List[Token],
    text: str,
    opts: ConversionOptions,
    i: int,
    target: TableName,
    tspan: Tuple[int, int],
    or_replace: bool,
    ine: bool,
) -> ActionDraft:
    """Classify ``CREATE [OR REPLACE] TABLE`` after its target path."""
    source_keyword = _kw(toks, i)
    if source_keyword in ("LIKE", "CLONE", "COPY"):
        draft = _ops_draft(
            stmt,
            "CREATE TABLE",
            target=target,
            writes=True,
            tspan=tspan,
            warnings=[
                (
                    "FALLBACK_OPERATIONS",
                    f"CREATE TABLE ... {source_keyword} has no typed Dataform "
                    "equivalent; kept as operations.",
                    stmt.start,
                )
            ],
        )
        _finish(draft, toks, "CREATE")
        _append_table_ref(draft, toks, i + 1)
        return draft
    info = _scan_pre_as_clauses(toks, i, text, allow_columns=True)
    if isinstance(info, tuple):
        draft = _ops_draft(
            stmt,
            "CREATE TABLE",
            target=target,
            writes=True,
            tspan=tspan,
            warnings=[
                ("FALLBACK_OPERATIONS", f"CREATE TABLE kept as operations: {info[1]}.", stmt.start)
            ],
        )
        return _finish(draft, toks, "CREATE")
    if info["as_index"] is None:
        return _plain_create(stmt, toks, opts, target, tspan, or_replace, ine, info)
    if info["has_columns"]:
        draft = _ops_draft(
            stmt,
            "CREATE TABLE",
            target=target,
            writes=True,
            tspan=tspan,
            warnings=[
                (
                    "COLUMN_DDL",
                    "CREATE TABLE ... AS with an explicit column "
                    "list can carry types or constraints that "
                    "Dataform's table type cannot express; kept "
                    "as operations.",
                    stmt.start,
                )
            ],
        )
        return _finish(draft, toks, "CREATE")
    if ine and opts.if_not_exists_strategy is IfNotExistsStrategy.OPERATIONS:
        draft = _ops_draft(
            stmt,
            "CREATE TABLE",
            target=target,
            writes=True,
            tspan=tspan,
            warnings=[
                (
                    "IF_NOT_EXISTS",
                    "IF NOT EXISTS preserved verbatim as " "operations per options.",
                    stmt.start,
                )
            ],
        )
        return _finish(draft, toks, "CREATE")
    bi = info["as_index"] + 1
    if not _body_head_ok(toks, bi):
        draft = _ops_draft(
            stmt,
            "CREATE TABLE",
            target=target,
            writes=True,
            tspan=tspan,
            warnings=[
                (
                    "FALLBACK_OPERATIONS",
                    "CREATE TABLE AS is not followed by a query; " "kept as operations.",
                    stmt.start,
                )
            ],
        )
        return _finish(draft, toks, "CREATE")
    warnings: List[Tuple[str, str, int]] = []
    if ine:
        warnings.append(
            (
                "IF_NOT_EXISTS",
                f"CREATE TABLE IF NOT EXISTS {target.display()} converted to "
                'type "table": Dataform always creates or replaces, so the '
                "'only if absent' guard is lost. Use "
                "--if-not-exists operations to preserve it verbatim.",
                stmt.start,
            )
        )
    elif not or_replace:
        warnings.append(
            (
                "CREATE_REPLACE_SEMANTICS",
                f"CREATE TABLE {target.display()} converted to a Dataform table; "
                "subsequent Dataform runs rebuild the target instead of failing "
                "when it already exists.",
                stmt.start,
            )
        )
    config: Dict[str, Any] = {}
    _apply_table_metadata(config, info)
    draft = ActionDraft(
        action_type=ActionType.TABLE,
        target=target,
        writes_target=False,
        creates_target=True,
        body_start=toks[bi].start,
        body_end=stmt.end,
        stmt_start=stmt.start,
        stmt_end=stmt.terminator_end,
        config=config,
        original_kind="CREATE TABLE",
        warnings=warnings,
    )
    return _finish(draft, toks, "CREATE")


def _plain_create(
    stmt: RawStatement,
    toks: List[Token],
    opts: ConversionOptions,
    target: TableName,
    tspan: Tuple[int, int],
    or_replace: bool,
    ine: bool,
    info: Dict[str, Any],
) -> ActionDraft:
    """Handle ``CREATE TABLE`` without ``AS`` (schema-only DDL)."""
    if opts.plain_create_strategy is PlainCreateStrategy.DECLARATION:
        config: Dict[str, Any] = {}
        _apply_table_metadata(config, info)
        config.pop("bigquery", None)
        draft = ActionDraft(
            action_type=ActionType.DECLARATION,
            target=target,
            writes_target=False,
            creates_target=True,
            body_start=stmt.end,
            body_end=stmt.end,
            stmt_start=stmt.start,
            stmt_end=stmt.terminator_end,
            config=config,
            original_kind="CREATE TABLE",
            warnings=[
                (
                    "DECLARATION_DROPPED_DDL",
                    f"Plain CREATE TABLE {target.display()} emitted as a "
                    "declaration; the DDL itself was dropped (schema "
                    "assumed to be managed outside Dataform).",
                    stmt.start,
                )
            ],
        )
        return _finish(draft, toks, "CREATE")
    warns: List[Tuple[str, str, int]] = [
        (
            "CREATE_NO_AS",
            f"Plain CREATE TABLE {target.display()} (no AS query) kept as "
            "operations; use --plain-create declaration to emit a source "
            "declaration instead.",
            stmt.start,
        )
    ]
    if not ine and not or_replace:
        warns.append(
            (
                "RERUN_RISK",
                "This CREATE TABLE has neither OR REPLACE nor IF NOT EXISTS; "
                "as a repeated Dataform operation it will fail on the second "
                "run.",
                stmt.start,
            )
        )
    draft = _ops_draft(
        stmt, "CREATE TABLE", target=target, writes=True, tspan=tspan, warnings=warns
    )
    return _finish(draft, toks, "CREATE")


def _parse_view_columns(
    toks: List[Token], i: int, text: str
) -> Optional[Tuple[List[Tuple[str, Optional[str]]], int]]:
    """Parse a view column list ``(name [OPTIONS(description='..')], ...)``.

    Args:
        toks: Token list; ``toks[i]`` must be ``(``.
        i: Index of the opening parenthesis.
        text: Full source text.

    Returns:
        ``([(name, description_or_None), ...], next_index)`` or ``None``
        for any other shape (caller falls back to operations).
    """
    j = i + 1
    out: List[Tuple[str, Optional[str]]] = []
    n = len(toks)
    while j < n:
        t = toks[j]
        if t.kind == BACKTICK:
            name = unquote_identifier(t.text)
        elif t.kind == IDENT and t.upper not in RESERVED:
            name = t.text
        else:
            return None
        j += 1
        desc: Optional[str] = None
        if _kw(toks, j) == "OPTIONS":
            parsed = _parse_options(toks, j, text)
            if parsed is None:
                return None
            entries, j = parsed
            if (
                len(entries) != 1
                or entries[0][0].lower() != "description"
                or not isinstance(entries[0][2], str)
            ):
                return None
            desc = entries[0][2]
        out.append((name, desc))
        if _is_op(toks, j, ","):
            j += 1
            continue
        if _is_op(toks, j, ")"):
            return out, j + 1
        return None
    return None


def _classify_view_body(
    stmt: RawStatement,
    toks: List[Token],
    text: str,
    opts: ConversionOptions,
    i: int,
    target: TableName,
    tspan: Tuple[int, int],
    materialized: bool,
    or_replace: bool,
    ine: bool,
) -> ActionDraft:
    """Classify ``CREATE [MATERIALIZED] VIEW`` after its target path."""
    kind = "CREATE MATERIALIZED VIEW" if materialized else "CREATE VIEW"

    def fallback(code: str, msg: str, source_index: Optional[int] = None) -> ActionDraft:
        draft = _ops_draft(
            stmt, kind, target=target, writes=True, tspan=tspan, warnings=[(code, msg, stmt.start)]
        )
        _finish(draft, toks, "CREATE")
        if source_index is not None:
            _append_table_ref(draft, toks, source_index)
        return draft

    columns: Optional[List[Tuple[str, Optional[str]]]] = None
    if _is_op(toks, i, "("):
        parsed_cols = _parse_view_columns(toks, i, text)
        if parsed_cols is None:
            return fallback(
                "FALLBACK_OPERATIONS",
                "View column list uses a form that cannot be "
                "safely converted; kept as operations.",
            )
        columns, i = parsed_cols
    info = _scan_pre_as_clauses(toks, i, text, allow_columns=False)
    if isinstance(info, tuple):
        return fallback("FALLBACK_OPERATIONS", f"{kind} kept as operations: {info[1]}.")
    if info["as_index"] is None:
        return fallback("FALLBACK_OPERATIONS", f"{kind} has no AS query; kept as operations.")
    bi = info["as_index"] + 1
    if _kw(toks, bi) == "REPLICA":
        return fallback(
            "FALLBACK_OPERATIONS",
            "MATERIALIZED VIEW ... AS REPLICA OF has no typed " "equivalent; kept as operations.",
            bi + 2 if _kw(toks, bi + 1) == "OF" else None,
        )
    if not _body_head_ok(toks, bi):
        return fallback(
            "FALLBACK_OPERATIONS", f"{kind} AS is not followed by a query; kept as " "operations."
        )
    if ine and opts.if_not_exists_strategy is IfNotExistsStrategy.OPERATIONS:
        return fallback(
            "IF_NOT_EXISTS", "IF NOT EXISTS preserved verbatim as operations per " "options."
        )
    edits: List[Tuple[int, int, str]] = []
    if columns:
        maybe = _alias_select_list_edits(toks, bi, len(toks) - 1, [c[0] for c in columns])
        if maybe is None:
            return fallback(
                "FALLBACK_SELECT_ALIAS",
                "The view column list could not be safely pushed "
                "into the select list; kept as operations.",
            )
        edits = maybe
    warnings: List[Tuple[str, str, int]] = []
    if ine:
        warnings.append(
            (
                "IF_NOT_EXISTS",
                f"{kind} IF NOT EXISTS {target.display()} converted to type "
                '"view": Dataform always creates or replaces, so the guard is '
                "lost.",
                stmt.start,
            )
        )
    elif not or_replace:
        warnings.append(
            (
                "CREATE_REPLACE_SEMANTICS",
                f"{kind} {target.display()} converted to a Dataform view; "
                "subsequent Dataform runs rebuild the target instead of failing "
                "when it already exists.",
                stmt.start,
            )
        )
    config: Dict[str, Any] = {}
    if materialized:
        config["materialized"] = True
    _apply_table_metadata(config, info)
    if columns:
        described = {name: desc for name, desc in columns if desc}
        if described:
            config["columns"] = described
    draft = ActionDraft(
        action_type=ActionType.VIEW,
        target=target,
        writes_target=False,
        creates_target=True,
        body_start=toks[bi].start,
        body_end=stmt.end,
        stmt_start=stmt.start,
        stmt_end=stmt.terminator_end,
        edits=edits,
        config=config,
        original_kind=kind,
        warnings=warnings,
    )
    return _finish(draft, toks, "CREATE")


# ---------------------------------------------------------------------------
# INSERT handling
# ---------------------------------------------------------------------------


def _classify_insert(
    stmt: RawStatement, toks: List[Token], text: str, opts: ConversionOptions
) -> ActionDraft:
    """Classify ``INSERT [INTO] target [(cols)] <query|VALUES ...>``."""
    i = 1
    if _kw(toks, i) == "INTO":
        i += 1
    pm = parse_table_path(toks, i)
    if pm is None or not (1 <= len(pm.parts) <= 3) or not all(pm.parts):
        draft = _ops_draft(
            stmt,
            "INSERT",
            warnings=[
                (
                    "FALLBACK_OPERATIONS",
                    "Could not parse the INSERT target path; kept verbatim.",
                    stmt.start,
                )
            ],
        )
        return _finish(draft, toks, "INSERT")
    target = TableName.from_parts(list(pm.parts))
    tspan = (pm.start, pm.end)
    i = pm.next_index
    cols: Optional[List[str]] = None
    if _is_op(toks, i, "("):
        cols, ni = _parse_ident_list(toks, i)
        if cols is None:
            draft = _ops_draft(
                stmt,
                "INSERT",
                target=target,
                writes=True,
                tspan=tspan,
                warnings=[
                    (
                        "FALLBACK_OPERATIONS",
                        "Unrecognized INSERT column list; kept " "verbatim.",
                        stmt.start,
                    )
                ],
            )
            return _finish(draft, toks, "INSERT")
        i = ni
    if _kw(toks, i) == "VALUES":
        draft = _ops_draft(
            stmt,
            "INSERT",
            target=target,
            writes=True,
            tspan=tspan,
            warnings=[
                (
                    "INSERT_VALUES",
                    "INSERT ... VALUES has no query body; kept as " "operations.",
                    stmt.start,
                )
            ],
        )
        return _finish(draft, toks, "INSERT")
    if opts.insert_strategy is InsertStrategy.OPERATIONS:
        draft = _ops_draft(stmt, "INSERT", target=target, writes=True, tspan=tspan)
        return _finish(draft, toks, "INSERT")
    if not _body_head_ok(toks, i):
        draft = _ops_draft(
            stmt,
            "INSERT",
            target=target,
            writes=True,
            tspan=tspan,
            warnings=[
                (
                    "FALLBACK_OPERATIONS",
                    "INSERT body is not a plain query; kept " "verbatim.",
                    stmt.start,
                )
            ],
        )
        return _finish(draft, toks, "INSERT")
    output_names = cols or _select_output_names(toks, i, len(toks) - 1)
    if (
        output_names is None
        or len({name.upper() for name in output_names}) != len(output_names)
        or any(not _dataform_insert_column(name) for name in output_names)
    ):
        draft = _ops_draft(
            stmt,
            "INSERT",
            target=target,
            writes=True,
            tspan=tspan,
            warnings=[
                (
                    "FALLBACK_SELECT_ALIAS",
                    "The incremental query's output column names "
                    "are not all exactly derivable, unique, and "
                    "safe for Dataform Core's runtime backtick "
                    "quoting; the statement was kept verbatim.",
                    stmt.start,
                )
            ],
        )
        return _finish(draft, toks, "INSERT")
    edits: List[Tuple[int, int, str]] = []
    if cols:
        maybe = _alias_select_list_edits(toks, i, len(toks) - 1, cols)
        if maybe is None:
            draft = _ops_draft(
                stmt,
                "INSERT",
                target=target,
                writes=True,
                tspan=tspan,
                warnings=[
                    (
                        "FALLBACK_SELECT_ALIAS",
                        "The INSERT column list could not be "
                        "safely pushed into the select list (star "
                        "expansion, INTERVAL/typed constructors, "
                        "or arity mismatch); kept verbatim.",
                        stmt.start,
                    )
                ],
            )
            return _finish(draft, toks, "INSERT")
        edits = maybe
    config: Dict[str, Any] = {}
    if opts.protect_incrementals:
        config["protected"] = True
    draft = ActionDraft(
        action_type=ActionType.INCREMENTAL,
        target=target,
        writes_target=False,
        creates_target=True,
        body_start=toks[i].start,
        body_end=stmt.end,
        stmt_start=stmt.start,
        stmt_end=stmt.terminator_end,
        edits=edits,
        config=config,
        original_kind="INSERT",
        warnings=[
            (
                "INSERT_INCREMENTAL",
                f"INSERT INTO {target.display()} converted to type "
                '"incremental": on first run or --full-refresh Dataform will '
                "(re)create the table from this query. Wrap date filters in "
                "${when(incremental(), ...)} if they should apply only to "
                "incremental runs. On later runs Dataform projects every "
                "existing target column by name, so the query output must match "
                "that target schema.",
                stmt.start,
            )
        ],
    )
    return _finish(draft, toks, "INSERT")


# ---------------------------------------------------------------------------
# MERGE handling
# ---------------------------------------------------------------------------


def _read_qualified_col(toks: Sequence[Token], j: int) -> Optional[Tuple[str, str, int]]:
    """Read a strictly qualified column ``qual.col`` at index ``j``.

    Returns:
        ``(qualifier, column, next_index)`` or ``None``.
    """
    if j + 2 >= len(toks):
        return None
    a, dot, b = toks[j], toks[j + 1], toks[j + 2]
    if a.kind not in (IDENT, BACKTICK) or (a.kind == IDENT and a.upper in RESERVED):
        return None
    if not (dot.kind == OP and dot.text == "."):
        return None
    if b.kind not in (IDENT, BACKTICK) or (b.kind == IDENT and b.upper in RESERVED):
        return None
    return (unquote_identifier(a.text), unquote_identifier(b.text), j + 3)


def _read_set_target_col(toks: Sequence[Token], j: int, tq: str) -> Optional[Tuple[str, int]]:
    """Read an UPDATE SET target column, optionally target-qualified.

    Args:
        toks: Token list.
        j: Start index.
        tq: Uppercased target qualifier (alias or table name).

    Returns:
        ``(column, next_index)`` or ``None``.
    """
    q = _read_qualified_col(toks, j)
    if q is not None:
        if q[0].upper() != tq:
            return None
        return q[1], q[2]
    if j < len(toks):
        t = toks[j]
        if t.kind == BACKTICK or (t.kind == IDENT and t.upper not in RESERVED):
            return unquote_identifier(t.text), j + 1
    return None


def _merge_safe_extract(
    toks: List[Token], i: int, target: TableName, talias: Optional[str]
) -> Union[str, Tuple[int, int, List[str]]]:
    """Prove a MERGE equivalent to a Dataform ``uniqueKey`` incremental.

    The proof requires *all* of: a subquery source with an alias; an ``ON``
    clause that is a pure conjunction of same-named target/source column
    equalities; exactly ``WHEN MATCHED THEN UPDATE SET`` with only direct
    same-named source assignments; exactly ``WHEN NOT MATCHED [BY TARGET]
    THEN INSERT`` with explicit matching source columns; and a
    source select list whose derivable output names are exactly the key
    columns plus the updated columns. Under those conditions the MERGE
    Dataform generates for ``type: "incremental"`` + ``uniqueKey`` has
    identical row-level effects.

    Args:
        toks: Statement tokens (EOF-terminated).
        i: Index of the expected ``USING`` keyword.
        target: The MERGE target table.
        talias: The target alias, if any.

    Returns:
        ``(body_start_offset, body_end_offset, unique_keys)`` on success,
        or a human-readable failure reason string.
    """
    if _kw(toks, i) != "USING":
        return "unrecognized MERGE shape"
    i += 1
    if not _is_op(toks, i, "("):
        return "USING source is a table, not a subquery"
    open_i = i
    after = _skip_balanced(toks, i)
    close_i = after - 1
    if not _is_op(toks, close_i, ")"):
        return "unterminated USING subquery"
    inner_ts, inner_te = open_i + 1, close_i
    if inner_ts >= inner_te:
        return "empty USING subquery"
    j = after
    if _kw(toks, j) == "AS":
        j += 1
    salias: Optional[str] = None
    if j < len(toks):
        t = toks[j]
        if t.kind == BACKTICK or (t.kind == IDENT and t.upper not in RESERVED):
            salias = unquote_identifier(t.text)
            j += 1
    if salias is None:
        return "the USING subquery has no alias"
    if _kw(toks, j) != "ON":
        return "unrecognized MERGE shape after USING"
    j += 1
    tq = (talias or target.table).upper()
    sq = salias.upper()
    if tq == sq:
        return "target and source aliases are not distinct"
    keys: List[str] = []
    while True:
        left = _read_qualified_col(toks, j)
        if left is None:
            return "ON is not a conjunction of simple qualified equalities"
        lq, lc, j = left
        if not _is_op(toks, j, "="):
            return "ON is not a conjunction of simple qualified equalities"
        j += 1
        right = _read_qualified_col(toks, j)
        if right is None:
            return "ON is not a conjunction of simple qualified equalities"
        rq, rc, j = right
        if lc.upper() != rc.upper():
            return "ON joins differently named columns"
        if {lq.upper(), rq.upper()} != {tq, sq}:
            return "ON does not compare target columns to source columns"
        keys.append(lc if lq.upper() == tq else rc)
        nxt = _kw(toks, j)
        if nxt == "AND":
            j += 1
            continue
        if nxt == "WHEN":
            break
        return "unsupported ON clause"
    if not (_kw(toks, j) == "WHEN" and _kw(toks, j + 1) == "MATCHED"):
        return "first WHEN clause is not WHEN MATCHED"
    j += 2
    if _kw(toks, j) == "AND":
        return "conditional WHEN MATCHED clause"
    if not (_kw(toks, j) == "THEN" and _kw(toks, j + 1) == "UPDATE" and _kw(toks, j + 2) == "SET"):
        return "WHEN MATCHED does not perform a plain UPDATE SET"
    j += 3
    set_cols: List[str] = []
    while True:
        left_set = _read_set_target_col(toks, j, tq)
        if left_set is None:
            return "UPDATE SET has a non-trivial assignment target"
        lc, j = left_set
        if not _is_op(toks, j, "="):
            return "UPDATE SET has a non-trivial assignment"
        j += 1
        right = _read_qualified_col(toks, j)
        if right is None or right[0].upper() != sq:
            return "UPDATE SET assigns something other than a source column"
        if right[1].upper() != lc.upper():
            return "UPDATE SET renames a column"
        j = right[2]
        set_cols.append(lc)
        if _is_op(toks, j, ","):
            j += 1
            continue
        if _kw(toks, j) == "WHEN":
            break
        return "unsupported UPDATE SET clause"
    if not (_kw(toks, j) == "WHEN" and _kw(toks, j + 1) == "NOT" and _kw(toks, j + 2) == "MATCHED"):
        return "second WHEN clause is not WHEN NOT MATCHED"
    j += 3
    if _kw(toks, j) == "BY":
        if _kw(toks, j + 1) != "TARGET":
            return "WHEN NOT MATCHED BY SOURCE is not expressible"
        j += 2
    if _kw(toks, j) == "AND":
        return "conditional WHEN NOT MATCHED clause"
    if not (_kw(toks, j) == "THEN" and _kw(toks, j + 1) == "INSERT"):
        return "WHEN NOT MATCHED does not perform a plain INSERT"
    j += 2
    outputs = _select_output_names(toks, inner_ts, inner_te)
    if outputs is None:
        return "cannot derive the source subquery's output column names"
    if len({output.upper() for output in outputs}) != len(outputs):
        return "the source subquery has duplicate output column names"
    if len({key.upper() for key in keys}) != len(keys):
        return "the MERGE ON clause repeats a unique-key column"
    if len({column.upper() for column in set_cols}) != len(set_cols):
        return "UPDATE SET assigns the same target column more than once"
    out_set = {o.upper() for o in outputs}
    key_set = {k.upper() for k in keys}
    set_col_set = {c.upper() for c in set_cols}
    if not key_set.issubset(set_col_set):
        return (
            "Dataform updates unique-key columns during its generated "
            "MERGE, but the original UPDATE SET does not"
        )
    if any(not _dataform_merge_column(output) for output in outputs):
        return (
            "Dataform cannot safely emit a quoted or reserved source "
            "column name in its generated MERGE"
        )
    need = key_set | set_col_set
    if out_set != need:
        return "source columns do not exactly cover the key + updated columns"
    if _kw(toks, j) == "ROW":
        return (
            "INSERT ROW depends on the target table's physical column "
            "order, which cannot be proven from SQL text alone"
        )
    else:
        if not _is_op(toks, j, "("):
            return "WHEN NOT MATCHED INSERT is not (columns) VALUES or ROW"
        cols, j = _parse_ident_list(toks, j)
        if cols is None:
            return "WHEN NOT MATCHED INSERT is not (columns) VALUES or ROW"
        if len({column.upper() for column in cols}) != len(cols):
            return "INSERT column list contains duplicates"
        if _kw(toks, j) != "VALUES" or not _is_op(toks, j + 1, "("):
            return "WHEN NOT MATCHED INSERT is not (columns) VALUES (...)"
        j += 2
        for idx, col in enumerate(cols):
            right = _read_qualified_col(toks, j)
            if right is None or right[0].upper() != sq or right[1].upper() != col.upper():
                return "INSERT VALUES are not the matching source columns"
            j = right[2]
            if idx < len(cols) - 1:
                if not _is_op(toks, j, ","):
                    return "INSERT VALUES are not the matching source columns"
                j += 1
        if not _is_op(toks, j, ")"):
            return "INSERT VALUES are not the matching source columns"
        j += 1
        if {c.upper() for c in cols} != out_set:
            return "INSERT column list does not match the source columns"
        if [c.upper() for c in cols] != [o.upper() for o in outputs]:
            return "source output order does not match the explicit INSERT " "column order"
        if toks[j].kind != EOF:
            return "unsupported trailing MERGE clauses"
    return (toks[inner_ts].start, toks[inner_te - 1].end, keys)


def _classify_merge(
    stmt: RawStatement, toks: List[Token], text: str, opts: ConversionOptions
) -> ActionDraft:
    """Classify a ``MERGE`` statement per the configured strategy."""
    i = 1
    if _kw(toks, i) == "INTO":
        i += 1
    pm = parse_table_path(toks, i)
    if pm is None or not (1 <= len(pm.parts) <= 3) or not all(pm.parts):
        draft = _ops_draft(
            stmt,
            "MERGE",
            warnings=[
                (
                    "FALLBACK_OPERATIONS",
                    "Could not parse the MERGE target path; kept verbatim.",
                    stmt.start,
                )
            ],
        )
        return _finish(draft, toks, "MERGE")
    target = TableName.from_parts(list(pm.parts))
    tspan = (pm.start, pm.end)
    i = pm.next_index
    talias: Optional[str] = None
    if _kw(toks, i) == "AS":
        if toks[i + 1].kind in (IDENT, BACKTICK):
            talias = unquote_identifier(toks[i + 1].text)
            i += 2
    elif toks[i].kind == BACKTICK or (toks[i].kind == IDENT and toks[i].upper not in RESERVED):
        talias = unquote_identifier(toks[i].text)
        i += 1
    if opts.merge_strategy is MergeStrategy.INCREMENTAL_WHEN_SAFE:
        result = _merge_safe_extract(toks, i, target, talias)
        if not isinstance(result, str):
            body_start, body_end, keys = result
            config: Dict[str, Any] = {"uniqueKey": keys}
            if opts.protect_incrementals:
                config["protected"] = True
            draft = ActionDraft(
                action_type=ActionType.INCREMENTAL,
                target=target,
                writes_target=False,
                creates_target=True,
                body_start=body_start,
                body_end=body_end,
                stmt_start=stmt.start,
                stmt_end=stmt.terminator_end,
                config=config,
                original_kind="MERGE",
                warnings=[
                    (
                        "MERGE_INCREMENTAL",
                        f"MERGE into {target.display()} proven equivalent to an "
                        f"incremental table with uniqueKey {keys}, provided the "
                        "existing target schema exactly matches the source "
                        "output; converted.",
                        stmt.start,
                    ),
                    (
                        "TARGET_SCHEMA_REQUIRED",
                        "Dataform builds incremental MERGE assignments from "
                        "existing target metadata. Verify that its columns "
                        "exactly match this query before the first incremental "
                        "run.",
                        stmt.start,
                    ),
                ],
            )
            return _finish(draft, toks, "MERGE")
        draft = _ops_draft(
            stmt,
            "MERGE",
            target=target,
            writes=True,
            tspan=tspan,
            warnings=[("MERGE_FALLBACK", f"MERGE kept as operations ({result}).", stmt.start)],
        )
        return _finish(draft, toks, "MERGE")
    draft = _ops_draft(stmt, "MERGE", target=target, writes=True, tspan=tspan)
    return _finish(draft, toks, "MERGE")


# ---------------------------------------------------------------------------
# Other DML / DDL with identifiable write targets
# ---------------------------------------------------------------------------


def _dml_common(
    stmt: RawStatement, toks: List[Token], pm: Optional[Any], kind: str, skip_target: bool
) -> ActionDraft:
    """Finalize a DML/DDL operations draft with an optional write target.

    Args:
        stmt: The raw statement.
        toks: Statement tokens.
        pm: Parsed target path (or ``None``).
        kind: Uppercase statement kind.
        skip_target: Exclude the target span from reference scanning
            (needed for ``DELETE FROM target``, where the scanner would
            otherwise see the target as a FROM-clause table read).

    Returns:
        The draft.
    """
    if pm is None or not (1 <= len(pm.parts) <= 3) or not all(pm.parts):
        return _finish(_ops_draft(stmt, kind), toks, kind)
    target = TableName.from_parts(list(pm.parts))
    tspan = (pm.start, pm.end)
    draft = _ops_draft(stmt, kind, target=target, writes=True, tspan=tspan)
    return _finish(draft, toks, kind, skip_spans=[tspan] if skip_target else ())


def _classify_drop_alter(stmt: RawStatement, toks: List[Token], head: str) -> ActionDraft:
    """Classify DROP/ALTER, extracting the table/view target when present."""
    i = 1
    ent = _kw(toks, i)
    special = _classify_attached_drop_alter(stmt, toks, head, ent)
    if special is not None:
        return special
    if ent in ("MATERIALIZED", "EXTERNAL", "SNAPSHOT"):
        nxt = _kw(toks, i + 1)
        if (ent == "MATERIALIZED" and nxt == "VIEW") or (
            ent in ("EXTERNAL", "SNAPSHOT") and nxt == "TABLE"
        ):
            i += 2
        else:
            return _finish(_ops_draft(stmt, head), toks, head)
    elif ent in ("TABLE", "VIEW"):
        i += 1
    else:
        return _finish(_ops_draft(stmt, head), toks, head)
    if _kw(toks, i) == "IF" and _kw(toks, i + 1) == "EXISTS":
        i += 2
    pm = parse_table_path(toks, i)
    draft = _dml_common(stmt, toks, pm, head, skip_target=False)
    if (
        head == "ALTER"
        and pm is not None
        and 1 <= len(pm.parts) <= 3
        and all(pm.parts)
        and _kw(toks, pm.next_index) == "RENAME"
        and _kw(toks, pm.next_index + 1) == "TO"
    ):
        renamed_match = parse_table_path(toks, pm.next_index + 2)
        if (
            renamed_match is not None
            and 1 <= len(renamed_match.parts) <= 3
            and all(renamed_match.parts)
        ):
            original = TableName.from_parts(list(pm.parts))
            renamed = TableName.from_parts(list(renamed_match.parts))
            if renamed.project is None and renamed.dataset is None:
                renamed = TableName(
                    original.project,
                    original.dataset,
                    renamed.table,
                )
            elif renamed.project is None and original.project is not None:
                renamed = TableName(
                    original.project,
                    renamed.dataset,
                    renamed.table,
                )
            draft.extra_write_targets.append(renamed)
    return draft


def _classify_dml(stmt: RawStatement, toks: List[Token], head: str) -> ActionDraft:
    """Classify UPDATE/DELETE/TRUNCATE/DROP/ALTER/LOAD statements."""
    if head == "UPDATE":
        return _dml_common(stmt, toks, parse_table_path(toks, 1), "UPDATE", skip_target=False)
    if head == "DELETE":
        i = 2 if _kw(toks, 1) == "FROM" else 1
        return _dml_common(stmt, toks, parse_table_path(toks, i), "DELETE", skip_target=True)
    if head == "TRUNCATE":
        i = 2 if _kw(toks, 1) == "TABLE" else 1
        return _dml_common(stmt, toks, parse_table_path(toks, i), "TRUNCATE", skip_target=False)
    if head in ("DROP", "ALTER"):
        return _classify_drop_alter(stmt, toks, head)
    if head == "LOAD":
        i = 1
        if _kw(toks, i) == "DATA":
            i += 1
        if _kw(toks, i) in ("INTO", "OVERWRITE"):
            i += 1
        return _dml_common(stmt, toks, parse_table_path(toks, i), "LOAD", skip_target=False)
    return _finish(_ops_draft(stmt, head), toks, head)  # pragma: no cover
