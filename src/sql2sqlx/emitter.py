# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""Render Dataform ``.sqlx`` files.

The emitter's contract is *character fidelity for untouched SQL*:
generated bodies are produced by applying span edits (reference rewrites,
select-list aliases, ``${self()}`` substitutions) to slices of the
**original** source text - tokens are never re-serialized, so the user's
formatting, casing and inline comments survive after input decoding.

SQLX-specific safety rules live here:

* Any literal ``${`` in a SQL string or quoted identifier is emitted through
  a constant JavaScript placeholder. Dataform does not provide a backslash
  escape for SQL placeholders, so ``\\${`` is insufficient. Replacement text
  inserted *by* sql2sqlx (``${ref(...)}``, ``${self()}``) stays active.
* GoogleSQL comments with SQLX-specific lexical hazards (``#`` comments and
  exact ``---`` separator-looking comments) are normalized without changing
  BigQuery semantics.
* Operations bodies have exactly one trailing semicolon stripped:
  Dataform executes the body as a BigQuery script, and a trailing empty
  statement is at best noise.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sql2sqlx.lexer import BACKTICK, COMMENT, PARAM, STRING, Token
from sql2sqlx.model import TableName

#: Canonical top-level config key order (unknown keys follow, insertion-ordered).
_KEY_ORDER = (
    "type",
    "database",
    "schema",
    "name",
    "materialized",
    "protected",
    "hasOutput",
    "uniqueKey",
    "description",
    "columns",
    "dependencies",
    "tags",
    "bigquery",
    "disabled",
)

#: Canonical key order inside the ``bigquery`` block.
_BQ_ORDER = (
    "partitionBy",
    "clusterBy",
    "updatePartitionFilter",
    "labels",
    "partitionExpirationDays",
    "requirePartitionFilter",
    "additionalOptions",
)

_JS_IDENT_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_SEPARATOR_COMMENT_RE = re.compile(r"^---[^\S\r\n]*$")
#: Control characters that JSON encoding turns into ``\n``/``\t``/``\uXXXX``
#: escapes. Dataform's placeholder-string lexer accepts only ``\"`` and ``\\``
#: escapes (``/"(?:\\["\\]|[^\n"\\])*"/``), so a whole-string placeholder that
#: embeds one of these characters cannot be lexed by Dataform.
_JS_STRING_UNSAFE_RE = re.compile(r"[\x00-\x1f]")


def _json(value: str) -> str:
    """JSON-encode a string for embedding in JS (non-ASCII preserved)."""
    return json.dumps(value, ensure_ascii=False)


def sqlx_constant(value: str) -> str:
    """Return a SQLX placeholder that evaluates to the constant ``value``."""
    return "${" + _json(value) + "}"


def normalize_sqlx_comment(comment: str) -> str:
    """Return a SQLX-safe spelling of a single GoogleSQL comment token.

    Dataform SQLX treats ``---`` at the start of a line as a statement
    separator and does not recognize GoogleSQL ``#`` line comments.  The
    rewrites here preserve BigQuery semantics while preventing comment text
    from being parsed as SQLX syntax.
    """
    if comment.startswith("#"):
        # Rewriting ``#`` to ``--`` can itself produce a ``---`` separator
        # line (e.g. the comment ``#-``), so fall through to the separator
        # guard below instead of returning early.
        comment = "--" + comment[1:]
    if _SEPARATOR_COMMENT_RE.match(comment):
        return "-- " + comment
    return comment


def sqlx_escape_edits(tokens: Sequence[Token]) -> List[Tuple[int, int, str]]:
    """Build edits that keep literal SQL safe when parsed as SQLX.

    Literal ``${`` in SQL strings, quoted identifiers, and quoted parameters
    is emitted through a constant JavaScript placeholder because Dataform has
    no SQL-level escape for placeholders.  String literals are replaced as
    whole tokens so safety does not depend on Dataform's per-quote lexer
    states.  A string that carries a control character (for example a
    multi-line triple-quoted literal) cannot be embedded whole - JSON encoding
    would introduce a ``\\n``/``\\t`` escape that Dataform's placeholder-string
    lexer rejects - so each ``${`` in such a string is escaped in place, where
    the surrounding SQL string state carries the raw characters unchanged.

    A ``COMMENT`` token is normalized in place, but only a caller that lexed
    with ``keep_comments=True`` ever supplies one: the conversion pipeline
    feeds this function the splitter's significant-token stream, which
    carries no comments, and normalizes a body's comments separately when it
    assembles that body's span edits. Do not remove that second pass on the
    assumption this one covers it.
    """
    edits: List[Tuple[int, int, str]] = []
    for token in tokens:
        if token.kind == COMMENT:
            normalized = normalize_sqlx_comment(token.text)
            if normalized != token.text:
                edits.append((token.start, token.end, normalized))
            continue
        if "${" not in token.text:
            continue
        if token.kind == STRING and _JS_STRING_UNSAFE_RE.search(token.text):
            offset = 0
            while True:
                offset = token.text.find("${", offset)
                if offset < 0:
                    break
                start = token.start + offset
                edits.append((start, start + 2, sqlx_constant("${")))
                offset += 2
            continue
        if token.kind in {BACKTICK, STRING} or (
            token.kind == PARAM and token.text.startswith("@`")
        ):
            edits.append((token.start, token.end, sqlx_constant(token.text)))
    return edits


def _js_key(key: str) -> str:
    """Render an object key: bare when a valid JS identifier, else quoted."""
    return key if _JS_IDENT_RE.match(key) else _json(key)


def _js_value(value: Any, indent: int) -> str:
    """Render a Python value as JavaScript object-literal source.

    Args:
        value: ``str``/``bool``/``int``/``float``/``list``/``dict`` (nested).
        indent: Current indentation level (2 spaces per level).

    Returns:
        JS source text for the value.
    """
    pad = "  " * indent
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, str):
        return _json(value)
    if isinstance(value, (list, tuple)):
        inner = [_js_value(v, indent + 1) for v in value]
        one_line = "[" + ", ".join(inner) + "]"
        if len(one_line) <= 72 and "\n" not in one_line:
            return one_line
        ip = "  " * (indent + 1)
        return "[\n" + ",\n".join(ip + v for v in inner) + "\n" + pad + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        ip = "  " * (indent + 1)
        lines = [f"{ip}{_js_key(k)}: {_js_value(v, indent + 1)}" for k, v in value.items()]
        return "{\n" + ",\n".join(lines) + "\n" + pad + "}"
    return _json(str(value))  # pragma: no cover - defensive


def render_config(config: Dict[str, Any]) -> str:
    """Render a Dataform ``config { ... }`` block.

    Keys are emitted in canonical order (:data:`_KEY_ORDER`, then any
    remaining keys in insertion order); the nested ``bigquery`` object is
    ordered by :data:`_BQ_ORDER`. Ordering is purely cosmetic but makes
    output deterministic and diff-friendly.

    Args:
        config: The config mapping.

    Returns:
        The complete ``config { ... }`` block, without a trailing newline.
    """
    ordered: Dict[str, Any] = {}
    for key in _KEY_ORDER:
        if key in config:
            ordered[key] = config[key]
    for key, val in config.items():
        if key not in ordered:
            ordered[key] = val
    bq = ordered.get("bigquery")
    if isinstance(bq, dict):
        obq: Dict[str, Any] = {k: bq[k] for k in _BQ_ORDER if k in bq}
        for k, v in bq.items():
            if k not in obq:
                obq[k] = v
        ordered["bigquery"] = obq
    lines = [f"  {_js_key(k)}: {_js_value(v, 1)}" for k, v in ordered.items()]
    return "config {\n" + ",\n".join(lines) + "\n}"


def apply_edits_escaped(
    text: str, start: int, end: int, edits: Iterable[Tuple[int, int, str]]
) -> str:
    """Slice ``text[start:end]`` and apply non-overlapping span edits.

    Edits use *absolute* offsets into ``text``. Edits outside the slice or
    overlapping an already-applied edit are skipped defensively. Literal SQLX
    interpolation is handled by :func:`sqlx_escape_edits`; replacements such
    as ``ref()`` and ``self()`` are inserted verbatim and remain active.

    Args:
        text: Full original source text.
        start: Slice start offset.
        end: Slice end offset.
        edits: ``(start, end, replacement)`` triples, any order.

    Returns:
        The edited, escaped slice.
    """
    segments: List[str] = []
    pos = start
    for a, b, replacement in sorted(edits, key=lambda e: (e[0], e[1])):
        if a < pos or a < start or b > end or b < a:
            continue
        segments.append(text[pos:a])
        segments.append(replacement)
        pos = b
    segments.append(text[pos:end])
    return "".join(segments)


def ref_expr(name: TableName) -> str:
    """Build the ``${ref(...)}`` expression for a produced table.

    The reference carries exactly the qualification the *producer* was
    declared with: ``${ref("name")}``, ``${ref("schema", "name")}``, or
    the object form ``${ref({database: ..., schema: ..., name: ...})}``
    when a project was explicit.

    Args:
        name: The producer's original (pre-resolution) table name.

    Returns:
        The interpolation expression text.
    """
    if name.project:
        return "${ref({database: %s, schema: %s, name: %s})}" % (
            _json(name.project),
            _json(name.dataset or ""),
            _json(name.table),
        )
    if name.dataset:
        return "${ref(%s, %s)}" % (_json(name.dataset), _json(name.table))
    return "${ref(%s)}" % _json(name.table)


def build_sqlx(
    config: Dict[str, Any],
    body: Optional[str] = None,
    annotation: Optional[str] = None,
    leading_comments: Optional[str] = None,
    trailing_comments: Optional[str] = None,
) -> str:
    """Assemble a complete ``.sqlx`` file.

    Layout: ``config`` block, blank line, then (in order) the provenance
    annotation comment, the statement's original leading comments, and the
    SQL body. One trailing semicolon is stripped from the body. When the
    body is ``None`` (declarations) only the config block is emitted.

    Args:
        config: The config mapping (see :func:`render_config`).
        body: Edited SQL body, or ``None``.
        annotation: Provenance comment line, or ``None``.
        leading_comments: Comments that preceded the statement, or ``None``.
        trailing_comments: Final file comments after the statement's
            terminating semicolon, or ``None``.

    Returns:
        The full file contents, newline-terminated.
    """
    head = render_config(config)
    bits: List[str] = []
    if annotation:
        bits.append(annotation)
    if leading_comments and leading_comments.strip():
        bits.append(leading_comments.strip())
    if body is not None:
        cleaned = body.strip()
        if cleaned.endswith(";"):
            cleaned = cleaned[:-1].rstrip()
        if cleaned:
            bits.append(cleaned)
    if trailing_comments and trailing_comments.strip():
        bits.append(trailing_comments.strip())
    if bits:
        return head + "\n\n" + "\n".join(bits) + "\n"
    return head + "\n"
