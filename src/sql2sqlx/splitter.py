# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""Split a token stream into top-level GoogleSQL statements.

Splitting SQL on semicolons is only correct if three things are tracked:

1. **Strings and comments** - handled upstream: the splitter operates on
   the significant token stream from :mod:`sql2sqlx.lexer`, so a ``;``
   inside ``'a;b'`` or ``/* ; */`` simply never appears here.
2. **Parenthesis depth** - a ``;`` can only end a statement at paren
   depth 0 (defensive; valid GoogleSQL has no parenthesized semicolons).
3. **Procedural blocks** - BigQuery scripting statements contain inner
   semicolons that must *not* split::

       BEGIN ... END;   IF c THEN ...; END IF;   LOOP ...; END LOOP;
       WHILE c DO ...; END WHILE;   REPEAT ...; UNTIL c END REPEAT;
       FOR x IN (q) DO ...; END FOR;   CASE WHEN c THEN ...; END CASE;

The block tracker maintains a frame stack. Openers are recognized in
*statement position* (start of input, after ``;``, after ``BEGIN``,
``LOOP``, ``REPEAT``, or after the ``THEN``/``ELSE``/``DO`` of a
*scripting* frame), after a block label, or at the body of a
``CREATE PROCEDURE``. Statement position distinguishes the scripting
``IF c THEN`` statement from the ``IF(a, b, c)`` expression function and
the ``LOOP``/``WHILE``/``FOR`` statements from the same words used as
(quoted-elsewhere) identifiers. ``CASE`` opens a frame in *any* position
because both the expression form (``CASE ... END``) and the scripting
form (``CASE ... END CASE``) are symmetric push/pop pairs; the two are
told apart by whether the ``CASE`` itself sits in statement position.
Inside an *expression* ``CASE``, ``THEN``/``ELSE`` are followed by
expressions, so they must not grant statement position - otherwise a
column named ``loop`` or ``begin`` right after them would open a bogus
frame, desynchronize ``END`` matching and glue unrelated statements
together. Likewise ``ELSEIF`` is followed by a *condition*; only its own
``THEN`` starts the branch statements.

For the one genuinely ambiguous opener - ``IF`` in statement position can
still be the expression function when it follows ``THEN``/``ELSE`` of a
*CASE expression* - a bounded, nesting-aware lookahead searches for the
scripting condition's top-level ``THEN`` without confusing a nested CASE.

``BEGIN`` immediately followed by ``;`` or ``TRANSACTION`` is the
transaction-control statement, not a block.

