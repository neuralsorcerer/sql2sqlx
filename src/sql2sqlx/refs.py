# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""Table-path parsing and reference-site discovery.

The reference scanner finds every position in a statement where a *table*
is being read - the places that can safely be rewritten to Dataform
``${ref(...)}`` calls. Doing this on raw text would be hopeless; doing it
on the token stream with a small amount of context tracking is exact for
the constructs that matter and *conservative* everywhere else (when in
doubt, a reference is left untouched, which always yields valid SQLX).

What is captured
----------------
A candidate reference site is a table path appearing:

* after ``FROM`` (except a ``FROM`` inside ``EXTRACT( ... )``);
* after any ``JOIN``;
* after a ``,`` inside an active ``FROM`` table list (comma joins);
* after ``USING`` of a top-level ``MERGE`` statement.

What is excluded
----------------
* ``UNNEST(...)`` and table-valued function calls (``name(`` directly
  after the table position);
* subqueries and parenthesized join trees (recursed into instead);
* references to a CTE in the CTE's actual visibility range;
* paths whose first segment is a visible range variable (FROM aliases are
  tracked per query block, including correlated scalar subqueries, without
  leaking aliases out of nested or set-operation query blocks);
* paths inside caller-supplied *skip spans* (e.g. the target of
  ``DELETE FROM target``);
* paths longer than three segments (correlated array references such as
  ``FROM t, t.array_col`` at four+ parts).

Path syntax handled
-------------------
``dataset.table``, ``project.dataset.table``, any mix of backticked
segments (`` `p.d.t` ``, `` `p`.`d`.`t` ``, `` `p.d`.t ``), whitespace
around dots, and BigQuery *dashed* project names (``my-project-123.d.t``)
- dash joining requires character adjacency, exactly like BigQuery's own
lexer, so ``a - b`` in an expression is never mistaken for a path.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Set, Tuple

from sql2sqlx.keywords import FROM_CLAUSE_ENDERS, RESERVED
from sql2sqlx.lexer import BACKTICK, EOF, IDENT, NUMBER, OP, Token, unquote_identifier
from sql2sqlx.model import RefSite, TableName


class PathMatch:
    """A parsed table path within the token stream.

    Attributes:
        parts: Decoded path segments (backticks removed, escapes decoded,
            dotted backtick contents split).
        start: Character offset of the first path character.
        end: Character offset one past the last path character.
        next_index: Token index immediately after the path.
    """

    __slots__ = ("parts", "start", "end", "next_index")

    def __init__(self, parts: List[str], start: int, end: int, next_index: int) -> None:
        """Store the parse result; see class docstring for fields."""
        self.parts = parts
        self.start = start
        self.end = end
        self.next_index = next_index


def _read_segment(tokens: Sequence[Token], k: int) -> Optional[Tuple[str, int, int]]:
    """Read one path segment starting at token index ``k``.

    Handles backticked segments and BigQuery dashed identifiers
    (``proj-name-123``), where every ``-`` and its continuation must be
    *character-adjacent* to the preceding token.

    Args:
        tokens: The token stream.
        k: Index of the candidate segment's first token.

    Returns:
        ``(decoded_text, end_offset, next_index)`` or ``None`` if no
        segment starts at ``k``.
    """
    tok = tokens[k]
    if tok.kind == BACKTICK:
        return unquote_identifier(tok.text), tok.end, k + 1
    if tok.kind != IDENT:
        return None
    text = tok.text
    e = tok.end
    k += 1
    n = len(tokens)
    while (
        k + 1 < n
        and tokens[k].kind == OP
        and tokens[k].text == "-"
        and tokens[k].start == e
        and tokens[k + 1].kind in (IDENT, NUMBER)
        and tokens[k + 1].start == tokens[k].end
    ):
        seg = tokens[k + 1].text
        e = tokens[k + 1].end
        was_number = tokens[k + 1].kind == NUMBER
        k += 2
        # `proj-123abc` lexes as NUMBER `123` + adjacent IDENT `abc`.
        if was_number and k < n and tokens[k].kind == IDENT and tokens[k].start == e:
            seg += tokens[k].text
            e = tokens[k].end
            k += 1
        text += "-" + seg
    return text, e, k


def parse_table_path(tokens: Sequence[Token], i: int) -> Optional[PathMatch]:
    """Parse a (possibly qualified) table path at token index ``i``.

    Args:
        tokens: The token stream (must be EOF-terminated).
        i: Index of the first token of the candidate path.

    Returns:
        A :class:`PathMatch`, or ``None`` when ``tokens[i]`` cannot begin
        a path (wrong kind, an unquoted reserved keyword, or an index at
        or past the end of the token list - script statement slices are
        not EOF-terminated, so a truncated ``DELETE``/``RENAME TO``/...
        can legitimately ask for the position after the last token).

    Example:
        >>> from sql2sqlx.lexer import tokenize
        >>> pm = parse_table_path(tokenize("`my-proj`.sales . orders x"), 0)
        >>> pm.parts
        ['my-proj', 'sales', 'orders']
    """
    if not (0 <= i < len(tokens)):
        return None
    first = tokens[i]
    if first.kind == IDENT:
        if first.upper in RESERVED:
            return None
    elif first.kind != BACKTICK:
        return None

    seg = _read_segment(tokens, i)
    if seg is None:
        return None
    text0, end, j = seg
    parts: List[str] = text0.split(".") if first.kind == BACKTICK and "." in text0 else [text0]
    start = first.start
    n = len(tokens)
    while j < n and tokens[j].kind == OP and tokens[j].text == ".":
        if j + 1 >= n:
            break
        was_backtick = tokens[j + 1].kind == BACKTICK
        nxt = _read_segment(tokens, j + 1)
        if nxt is None:
            break
        text_n, end_n, j2 = nxt
        if was_backtick and "." in text_n:
            parts.extend(text_n.split("."))
        else:
            parts.append(text_n)
        end = end_n
        j = j2
    return PathMatch(parts, start, end, j)


#: Unreserved keywords that directly follow a table expression but are
#: never aliases (consuming them as aliases would be harmless for the
#: exclusion set, but skipping their token would desynchronize scanning).
_NON_ALIAS_FOLLOWERS = frozenset({"PIVOT", "UNPIVOT"})


class _QueryScope:
    """Reference-name visibility for one query expression or script unit."""

    __slots__ = (
        "start",
        "end",
        "branches",
        "outer_aliases",
        "aliases",
        "outer_ctes",
        "ctes",
    )

    def __init__(
        self,
        start: int,
        end: int,
        outer_aliases: Set[str],
        outer_ctes: Set[str],
    ) -> None:
        self.start = start
        self.end = end
        self.branches: Optional[List[Tuple[int, int]]] = None
        self.outer_aliases = outer_aliases
        self.aliases: Set[str] = set()
        self.outer_ctes = outer_ctes
        self.ctes: Set[str] = set()


def _paren_pairs(tokens: Sequence[Token]) -> Dict[int, int]:
    """Build opening-to-closing parenthesis indexes in one linear pass."""
    stack: List[int] = []
    pairs: Dict[int, int] = {}
    for i, token in enumerate(tokens):
        if token.kind != OP:
            continue
        if token.text == "(":
            stack.append(i)
        elif token.text == ")" and stack:
            pairs[stack.pop()] = i
    return pairs


def _matching_paren(
    tokens: Sequence[Token],
    opening: int,
    end: int,
    pairs: Optional[Dict[int, int]] = None,
) -> Optional[int]:
    """Return the matching ``)`` token index for ``tokens[opening]``."""
    if pairs is not None:
        closing = pairs.get(opening)
        return closing if closing is not None and closing < end else None
    depth = 0
    for i in range(opening, end):
        token = tokens[i]
        if token.kind != OP:
            continue
        if token.text == "(":
            depth += 1
        elif token.text == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _segment_end(tokens: Sequence[Token], start: int) -> int:
    """Return the exclusive token index of the current script statement."""
    for i in range(start, len(tokens)):
        token = tokens[i]
        if token.kind == EOF or (token.kind == OP and token.text == ";"):
            return i
    return len(tokens)


def _begins_query(tokens: Sequence[Token], opening: int, closing: int) -> bool:
    """Whether a parenthesized expression contains a query expression."""
    i = opening + 1
    while i < closing and tokens[i].kind == OP and tokens[i].text == "(":
        i += 1
    return i < closing and tokens[i].kind == IDENT and tokens[i].upper in ("SELECT", "WITH", "FROM")


def _cte_definitions(
    tokens: Sequence[Token],
    with_index: int,
    end: int,
    paren_pairs: Dict[int, int],
) -> Optional[Tuple[bool, List[Tuple[str, int, int]]]]:
    """Parse the top-level definitions of one ``WITH`` clause.

    Returns ``(recursive, [(name, body_open, body_close), ...])``.  The
    strict shape check prevents unrelated uses of the unreserved word
    ``with`` from changing reference visibility.
    """
    i = with_index + 1
    recursive = False
    if i < end and tokens[i].kind == IDENT and tokens[i].upper == "RECURSIVE":
        recursive = True
        i += 1
    definitions: List[Tuple[str, int, int]] = []
    while i < end:
        token = tokens[i]
        if token.kind == BACKTICK:
            name = unquote_identifier(token.text)
        elif token.kind == IDENT and token.upper not in RESERVED:
            name = token.text
        else:
            return None
        i += 1
        # Accept the standard optional CTE column-name list.
        if i < end and tokens[i].kind == OP and tokens[i].text == "(":
            column_close = _matching_paren(tokens, i, end, paren_pairs)
            if column_close is None:
                return None
            i = column_close + 1
        if not (i < end and tokens[i].kind == IDENT and tokens[i].upper == "AS"):
            return None
        i += 1
        if not (i < end and tokens[i].kind == OP and tokens[i].text == "("):
            return None
        body_open = i
        body_close = _matching_paren(tokens, body_open, end, paren_pairs)
        if body_close is None:
            return None
        definitions.append((name.upper(), body_open, body_close))
        i = body_close + 1
        if not (i < end and tokens[i].kind == OP and tokens[i].text == ","):
            break
        # A comma belongs to the WITH list only when another ``name AS (``
        # definition follows.  Let the next iteration validate that shape.
        i += 1
    return (recursive, definitions) if definitions else None


def _maybe_alias(tokens: Sequence[Token], i: int, excluded: Set[str]) -> int:
    """Consume an optional ``[AS] alias`` at index ``i``.

    The alias (decoded, upper-cased) is added to ``excluded`` so later
    dotted paths starting with it (``alias.column``) are never treated as
    table references.

    Args:
        tokens: The token stream.
        i: Index where an alias may start.
        excluded: Mutable set of excluded first segments.

    Returns:
        The index of the first token after the alias (or ``i`` unchanged
        when no alias is present).
    """
    n = len(tokens)
    if i < n and tokens[i].kind == IDENT and tokens[i].upper == "AS":
        j = i + 1
        if j < n and tokens[j].kind in (IDENT, BACKTICK):
            t = tokens[j]
            if t.kind == BACKTICK or t.upper not in RESERVED:
                excluded.add(unquote_identifier(t.text).upper())
                return j + 1
        return i
    if i < n:
        t = tokens[i]
        if t.kind == BACKTICK:
            excluded.add(unquote_identifier(t.text).upper())
            return i + 1
        if t.kind == IDENT and t.upper not in RESERVED and t.upper not in _NON_ALIAS_FOLLOWERS:
            excluded.add(t.upper)
            return i + 1
    return i


def _set_operator(tokens: Sequence[Token], i: int) -> bool:
    """Whether ``tokens[i]`` is a top-level set operator."""
    token = tokens[i]
    if token.kind != IDENT or token.upper not in ("UNION", "INTERSECT", "EXCEPT"):
        return False
    # ``SELECT * EXCEPT (column)`` is a projection modifier, not a set op.
    return not (
        token.upper == "EXCEPT"
        and i + 1 < len(tokens)
        and tokens[i + 1].kind == OP
        and tokens[i + 1].text == "("
    )


def _branch_ranges(
    tokens: Sequence[Token],
    start: int,
    end: int,
) -> List[Tuple[int, int]]:
    """Split a query expression into its top-level set-operation branches."""
    branches: List[Tuple[int, int]] = []
    branch_start = start
    depth = 0
    bracket_depth = 0
    for i in range(start, end):
        token = tokens[i]
        if token.kind == OP:
            if token.text == "(":
                depth += 1
            elif token.text == ")" and depth:
                depth -= 1
            elif token.text == "[":
                bracket_depth += 1
            elif token.text == "]" and bracket_depth:
                bracket_depth -= 1
            continue
        if depth == 0 and bracket_depth == 0 and _set_operator(tokens, i):
            branches.append((branch_start, i))
            branch_start = i + 1
    branches.append((branch_start, end))
    return branches


def _collect_range_aliases(
    tokens: Sequence[Token],
    start: int,
    end: int,
    *,
    from_active: bool = False,
    cte_names: Optional[Set[str]] = None,
    paren_pairs: Optional[Dict[int, int]] = None,
) -> Set[str]:
    """Collect range variables declared in one query/set-operation block."""
    aliases: Set[str] = set()
    expecting = from_active
    i = start
    while i < end:
        token = tokens[i]
        if token.kind == OP:
            if token.text == "(":
                closing = _matching_paren(tokens, i, end, paren_pairs)
                if closing is None:
                    return aliases
                if expecting:
                    is_query = _begins_query(tokens, i, closing)
                    nested: Set[str] = set()
                    if not is_query:
                        nested = _collect_range_aliases(
                            tokens,
                            i + 1,
                            closing,
                            from_active=True,
                            cte_names=cte_names,
                            paren_pairs=paren_pairs,
                        )
                    alias_end = _maybe_alias(tokens, closing + 1, aliases)
                    if alias_end == closing + 1:
                        aliases.update(nested)
                    i = alias_end
                    expecting = False
                    continue
                i = closing + 1
                continue
            if token.text == "," and from_active:
                expecting = True
            i += 1
            continue
        if token.kind not in (IDENT, BACKTICK):
            i += 1
            continue
        upper = token.upper if token.kind == IDENT else ""
        if token.kind == IDENT and upper == "FROM":
            from_active = True
            expecting = True
            i += 1
            continue
        if token.kind == IDENT and upper == "JOIN":
            from_active = True
            expecting = True
            i += 1
            continue
        if token.kind == IDENT and upper in FROM_CLAUSE_ENDERS:
            from_active = False
            expecting = False
            i += 1
            continue
        if not expecting:
            i += 1
            continue
        if token.kind == IDENT and upper == "LATERAL":
            i += 1
            continue
        if token.kind == IDENT and upper == "UNNEST":
            if i + 1 < end and tokens[i + 1].kind == OP and tokens[i + 1].text == "(":
                closing = _matching_paren(tokens, i + 1, end, paren_pairs)
                if closing is None:
                    return aliases
                alias_end = _maybe_alias(tokens, closing + 1, aliases)
                if alias_end == closing + 1 and i + 2 < closing:
                    path = parse_table_path(tokens, i + 2)
                    if path is not None and path.next_index == closing:
                        aliases.add(path.parts[-1].upper())
                i = alias_end
            else:
                i += 1
            expecting = False
            continue
        if token.kind == IDENT and upper in RESERVED:
            expecting = False
            i += 1
            continue
        path = parse_table_path(tokens, i)
        if path is None:
            expecting = False
            i += 1
            continue
        after = path.next_index
        if after < end and tokens[after].kind == OP and tokens[after].text == "(":
            closing = _matching_paren(tokens, after, end, paren_pairs)
            if closing is None:
                return aliases
            after = closing + 1
        alias_end = _maybe_alias(tokens, after, aliases)
        if alias_end == after and not (
            path.next_index < end
            and tokens[path.next_index].kind == OP
            and tokens[path.next_index].text == "("
        ):
            lexical = unquote_identifier(tokens[i].text).upper()
            is_cte = cte_names is not None and path.next_index == i + 1 and lexical in cte_names
            aliases.add(lexical if is_cte else path.parts[-1].upper())
        i = alias_end
        expecting = False
    return aliases


def _branch_aliases(
    tokens: Sequence[Token],
    scope: _QueryScope,
    position: int,
    cache: Dict[Tuple[int, int, Tuple[str, ...]], Set[str]],
    paren_pairs: Dict[int, int],
) -> Set[str]:
    """Return every range alias in ``scope``'s branch at ``position``."""
    if scope.branches is None:
        scope.branches = _branch_ranges(tokens, scope.start, scope.end)
    start, end = scope.start, scope.end
    for branch_start, branch_end in scope.branches:
        if branch_start <= position < branch_end:
            start, end = branch_start, branch_end
            break
    cte_names = scope.outer_ctes | scope.ctes
    key = (start, end, tuple(sorted(cte_names)))
    if key not in cache:
        cache[key] = _collect_range_aliases(
            tokens,
            start,
            end,
            cte_names=cte_names,
            paren_pairs=paren_pairs,
        )
    return cache[key]


def _exclude_write_target_alias(
    tokens: Sequence[Token],
    i: int,
    excluded: Set[str],
) -> None:
    """Register the range variable of an UPDATE/DELETE/MERGE target.

    Unlike a ``DELETE FROM`` target, UPDATE and MERGE targets do not pass
    through the ordinary FROM-item scanner.  Their explicit (or implicit)
    aliases are nevertheless visible later in the statement and must shield
    correlated paths such as ``target_alias.repeated_field`` from physical
    table-reference rewriting.
    """
    head = tokens[i].upper
    j = i + 1
    if (
        head == "MERGE"
        and j < len(tokens)
        and (tokens[j].kind == IDENT and tokens[j].upper == "INTO")
    ):
        j += 1
    elif (
        head == "DELETE"
        and j < len(tokens)
        and (tokens[j].kind == IDENT and tokens[j].upper == "FROM")
    ):
        j += 1
    match = parse_table_path(tokens, j)
    if match is None or not match.parts:
        return
    j = match.next_index
    if j < len(tokens) and tokens[j].kind == IDENT and tokens[j].upper == "AS":
        j += 1
        if j < len(tokens) and tokens[j].kind in (IDENT, BACKTICK):
            alias = tokens[j]
            if alias.kind == BACKTICK or alias.upper not in RESERVED:
                excluded.add(unquote_identifier(alias.text).upper())
                return
    elif j < len(tokens):
        alias = tokens[j]
        if alias.kind == BACKTICK or (alias.kind == IDENT and alias.upper not in RESERVED):
            excluded.add(unquote_identifier(alias.text).upper())
            return
    excluded.add(match.parts[-1].upper())


def scan_ref_sites(
    tokens: Sequence[Token],
    stmt_kind: str,
    skip_spans: Sequence[Tuple[int, int]] = (),
    statement_start_offsets: Optional[Set[int]] = None,
) -> List[RefSite]:
    """Scan one statement's tokens for rewritable table references.

    Args:
        tokens: Significant tokens of the statement (EOF terminator is
            tolerated but not required).
        stmt_kind: Uppercase leading keyword of the statement (``"MERGE"``
            enables ``USING`` as a table introducer at depth 0).
        skip_spans: Character spans (absolute offsets) whose contained
            paths must not be reported - typically the statement's own
            write target.
        statement_start_offsets: Optional block-aware source offsets for
            nested statement heads.  Supplying these lets a whole-script
            scan recognize nested ``MERGE ... USING`` statements without
            treating an arbitrary ``MERGE`` token as a statement.

    Returns:
        Filtered list of :class:`~sql2sqlx.model.RefSite` in source order.
        CTE references and paths rooted at a visible range variable are
        removed.
    """
    resolved: List[RefSite] = []
    n = len(tokens)
    paren_pairs = _paren_pairs(tokens)
    scopes: List[_QueryScope] = [_QueryScope(0, _segment_end(tokens, 0), set(), set())]
    # close-index -> owning scope/name for non-recursive CTE visibility.
    cte_close_events: Dict[int, List[Tuple[_QueryScope, str]]] = {}
    cte_body_opens: Set[int] = set()
    alias_cache: Dict[Tuple[int, int, Tuple[str, ...]], Set[str]] = {}
    implicit_alias_at_close: Dict[int, str] = {}
    # One context per open paren/bracket (plus root):
    # [from_active, is_extract, opened_query_scope].
    ctx: List[List[bool]] = [[False, False, False]]
    expecting = False
    prev_upper = ""
    merge_active = stmt_kind == "MERGE"
    i = 0
    while i < n:
        tok = tokens[i]
        kind = tok.kind
        if kind == OP:
            t = tok.text
            if t == ";":
                # A procedural draft can contain several inner statements.
                # FROM state and alias/CTE exclusions are statement-scoped;
                # carrying either across `;` can invent references or hide
                # real ones in the next statement.
                scopes = [_QueryScope(i + 1, _segment_end(tokens, i + 1), set(), set())]
                ctx = [[False, False, False]]
                merge_active = False
                expecting = False
                prev_upper = ""
                i += 1
                continue
            if t == "(":
                closing = _matching_paren(tokens, i, n, paren_pairs)
                begins_query = closing is not None and (
                    _begins_query(tokens, i, closing) or i in cte_body_opens
                )
                if begins_query and closing is not None:
                    parent = scopes[-1]
                    outer_ctes = parent.outer_ctes | parent.ctes
                    outer_aliases: Set[str]
                    if i in cte_body_opens:
                        # BigQuery explicitly disallows a CTE body from
                        # referencing correlated columns in an outer query.
                        outer_aliases = set()
                    elif expecting:
                        # BigQuery FROM subqueries are not lateral.  UNNEST
                        # and TVFs are handled separately and can consume
                        # preceding range variables in their arguments.
                        outer_aliases = set()
                    else:
                        outer_aliases = (
                            parent.outer_aliases
                            | parent.aliases
                            | _branch_aliases(
                                tokens,
                                parent,
                                i,
                                alias_cache,
                                paren_pairs,
                            )
                        )
                    scopes.append(
                        _QueryScope(
                            i + 1,
                            closing,
                            set(outer_aliases),
                            set(outer_ctes),
                        )
                    )
                ctx.append(
                    [
                        expecting and not begins_query,
                        prev_upper == "EXTRACT",
                        begins_query,
                    ]
                )
                if begins_query:
                    expecting = False
                prev_upper = ""
                i += 1
                continue
            if t == "[":
                # Array literals/subscripts can contain commas but never a
                # table list directly.
                ctx.append([False, False, False])
                expecting = False
                prev_upper = ""
                i += 1
                continue
            if t == ")":
                if len(ctx) > 1:
                    closed = ctx.pop()
                    if closed[2] and len(scopes) > 1:
                        scopes.pop()
                for owner, name in cte_close_events.get(i, []):
                    owner.ctes.add(name)
                if ctx[-1][0]:
                    alias_end = _maybe_alias(
                        tokens,
                        i + 1,
                        scopes[-1].aliases,
                    )
                    if alias_end == i + 1 and i in implicit_alias_at_close:
                        scopes[-1].aliases.add(implicit_alias_at_close[i])
                    i = alias_end
                else:
                    i += 1
                expecting = False
                prev_upper = ""
                continue
            if t == "]":
                if len(ctx) > 1:
                    ctx.pop()
                expecting = False
                prev_upper = ""
                i += 1
                continue
            if t == "|>":
                # The preceding FROM table list ends at a pipe operator.
                # A later pipe JOIN will explicitly reactivate table mode.
                ctx[-1][0] = False
                expecting = False
                prev_upper = ""
                i += 1
                continue
            if t == "," and ctx[-1][0]:
                expecting = True
                prev_upper = ""
                i += 1
                continue
            expecting = False
            prev_upper = ""
            i += 1
            continue

        if kind == IDENT or kind == BACKTICK:
            up = tok.upper if kind == IDENT else ""
            if kind == IDENT:
                is_statement_head = (
                    tok.start in statement_start_offsets
                    if statement_start_offsets is not None
                    else i == 0
                )
                if is_statement_head and up in ("UPDATE", "DELETE", "MERGE"):
                    _exclude_write_target_alias(tokens, i, scopes[-1].aliases)
                if (
                    up == "MERGE"
                    and statement_start_offsets is not None
                    and tok.start in statement_start_offsets
                ):
                    merge_active = True
                if up == "WITH":
                    definitions = _cte_definitions(
                        tokens,
                        i,
                        scopes[-1].end,
                        paren_pairs,
                    )
                    if definitions is not None:
                        recursive, ctes = definitions
                        cte_body_opens.update(opening for _, opening, _ in ctes)
                        if recursive:
                            scopes[-1].ctes.update(name for name, _, _ in ctes)
                        else:
                            owner = scopes[-1]
                            for name, _, closing in ctes:
                                cte_close_events.setdefault(closing, []).append((owner, name))
                if up == "FROM":
                    if ctx[-1][1]:  # EXTRACT(... FROM ...)
                        expecting = False
                    else:
                        ctx[-1][0] = True
                        expecting = True
                    prev_upper = up
                    i += 1
                    continue
                if up == "JOIN":
                    ctx[-1][0] = True
                    expecting = True
                    prev_upper = up
                    i += 1
                    continue
                if up == "USING" and merge_active and len(ctx) == 1:
                    # Only the MERGE statement's first USING introduces its
                    # source.  A later JOIN ... USING(column_list) is a join
                    # condition, not another table position.
                    merge_active = False
                    expecting = True
                    prev_upper = up
                    i += 1
                    continue
                if _set_operator(tokens, i):
                    scopes[-1].aliases.clear()
                if up in FROM_CLAUSE_ENDERS:
                    ctx[-1][0] = False
                    expecting = False
                    prev_upper = up
                    i += 1
                    continue
            if expecting:
                if kind == IDENT:
                    if up == "LATERAL":
                        prev_upper = up
                        i += 1
                        continue
                    if (
                        up == "UNNEST"
                        and i + 1 < n
                        and (tokens[i + 1].kind == OP and tokens[i + 1].text == "(")
                    ):
                        closing = _matching_paren(tokens, i + 1, n, paren_pairs)
                        if closing is None:
                            expecting = False
                            i += 1
                            continue
                        if i + 2 < closing:
                            path = parse_table_path(tokens, i + 2)
                            if path is not None and path.next_index == closing:
                                implicit_alias_at_close[closing] = path.parts[-1].upper()
                        expecting = False
                        prev_upper = ""
                        # Continue through the arguments: ARRAY/scalar
                        # subqueries inside UNNEST can contain physical table
                        # reads and can correlate to the enclosing query.
                        i += 1
                        continue
                    if up in RESERVED:  # UNNEST, SELECT, ...
                        expecting = False
                        prev_upper = up
                        i += 1
                        continue
                pm = parse_table_path(tokens, i)
                if pm is None:
                    expecting = False
                    prev_upper = up
                    i += 1
                    continue
                expecting = False
                nxt = tokens[pm.next_index] if pm.next_index < n else None
                if nxt is not None and nxt.kind == OP and nxt.text == "(":
                    # table-valued function call - not a plain table
                    # Scan its arguments because they may contain query
                    # expressions with ordinary physical table reads.
                    prev_upper = ""
                    i = pm.next_index
                    continue
                in_skip = any(pm.start >= a and pm.end <= b for a, b in skip_spans)
                lexical_root = unquote_identifier(tokens[i].text).upper()
                roots = {pm.parts[0].upper(), lexical_root}
                scope = scopes[-1]
                alias_names = scope.outer_aliases | scope.aliases
                cte_names = scope.outer_ctes | scope.ctes
                is_cte_ref = pm.next_index == i + 1 and lexical_root in cte_names
                if (
                    not in_skip
                    and 1 <= len(pm.parts) <= 3
                    and all(pm.parts)
                    and roots.isdisjoint(alias_names)
                    and not is_cte_ref
                ):
                    resolved.append(
                        RefSite(
                            pm.start,
                            pm.end,
                            TableName.from_parts(list(pm.parts)),
                        )
                    )
                alias_end = _maybe_alias(
                    tokens,
                    pm.next_index,
                    scopes[-1].aliases,
                )
                if alias_end == pm.next_index:
                    # A plain table path has the implicit alias of its last
                    # identifier.  It is visible to subsequent FROM items,
                    # e.g. ``FROM d.parent, parent.children``.
                    implicit = lexical_root if is_cte_ref else pm.parts[-1].upper()
                    scopes[-1].aliases.add(implicit)
                i = alias_end
                prev_upper = ""
                continue
            prev_upper = up
            i += 1
            continue

        # STRING / NUMBER / PARAM / EOF
        expecting = False
        prev_upper = ""
        i += 1

    return resolved
