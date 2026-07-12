# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""Lexer and statement-splitter tests.

These are the adversarial foundations: semicolons inside strings and
comments, raw-string backslash rules, non-nested block comments, dashed
project identifiers, and every BigQuery scripting construct that could
fool a naive splitter.
"""

from __future__ import annotations

import pytest

from sql2sqlx.errors import LexError
from sql2sqlx.lexer import (
    BACKTICK,
    COMMENT,
    EOF,
    IDENT,
    NUMBER,
    OP,
    PARAM,
    STRING,
    LineIndex,
    comment_spans,
    tokenize,
    unquote_identifier,
)
from sql2sqlx.refs import parse_table_path
from sql2sqlx.splitter import split_statements


def texts(sql):
    """Token texts, EOF excluded."""
    return [t.text for t in tokenize(sql) if t.kind != EOF]


def kinds(sql):
    """Token kinds, EOF excluded."""
    return [t.kind for t in tokenize(sql) if t.kind != EOF]


def split(sql):
    """Convenience: statements of ``sql``."""
    return split_statements(tokenize(sql))


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------


def test_basic_token_kinds():
    sql = "SELECT `a b`, 'x', 1.5e-3, .5, 42, @p, @@proc.var, ? FROM t"
    got = kinds(sql)
    assert got == [
        IDENT,
        BACKTICK,
        OP,
        STRING,
        OP,
        NUMBER,
        OP,
        NUMBER,
        OP,
        NUMBER,
        OP,
        PARAM,
        OP,
        PARAM,
        OP,
        PARAM,
        IDENT,
        IDENT,
    ]


def test_string_escapes_do_not_terminate():
    assert texts(r"SELECT 'it\'s'") == ["SELECT", r"'it\'s'"]
    assert texts(r'SELECT "a\"b"') == ["SELECT", r'"a\"b"']


def test_raw_string_backslash_is_literal():
    # Raw strings preserve backslashes, but GoogleSQL requires an even
    # number immediately before the closing quote.
    got = texts(r"SELECT r'\\' , 1")
    assert got == ["SELECT", r"r'\\'", ",", "1"]


def test_triple_quoted_strings():
    assert texts("SELECT '''a'b''c'''") == ["SELECT", "'''a'b''c'''"]
    sql = 'SELECT """line1\nline2; not a split"""'
    assert texts(sql) == ["SELECT", '"""line1\nline2; not a split"""']
    assert len(split(sql)) == 1


def test_bytes_and_raw_prefixes():
    for lit in ("b'x'", 'B"x"', "rb'x'", "BR'x'", "Rb'''x'''"):
        toks = tokenize(f"SELECT {lit}")
        assert toks[1].kind == STRING and toks[1].text == lit


def test_unterminated_string_location():
    with pytest.raises(LexError) as exc:
        tokenize("SELECT 1;\n\n  'oops")
    assert exc.value.line == 3 and exc.value.column == 3
    assert "string" in str(exc.value).lower()


def test_unterminated_backtick_and_block_comment():
    with pytest.raises(LexError) as exc:
        tokenize("SELECT `broken")
    assert "backtick" in str(exc.value).lower()
    with pytest.raises(LexError) as exc:
        tokenize("SELECT 1 /* never closed")
    assert "comment" in str(exc.value).lower()


def test_comment_forms_and_spans():
    sql = "-- a\n# b\n/* c\nd */ SELECT 1"
    assert texts(sql) == ["SELECT", "1"]
    spans = comment_spans(sql)
    assert [sql[a:b] for a, b in spans] == ["-- a", "# b", "/* c\nd */"]
    kept = [t.kind for t in tokenize(sql, keep_comments=True) if t.kind != EOF]
    assert kept == [COMMENT, COMMENT, COMMENT, IDENT, NUMBER]


def test_block_comments_do_not_nest():
    # Per GoogleSQL, the comment ends at the FIRST */ - so `b */` closes
    # it and SELECT survives.
    assert texts("/* a /* b */ SELECT 1") == ["SELECT", "1"]


def test_comment_markers_inside_strings_are_text():
    assert texts("SELECT '#not', '--not', '/*not*/'") == [
        "SELECT",
        "'#not'",
        ",",
        "'--not'",
        ",",
        "'/*not*/'",
    ]


def test_line_index():
    idx = LineIndex("ab\ncd\n\nx")
    assert idx.locate(0) == (1, 1)
    assert idx.locate(3) == (2, 1)
    assert idx.locate(7) == (4, 1)
    mixed = LineIndex("a\r\nb\rc\n")
    assert mixed.locate(3) == (2, 1)
    assert mixed.locate(5) == (3, 1)


def test_unquote_identifier():
    assert unquote_identifier("plain") == "plain"
    assert unquote_identifier("`a b`") == "a b"
    assert unquote_identifier(r"`we\`ird`") == "we`ird"


# ---------------------------------------------------------------------------
# Table-path parsing (dashed identifiers, backticks)
# ---------------------------------------------------------------------------


def test_dashed_project_path_requires_adjacency():
    pm = parse_table_path(tokenize("my-proj-123.ds.t rest"), 0)
    assert pm.parts == ["my-proj-123", "ds", "t"]
    # With whitespace the dash is arithmetic, not a name.
    pm = parse_table_path(tokenize("a - b"), 0)
    assert pm.parts == ["a"]


def test_dashed_segment_with_number_letter_suffix():
    pm = parse_table_path(tokenize("proj-1a.ds.t x"), 0)
    assert pm.parts == ["proj-1a", "ds", "t"]


def test_backtick_path_forms():
    assert parse_table_path(tokenize("`p.d.t` x"), 0).parts == ["p", "d", "t"]
    assert parse_table_path(tokenize("`p`.`d`.t x"), 0).parts == ["p", "d", "t"]
    assert parse_table_path(tokenize("`p.d`.t x"), 0).parts == ["p", "d", "t"]
    # Whitespace around dots is legal GoogleSQL.
    assert parse_table_path(tokenize("ds . t x"), 0).parts == ["ds", "t"]


def test_reserved_word_cannot_start_path():
    assert parse_table_path(tokenize("SELECT x"), 0) is None


# ---------------------------------------------------------------------------
# Splitter
# ---------------------------------------------------------------------------


def test_semicolons_in_strings_and_comments_do_not_split():
    stmts = split("SELECT 'a;b'; /* ; */ SELECT 2; -- ;\nSELECT 3")
    assert len(stmts) == 3


def test_begin_end_block_is_one_statement():
    sql = "BEGIN\n  CREATE TABLE d.t AS SELECT 1;\n" "  DELETE FROM d.t WHERE 1=1;\nEND;\nSELECT 2;"
    stmts = split(sql)
    assert len(stmts) == 2
    assert stmts[0].tokens[0].upper == "BEGIN"


def test_begin_transaction_is_not_a_block():
    stmts = split("BEGIN TRANSACTION; SELECT 1; COMMIT TRANSACTION;")
    assert len(stmts) == 3
    stmts = split("BEGIN; SELECT 1; COMMIT;")
    assert len(stmts) == 3


def test_scripting_if_vs_function_if():
    stmts = split("IF x > 1 THEN SELECT 1; END IF; SELECT IF(a, b, c);")
    assert len(stmts) == 2
    # IF-function right after THEN of a CASE *expression* must not open a
    # frame (the lookahead sees no THEN after the balanced group).
    stmts = split("SELECT CASE WHEN a THEN IF(x, 1, 2) ELSE 3 END; SELECT 2;")
    assert len(stmts) == 2


def test_scripting_if_with_parenthesized_condition():
    sql = "IF (x > 1) THEN SELECT 1; " "ELSEIF (y) THEN SELECT 2; ELSE SELECT 3; END IF;"
    assert len(split(sql)) == 1


def test_loop_while_repeat_for_blocks():
    sql = (
        "LOOP SELECT 1; BREAK; END LOOP;"
        "WHILE x < 3 DO SET x = x + 1; END WHILE;"
        "REPEAT SELECT 1; UNTIL done END REPEAT;"
        "FOR rec IN (SELECT 1 AS a) DO SELECT rec.a; END FOR;"
        "SELECT 'after';"
    )
    assert len(split(sql)) == 5


def test_scripting_case_block():
    sql = "CASE WHEN x = 1 THEN SELECT 1; ELSE SELECT 2; END CASE; SELECT 9;"
    assert len(split(sql)) == 2


def test_merge_then_clauses_do_not_open_frames():
    sql = (
        "MERGE d.t USING d.s ON t.id = s.id "
        "WHEN MATCHED THEN UPDATE SET a = s.a "
        "WHEN NOT MATCHED THEN INSERT ROW; SELECT 1;"
    )
    assert len(split(sql)) == 2


def test_last_statement_without_semicolon():
    stmts = split("SELECT 1; SELECT 2")
    assert len(stmts) == 2
    assert stmts[0].terminated is True
    assert stmts[1].terminated is False


def test_nested_blocks():
    sql = "BEGIN IF a THEN WHILE b DO SELECT 1; END WHILE; END IF; END; " "SELECT 2;"
    assert len(split(sql)) == 2


def test_repeat_body_starts_in_statement_position():
    # A REPEAT body begins immediately after the keyword (like LOOP), so a
    # scripting block as its first statement must not desynchronize frames.
    sql = "REPEAT IF x THEN SET y = 1; END IF; UNTIL x END REPEAT; SELECT 1;"
    assert len(split(sql)) == 2
    sql = "REPEAT BEGIN SELECT 1; END; UNTIL done END REPEAT; " "SELECT 'after';"
    assert len(split(sql)) == 2


def test_case_expression_keyword_columns_do_not_open_frames():
    # LOOP/BEGIN/WHILE/REPEAT are unreserved in GoogleSQL, so they are valid
    # column names directly after the THEN/ELSE of a CASE *expression*.
    # They must not be mistaken for scripting block openers, which would
    # leave the CASE frame unclosed and glue the following statements.
    sql = "SELECT CASE WHEN a THEN 1 ELSE loop END AS c FROM d.s; " "SELECT 2;"
    assert len(split(sql)) == 2
    sql = "SELECT CASE WHEN a THEN begin ELSE 1 END AS c FROM d.s; " "SELECT 2;"
    assert len(split(sql)) == 2
    # The scripting CASE form still grants statement position to its
    # branches, including nested blocks.
    sql = "CASE WHEN x THEN BEGIN SELECT 1; END; ELSE SELECT 2; END CASE; " "SELECT 3;"
    assert len(split(sql)) == 2


def test_elseif_condition_is_not_statement_position():
    # ELSEIF is followed by a condition; a condition starting with an
    # unreserved keyword such as `begin` must not open a block frame.
    sql = "IF a THEN SELECT 1; ELSEIF begin THEN SELECT 2; END IF; " "SELECT 3;"
    assert len(split(sql)) == 2