``END`` closers are matched leniently (an ``END X`` pops the innermost
frame even on a type mismatch) because the splitter's only job is to find
top-level semicolons; leniency degrades gracefully into "statements kept
together", which downstream classification handles safely as an
``operations`` action, whereas strictness would reject convertible files.
"""

from __future__ import annotations

from typing import List, Optional, Set

from sql2sqlx.lexer import BACKTICK, EOF, IDENT, OP, Token


class RawStatement:
    """One top-level statement as a slice of the token stream.

    Attributes:
        tokens: The significant tokens of the statement, **excluding** the
            terminating semicolon.
        start: Character offset of the first token.
        end: Character offset one past the last token (semicolon excluded).
        terminator_end: Offset immediately after the terminating semicolon,
            or ``end`` when the final statement was unterminated.
        terminated: Whether the statement was followed by ``;`` in source.
    """

    __slots__ = ("tokens", "start", "end", "terminator_end", "terminated")

    def __init__(
        self,
        tokens: List[Token],
        terminated: bool,
        end: Optional[int] = None,
        terminator_end: Optional[int] = None,
    ) -> None:
        """Build a statement from a non-empty token slice.

        Args:
            tokens: Significant tokens (no semicolon terminator).
            terminated: True if a ``;`` followed in the source.
            end: End of the statement body, excluding the semicolon but
                including any whitespace/comments immediately before it.
            terminator_end: End of the semicolon when present.
        """
        self.tokens = tokens
        self.start = tokens[0].start
        self.end = tokens[-1].end if end is None else end
        self.terminator_end = self.end if terminator_end is None else terminator_end
        self.terminated = terminated


def _if_is_scripting(tokens: List[Token], i: int, limit: int = 4096) -> bool:
    """Decide whether ``tokens[i]`` (an ``IF``) starts a scripting IF.

    Grammar facts used:

    * The expression form is ``IF(<expr>, <expr>, <expr>)`` - the token
      after ``IF`` is **always** ``(`` and the token after the balanced
      group is **never** ``THEN``.
    * The scripting form is ``IF <condition> THEN``. Conditions may begin
      with a parenthesized term and continue (for example ``IF (a) AND b
      THEN``), so the scanner searches for a top-level ``THEN`` while
      tracking parentheses and nested CASE expressions.

    Args:
        tokens: Full token list.
        i: Index of the ``IF`` token.
        limit: Maximum lookahead in tokens (defensive bound).

    Returns:
        True if this ``IF`` opens a scripting block frame.
    """
    j = i + 1
    if j >= len(tokens) or tokens[j].kind == EOF:
        return False
    if not (tokens[j].kind == OP and tokens[j].text == "("):
        # `IF NOT EXISTS` never reaches here (handled inside CREATE/DROP,
        # which are not statement-position IF); `IF cond THEN` it is.
        return True
    depth = 0
    case_depth = 0
    end = min(len(tokens), j + limit)
    while j < end:
        t = tokens[j]
        if t.kind == EOF:
            return False
        if t.kind == OP:
            if t.text == "(":
                depth += 1
            elif t.text == ")":
                depth = max(0, depth - 1)
            elif t.text == ";" and depth == 0:
                return False
        elif t.kind == IDENT and depth == 0:
            up = t.upper
            if up == "CASE":
                case_depth += 1
            elif up == "END" and case_depth:
                case_depth -= 1
            elif up == "THEN" and case_depth == 0:
                return True
            elif up in ("ELSE", "ELSEIF") and case_depth == 0:
                return False
        j += 1
    # A very large/unfinished condition is kept together rather than split
    # into independently executable actions.
    return True


# Two-token END closers.
_END_CLOSERS = frozenset({"IF", "LOOP", "WHILE", "REPEAT", "FOR", "CASE"})
# Statement-position block openers besides BEGIN/IF/CASE.
_SIMPLE_OPENERS = frozenset({"LOOP", "WHILE", "REPEAT", "FOR"})
# Openers whose statement body starts immediately after the keyword
# (WHILE/FOR bodies start at DO, IF/CASE bodies at THEN/ELSE).
_IMMEDIATE_BODY_OPENERS = frozenset({"LOOP", "REPEAT"})
# Frame marker for a CASE *expression* (``CASE ... END``); its THEN/ELSE
# introduce expressions, never statements.
_CASE_EXPR = "CASE_EXPR"


def _starts_create_procedure(tokens: List[Token]) -> bool:
    """Whether the current statement prefix is ``CREATE ... PROCEDURE``."""
    words = [token.upper for token in tokens if token.kind == IDENT]
    if not words or words[0] != "CREATE":
        return False
    i = 1
    if words[i : i + 2] == ["OR", "REPLACE"]:
        i += 2
    return i < len(words) and words[i] == "PROCEDURE"


def split_statements(
    tokens: List[Token],
    statement_starts_out: Optional[Set[int]] = None,
) -> List[RawStatement]:
    """Split a significant-token stream into top-level statements.

    Args:
        tokens: Token stream from :func:`sql2sqlx.lexer.tokenize`
            (must end with the synthetic :data:`~sql2sqlx.lexer.EOF`).
        statement_starts_out: Optional set that receives the source offset
            of every top-level or nested statement head.  The offsets are
            derived from the same block-aware state machine used for
            splitting, so a keyword after a scripting ``THEN`` is included
            while the same keyword after a ``CASE`` *expression* is not.

    Returns:
        List of :class:`RawStatement`, in source order. Empty statements
        (e.g. from ``;;`` or a trailing ``;``) are omitted.

    Example:
        >>> from sql2sqlx.lexer import tokenize
        >>> stmts = split_statements(tokenize(
        ...     "CREATE TABLE d.a AS SELECT 1; BEGIN DELETE d.a WHERE true; END;"))
        >>> len(stmts)
        2
    """
    statements: List[RawStatement] = []
    current: List[Token] = []
    frames: List[str] = []  # open block frames, innermost last
    paren_depth = 0
    stmt_position = True  # next token may begin a (sub)statement

    def flush(
        terminated: bool, end: Optional[int] = None, terminator_end: Optional[int] = None
    ) -> None:
        if current:
            statements.append(RawStatement(list(current), terminated, end, terminator_end))
            current.clear()

    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok.kind == EOF:
            break
        if stmt_position and statement_starts_out is not None:
            statement_starts_out.add(tok.start)
        kind = tok.kind
        if (
            stmt_position
            and kind in (IDENT, BACKTICK)
            and i + 2 < n
            and tokens[i + 1].kind == OP
            and tokens[i + 1].text == ":"
            and tokens[i + 2].kind == IDENT
            and tokens[i + 2].upper in (_SIMPLE_OPENERS | {"BEGIN"})
        ):
            # GoogleSQL labels belong to the following block/loop and do not
            # consume statement position: ``label: BEGIN ... END label``.
            current.append(tok)
            current.append(tokens[i + 1])
            i += 2
            continue
        if kind == OP:
            text = tok.text
            if text == ";":
                if paren_depth == 0 and not frames:
                    flush(True, tok.start, tok.end)
                    stmt_position = True
                    i += 1
                    continue
                current.append(tok)
                stmt_position = True  # statement boundary inside a block
                i += 1
                continue
            if text == "(":
                paren_depth += 1
            elif text == ")":
                paren_depth = max(0, paren_depth - 1)
            current.append(tok)
            stmt_position = False
            i += 1
            continue

        if kind == IDENT:
            up = tok.upper
            if up == "END":
                nxt: Optional[Token] = tokens[i + 1] if i + 1 < n else None
                if (
                    nxt is not None
                    and nxt.kind == IDENT
                    and nxt.upper in _END_CLOSERS
                    and frames
                    and frames[-1] == nxt.upper
                ):
                    frames.pop()
                    current.append(tok)
                    current.append(nxt)
                    stmt_position = False
                    i += 2
                    continue
                if frames:
                    # Plain END (or lenient mismatch): pop innermost.
                    frames.pop()
                current.append(tok)
                stmt_position = False
                i += 1
                continue
            if up == "CASE":
                # A CASE beginning a (sub)statement is the scripting form
                # (``CASE ... END CASE``); anywhere else it is an expression.
                frames.append("CASE" if stmt_position else _CASE_EXPR)
                current.append(tok)
                stmt_position = False
                i += 1
                continue
            if stmt_position:
                if up == "BEGIN":
                    nxt = tokens[i + 1] if i + 1 < n else None
                    is_txn = (
                        nxt is None
                        or nxt.kind == EOF
                        or (nxt.kind == OP and nxt.text == ";")
                        or (nxt.kind == IDENT and nxt.upper == "TRANSACTION")
                    )
                    if not is_txn:
                        frames.append("BEGIN")
                        current.append(tok)
                        stmt_position = True  # first inner statement follows
                        i += 1
                        continue
                elif up == "IF" and _if_is_scripting(tokens, i):
                    frames.append("IF")
                    current.append(tok)
                    stmt_position = False
                    i += 1
                    continue
                elif up in _SIMPLE_OPENERS:
                    frames.append(up)
                    current.append(tok)
                    # LOOP and REPEAT bodies start immediately after the
                    # keyword; WHILE/FOR bodies start at their DO.
                    stmt_position = up in _IMMEDIATE_BODY_OPENERS
                    i += 1
                    continue
            elif up == "BEGIN" and not frames and _starts_create_procedure(current):
                # A SQL stored-procedure body begins after its signature, so
                # BEGIN is not in ordinary statement position.
                frames.append("BEGIN")
                current.append(tok)
                stmt_position = True
                i += 1
                continue
            if up in ("THEN", "ELSE", "DO"):
                # Statements follow only inside a scripting frame (IF,
                # scripting CASE, WHILE/FOR, BEGIN exception handlers). In
                # a CASE *expression* - or with no frame open at all, as in
                # ``MERGE ... WHEN MATCHED THEN UPDATE`` - an expression or
                # clause keyword follows instead, and granting statement
                # position would let a column named ``loop``/``begin`` open
                # a bogus block frame. ``ELSEIF`` is followed by a
                # condition, so it grants nothing; its own THEN does.
                current.append(tok)
                stmt_position = bool(frames) and frames[-1] != _CASE_EXPR
                i += 1
                continue
            current.append(tok)
            stmt_position = False
            i += 1
            continue

        # STRING / NUMBER / PARAM / BACKTICK
        current.append(tok)
        stmt_position = False
        i += 1

    eof_pos = tokens[-1].start if tokens and tokens[-1].kind == EOF else None
    flush(False, eof_pos, eof_pos)
    return statements
