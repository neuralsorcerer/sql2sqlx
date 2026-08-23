# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""Regression tests for semantic and lexical correctness gaps.

These cases are intentionally derived from the official GoogleSQL and
Dataform grammars.  Each test protects a failure mode that can otherwise
produce invalid SQLX or silently change multi-statement behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sql2sqlx import (
    ActionType,
    ConversionOptions,
    IfNotExistsStrategy,
    InsertStrategy,
    MergeStrategy,
    convert_directory,
    convert_file,
    convert_string,
    parse_source,
    write_result,
)
from sql2sqlx.errors import ConversionError
from sql2sqlx.lexer import EOF, NUMBER, PARAM, LexError, tokenize, unquote_identifier
from sql2sqlx.model import ConversionReport, ConversionResult, SqlxFile, sanitize_filename
from sql2sqlx.refs import parse_table_path, scan_ref_sites
from sql2sqlx.splitter import split_statements


def _texts(sql: str) -> list[str]:
    return [token.text for token in tokenize(sql) if token.kind != EOF]


def _codes(result: object) -> set[str]:
    return {warning.code for warning in result.report.warnings}  # type: ignore[attr-defined]


def _warning(result: object, code: str) -> object:
    for warning in result.report.warnings:  # type: ignore[attr-defined]
        if warning.code == code:
            return warning
    raise AssertionError(f"missing warning {code!r}")


def _by_name(result: object, name: str) -> object:
    for file in result.files:  # type: ignore[attr-defined]
        if file.action_name == name:
            return file
    raise AssertionError(f"missing action {name!r}")


def _synthetic_result(*relpaths: str) -> ConversionResult:
    """Build a minimal result for exercising the public output writer."""
    return ConversionResult(
        files=[
            SqlxFile(path, 'config { type: "operations" }\n', ActionType.OPERATIONS, "test")
            for path in relpaths
        ],
        report=ConversionReport(),
    )


@pytest.mark.parametrize(
    "relpath",
    ["", "/absolute.sqlx", "../escape.sqlx", "nested/../alias.sqlx", "nested\\file.sqlx"],
)
def test_write_result_rejects_noncanonical_paths(tmp_path: Path, relpath: str) -> None:
    with pytest.raises(ConversionError, match="canonical relative path"):
        write_result(_synthetic_result(relpath), str(tmp_path / "out"))
    assert not (tmp_path / "out").exists()


def test_write_result_rejects_destination_collisions_before_writing(tmp_path: Path) -> None:
    result = _synthetic_result("same.sqlx", "same.sqlx")
    with pytest.raises(ConversionError, match="collide"):
        write_result(result, str(tmp_path / "out"))
    assert not (tmp_path / "out").exists()


def test_lexer_enforces_raw_and_newline_backslash_rules() -> None:
    # In a raw literal the backslash still consumes the following quote, so
    # a raw string ending in an odd number of backslashes never closes.
    with pytest.raises(LexError, match="[Uu]nterminated string"):
        tokenize(r"SELECT r'\'")
    with pytest.raises(LexError, match="[Uu]nterminated string"):
        tokenize(r"SELECT r'abc\\\'")
    with pytest.raises(LexError, match="newline"):
        tokenize("SELECT 'a\\\nb'")
    with pytest.raises(LexError, match="newline"):
        tokenize("SELECT '''a\\\nb'''")


def test_raw_string_escaped_quote_does_not_terminate() -> None:
    # GoogleSQL: escapes in raw literals are not interpreted, but the
    # backslash still consumes the next character, so r'a\'b' is ONE
    # literal whose value contains the backslash and the quote.
    assert _texts(r"SELECT r'a\'b'") == ["SELECT", r"r'a\'b'"]
    assert _texts(r'SELECT R"x\"y"') == ["SELECT", r'R"x\"y"']
    assert _texts(r"SELECT rb'x\'y'") == ["SELECT", r"rb'x\'y'"]
    assert _texts(r"SELECT r'''a\'''b'''") == ["SELECT", r"r'''a\'''b'''"]
    # Even trailing backslashes still close the literal exactly as before.
    assert _texts(r"SELECT r'a\\'") == ["SELECT", r"r'a\\'"]
    # A semicolon after an escaped quote is inside the literal, not a split.
    statements = split_statements(tokenize(r"SELECT r'a\';b'; SELECT 2;"))
    assert len(statements) == 2


def test_lexer_supports_hex_integers_and_quoted_parameters() -> None:
    tokens = [token for token in tokenize("SELECT 0xABC, 1.e2, @`select`") if token.kind != EOF]
    assert tokens[1].kind == NUMBER and tokens[1].text == "0xABC"
    assert tokens[3].kind == NUMBER and tokens[3].text == "1.e2"
    assert tokens[5].kind == PARAM and tokens[5].text == "@`select`"


def test_identifier_escape_decoding_is_complete() -> None:
    assert unquote_identifier(r"`A\x42\101\u0044\U00000045`") == "ABADE"


def test_empty_quoted_identifiers_are_rejected() -> None:
    with pytest.raises(LexError, match="cannot be empty"):
        tokenize("SELECT ``")
    with pytest.raises(LexError, match="cannot be empty"):
        tokenize("SELECT @``")


def test_current_reserved_keywords_are_not_paths() -> None:
    assert parse_table_path(tokenize("GRAPH_TABLE(x)"), 0) is None


def test_compound_parenthesized_if_condition_stays_one_statement() -> None:
    sql = "IF (a = 1) AND b THEN SELECT 1; ELSE SELECT 2; END IF; SELECT 3;"
    statements = split_statements(tokenize(sql))
    assert len(statements) == 2
    assert statements[0].tokens[0].upper == "IF"


def test_labeled_blocks_and_loops_split_correctly() -> None:
    sql = (
        "outer_label: BEGIN SELECT 1; END outer_label;"
        "`loop.label`: LOOP SELECT 2; LEAVE `loop.label`; END LOOP `loop.label`;"
        "SELECT 3;"
    )
    assert len(split_statements(tokenize(sql))) == 3


def test_create_procedure_body_is_not_split_at_inner_semicolons() -> None:
    sql = "CREATE PROCEDURE d.p() BEGIN " "SET x = 1; SELECT x; END; " "SELECT 2;"
    statements = split_statements(tokenize(sql))
    assert len(statements) == 2
    assert [token.upper for token in statements[0].tokens[:2]] == ["CREATE", "PROCEDURE"]


def test_transaction_is_preserved_as_one_operations_action() -> None:
    sql = "BEGIN TRANSACTION; " "UPDATE d.t SET x = 1 WHERE TRUE; " "COMMIT TRANSACTION;"
    result = convert_string(sql)
    assert len(result.files) == 1
    content = result.files[0].content
    assert result.files[0].action_type.value == "operations"
    assert "BEGIN TRANSACTION;" in content and "COMMIT TRANSACTION" in content
    assert "hasOutput" not in content and "${self()}" not in content
    assert "SCRIPT_FILE" in _codes(result)


def test_temporary_objects_keep_shared_script_scope() -> None:
    sql = (
        "CREATE TEMP TABLE temp_rows AS SELECT 1 AS x; "
        "CREATE TEMP FUNCTION plus_one(v INT64) AS (v + 1); "
        "SELECT plus_one(x) FROM temp_rows;"
    )
    result = convert_string(sql)
    assert len(result.files) == 1
    content = result.files[0].content
    assert "CREATE TEMP TABLE temp_rows" in content
    assert "CREATE TEMP FUNCTION plus_one" in content
    assert "FROM temp_rows" in content
    assert "${ref" not in content


def test_temporary_aggregate_function_keeps_its_call_in_one_action() -> None:
    result = convert_string(
        "CREATE TEMP AGGREGATE FUNCTION AddAll(x INT64) AS (SUM(x)); "
        "SELECT AddAll(x) FROM UNNEST([1, 2]) AS x;"
    )
    assert len(result.files) == 1
    assert "CREATE TEMP AGGREGATE FUNCTION" in result.files[0].content
    assert "SELECT AddAll(x)" in result.files[0].content
    assert "SCRIPT_FILE" in _codes(result)


def test_script_report_does_not_claim_inner_typed_conversions() -> None:
    result = convert_string(
        "DECLARE cutoff INT64 DEFAULT 1; "
        "CREATE TABLE d.t AS SELECT cutoff AS x; "
        "INSERT INTO d.t SELECT 2;",
        ConversionOptions(insert_strategy=InsertStrategy.INCREMENTAL),
    )
    assert result.files[0].action_type.value == "operations"
    codes = _codes(result)
    assert "CREATE_REPLACE_SEMANTICS" not in codes
    assert "INSERT_INCREMENTAL" not in codes


def test_dynamic_side_effects_are_reported_for_manual_dependencies() -> None:
    direct = convert_string("CALL d.mutate_tables(); CREATE TABLE d.after AS SELECT 1 AS x;")
    assert "DYNAMIC_SIDE_EFFECTS" in _codes(direct)
    assert len(direct.files) == 1
    assert direct.files[0].action_type.value == "operations"

    scripted = convert_string("BEGIN EXECUTE IMMEDIATE 'DELETE FROM d.t WHERE TRUE'; END;")
    assert "DYNAMIC_SIDE_EFFECTS" in _codes(scripted)


def test_procedural_block_writes_are_tracked_across_files(tmp_path: Path) -> None:
    source = tmp_path / "sql"
    source.mkdir()
    (source / "a_script.sql").write_text(
        "BEGIN UPDATE d.t SET x = 1 WHERE TRUE; END;", encoding="utf-8"
    )
    (source / "b_reader.sql").write_text("CREATE TABLE d.r AS SELECT * FROM d.t;", encoding="utf-8")
    result = convert_directory(str(source), options=ConversionOptions(jobs=1))
    reader = _by_name(result, "r")
    assert "a_script" in reader.content
    assert "SCRIPT_WRITES" in _codes(result)


def test_script_reference_state_resets_at_inner_semicolons() -> None:
    sites = scan_ref_sites(
        tokenize("BEGIN " "SELECT * FROM d.a AS b; " "SELECT 1, d.not_a_table FROM b; " "END"),
        "SCRIPT",
    )
    assert [site.name.display() for site in sites] == ["d.a", "b"]


def test_reference_context_does_not_leak_into_query_expressions() -> None:
    nested = scan_ref_sites(
        tokenize(
            "SELECT * FROM ((SELECT x, d.not_a_table FROM d.real)) q "
            "JOIN d.joined ON q.x = joined.x"
        ),
        "SELECT",
    )
    assert [site.name.display() for site in nested] == ["d.real", "d.joined"]

    piped = scan_ref_sites(
        tokenize("FROM d.source |> SELECT x, d.not_a_table " "|> JOIN d.lookup ON x = lookup.x"),
        "FROM",
    )
    assert [site.name.display() for site in piped] == ["d.source", "d.lookup"]

    array_condition = scan_ref_sites(
        tokenize("SELECT * FROM d.a JOIN d.b " "ON [d.not_a_table, 1][OFFSET(0)] = [1][OFFSET(0)]"),
        "SELECT",
    )
    assert [site.name.display() for site in array_condition] == ["d.a", "d.b"]

    merge = scan_ref_sites(
        tokenize(
            "MERGE d.t t USING ("
            "SELECT * FROM d.a JOIN d.b USING(id)"
            ") s ON t.id = s.id WHEN NOT MATCHED THEN INSERT ROW"
        ),
        "MERGE",
    )
    assert [site.name.display() for site in merge] == ["d.a", "d.b"]


def test_quoted_ctes_and_range_aliases_are_never_physical_refs() -> None:
    quoted_cte = scan_ref_sites(
        tokenize("WITH `local-rows` AS (SELECT * FROM d.source) " "SELECT * FROM `local-rows`"),
        "WITH",
    )
    assert [site.name.display() for site in quoted_cte] == ["d.source"]

    implicit_alias = scan_ref_sites(
        tokenize("SELECT * FROM d.parent, parent.children"),
        "SELECT",
    )
    assert [site.name.display() for site in implicit_alias] == ["d.parent"]

    dotted_alias = scan_ref_sites(
        tokenize("SELECT * FROM d.parent AS `p.root`, `p.root`.children"),
        "SELECT",
    )
    assert [site.name.display() for site in dotted_alias] == ["d.parent"]

    update_target_alias = scan_ref_sites(
        tokenize("UPDATE d.parent AS p SET x = child.x " "FROM p.children AS child"),
        "UPDATE",
    )
    assert update_target_alias == []

    # End-to-end: a physical table with the same name must not capture the
    # quoted CTE reference when defaults make both names resolvable.
    result = convert_string(
        "CREATE TABLE d.`local-rows` AS SELECT 1 AS x;"
        "CREATE TABLE d.out AS "
        "WITH `local-rows` AS (SELECT 2 AS x) "
        "SELECT * FROM `local-rows`;",
        ConversionOptions(default_dataset="d"),
    )
    assert '${ref("d", "local-rows")}' not in _by_name(result, "out").content


def test_reference_names_follow_query_scope_and_cte_visibility() -> None:
    # A non-recursive CTE cannot see a later CTE. The same text under WITH
    # RECURSIVE can, so only the former `b` is a physical table read.
    non_recursive = scan_ref_sites(
        tokenize("WITH a AS (SELECT * FROM b), b AS (SELECT 1) " "SELECT * FROM a"),
        "WITH",
    )
    assert [site.name.display() for site in non_recursive] == ["b"]
    recursive = scan_ref_sites(
        tokenize("WITH RECURSIVE a AS (SELECT * FROM b), b AS (SELECT 1) " "SELECT * FROM a"),
        "WITH",
    )
    assert recursive == []

    # A one-part CTE name does not hide a qualified physical table path.
    qualified = scan_ref_sites(
        tokenize("WITH d AS (SELECT 1) SELECT * FROM d.real"),
        "WITH",
    )
    assert [site.name.display() for site in qualified] == ["d.real"]

    quoted_correlated = scan_ref_sites(
        tokenize(
            "WITH `p.root` AS (SELECT * FROM d.source) "
            "SELECT (SELECT COUNT(*) FROM `p.root`.children) "
            "FROM `p.root`"
        ),
        "WITH",
    )
    assert [site.name.display() for site in quoted_correlated] == ["d.source"]

    # Inner aliases and aliases from an earlier set-operation branch do not
    # leak into an outer/later query block.
    inner = scan_ref_sites(
        tokenize("SELECT * FROM (SELECT * FROM d.inner AS shadow) q " "JOIN shadow.real ON TRUE"),
        "SELECT",
    )
    assert [site.name.display() for site in inner] == ["d.inner", "shadow.real"]
    union = scan_ref_sites(
        tokenize("SELECT * FROM d.a AS x UNION ALL SELECT * FROM x.b"),
        "SELECT",
    )
    assert [site.name.display() for site in union] == ["d.a", "x.b"]


def test_correlated_and_non_lateral_subquery_alias_visibility() -> None:
    # SELECT expressions are lexically before FROM, but the FROM alias is
    # visible to their correlated scalar subqueries.
    correlated = scan_ref_sites(
        tokenize("SELECT (SELECT COUNT(*) FROM p.children) " "FROM d.parent AS p"),
        "SELECT",
    )
    assert [site.name.display() for site in correlated] == ["d.parent"]

    # A derived table in FROM is not lateral in BigQuery. Its x.b path is a
    # physical table path, not a correlation to the preceding x alias.
    derived = scan_ref_sites(
        tokenize("SELECT * FROM d.a AS x " "JOIN (SELECT * FROM x.b) q ON TRUE"),
        "SELECT",
    )
    assert [site.name.display() for site in derived] == ["d.a", "x.b"]

    # BigQuery does not allow a CTE body to correlate to an outer column, so
    # the qualified path is a physical table read even inside a scalar query.
    cte_body = scan_ref_sites(
        tokenize(
            "SELECT (WITH c AS (SELECT * FROM p.children) SELECT * FROM c) " "FROM d.parent AS p"
        ),
        "SELECT",
    )
    assert [site.name.display() for site in cte_body] == [
        "p.children",
        "d.parent",
    ]

    # Explicit and implicit UNNEST range variables remain local paths.
    explicit = scan_ref_sites(
        tokenize(
            "SELECT * FROM d.parent, UNNEST(parent.children) AS child, " "child.grandchildren"
        ),
        "SELECT",
    )
    assert [site.name.display() for site in explicit] == ["d.parent"]
    implicit = scan_ref_sites(
        tokenize("SELECT * FROM d.parent, UNNEST(parent.children), " "children.grandchildren"),
        "SELECT",
    )
    assert [site.name.display() for site in implicit] == ["d.parent"]

    # A correlated subquery wrapped by UNNEST is legal and its own physical
    # source must still participate in dependency linking.
    array_subquery = scan_ref_sites(
        tokenize(
            "SELECT * FROM d.parent AS p JOIN "
            "UNNEST(ARRAY(SELECT AS STRUCT * FROM d.child AS c "
            "WHERE c.parent_id = p.id)) AS matches ON TRUE"
        ),
        "SELECT",
    )
    assert [site.name.display() for site in array_subquery] == [
        "d.parent",
        "d.child",
    ]


def test_standalone_pipe_query_is_reported_as_an_orphan_query() -> None:
    result = convert_string("FROM d.source |> SELECT *;")
    assert result.files[0].action_type.value == "operations"
    assert "ORPHAN_SELECT" in _codes(result)
    assert result.report.refs_unresolved == ["d.source"]


def test_trailing_comments_stay_with_their_statement() -> None:
    result = convert_string(
        "SELECT 1 /* first-tail */; -- second-head\n" "SELECT 2; -- final-tail\n"
    )
    first = _by_name(result, "input")
    second = _by_name(result, "input_2")
    assert "first-tail" in first.content and "first-tail" not in second.content
    assert "second-head" in second.content
    assert "final-tail" in second.content


def test_file_conversion_preserves_internal_crlf_newlines(tmp_path: Path) -> None:
    source = tmp_path / "crlf.sql"
    source.write_bytes(b"CREATE TABLE d.t AS\r\nSELECT 1 AS x\r\nFROM UNNEST([1]);\r\n")
    result = convert_file(str(source))
    assert "SELECT 1 AS x\r\nFROM UNNEST([1])" in result.files[0].content
    assert result.report.input_bytes == source.stat().st_size


def test_mutating_dml_is_never_falsely_declared_as_an_output() -> None:
    for sql in (
        "UPDATE d.t SET x = 1 WHERE TRUE;",
        "INSERT INTO d.t SELECT 1;",
        "MERGE d.t t USING d.s s ON t.id = s.id WHEN NOT MATCHED THEN INSERT ROW;",
    ):
        result = convert_string(sql)
        assert "hasOutput" not in result.files[0].content
        assert "${self()}" not in result.files[0].content


def test_copy_like_ddl_sources_participate_in_dependencies() -> None:
    result = convert_string(
        "CREATE TABLE d.source AS SELECT 1 AS x;"
        "CREATE TABLE d.clone CLONE d.source;"
        "CREATE SNAPSHOT TABLE d.snapshot CLONE d.source;"
        "CREATE MATERIALIZED VIEW d.base AS SELECT 1 AS x;"
        "CREATE MATERIALIZED VIEW d.replica AS REPLICA OF d.base;"
    )
    clone = _by_name(result, "clone")
    snapshot = _by_name(result, "snapshot")
    replica = _by_name(result, "replica")
    assert '${ref("d", "source")}' in clone.content
    assert '${ref("d", "source")}' in snapshot.content
    assert '${ref("d", "base")}' in replica.content
    assert result.report.refs_rewritten == 3


def test_nested_script_clone_source_is_discovered() -> None:
    parsed = parse_source(
        "script.sql",
        "script.sql",
        "BEGIN CREATE TABLE d.clone CLONE d.source; END;",
        ConversionOptions(),
    )
    assert parsed.error is None
    assert [site.name.display() for site in parsed.drafts[0].ref_sites] == ["d.source"]


def test_external_declarations_exclude_table_decorators() -> None:
    result = convert_string(
        "SELECT * FROM `d.events$20240101`;",
        ConversionOptions(declare_external=True),
    )
    assert len(result.files) == 1
    assert result.files[0].action_type.value == "operations"
    assert "`d.events$20240101`" in result.files[0].content
    assert "${ref" not in result.files[0].content


def test_external_declaration_uses_defaults_for_unqualified_source() -> None:
    result = convert_string(
        "CREATE TABLE d.out AS SELECT * FROM external_rows;",
        ConversionOptions(
            default_project="project-a",
            default_dataset="d",
            declare_external=True,
        ),
    )
    declaration = _by_name(result, "external_rows")
    output = _by_name(result, "out")
    assert declaration.action_type.value == "declaration"
    assert 'database: "project-a"' in declaration.content
    assert 'schema: "d"' in declaration.content
    assert (
        '${ref({database: "project-a", schema: "d", ' 'name: "external_rows"})}' in output.content
    )


def test_semantics_changing_conversions_are_opt_in() -> None:
    insert = convert_string("INSERT INTO d.t SELECT 1 AS x;")
    assert insert.files[0].action_type.value == "operations"
    opted_in = convert_string(
        "INSERT INTO d.t SELECT 1 AS x;",
        ConversionOptions(insert_strategy=InsertStrategy.INCREMENTAL),
    )
    assert opted_in.files[0].action_type.value == "incremental"

    guarded = convert_string("CREATE TABLE IF NOT EXISTS d.t AS SELECT 1;")
    assert guarded.files[0].action_type.value == "operations"
    opted_in_guard = convert_string(
        "CREATE TABLE IF NOT EXISTS d.t AS SELECT 1;",
        ConversionOptions(if_not_exists_strategy=IfNotExistsStrategy.TABLE),
    )
    assert opted_in_guard.files[0].action_type.value == "table"

    guarded_view = convert_string("CREATE VIEW IF NOT EXISTS d.v AS SELECT 1 AS x;")
    assert guarded_view.files[0].action_type.value == "operations"
    opted_in_view = convert_string(
        "CREATE VIEW IF NOT EXISTS d.v AS SELECT 1 AS x;",
        ConversionOptions(if_not_exists_strategy=IfNotExistsStrategy.TABLE),
    )
    assert opted_in_view.files[0].action_type.value == "view"


def test_alias_rewrite_falls_back_if_later_clause_uses_old_alias() -> None:
    sql = "INSERT INTO d.t (new_name) SELECT x AS old_name FROM d.s ORDER BY old_name;"
    result = convert_string(sql, ConversionOptions(insert_strategy=InsertStrategy.INCREMENTAL))
    assert result.files[0].action_type.value == "operations"
    assert "FALLBACK_SELECT_ALIAS" in _codes(result)

    ordinal = convert_string(
        "INSERT INTO d.t (new_name) SELECT x AS old_name FROM d.s ORDER BY 1;",
        ConversionOptions(insert_strategy=InsertStrategy.INCREMENTAL),
    )
    assert ordinal.files[0].action_type.value == "incremental"
    assert "x AS new_name" in ordinal.files[0].content

    nested = convert_string(
        "INSERT INTO d.t (new_name) " "SELECT x AS old_name FROM d.s ORDER BY ABS(old_name);",
        ConversionOptions(insert_strategy=InsertStrategy.INCREMENTAL),
    )
    assert nested.files[0].action_type.value == "operations"

    case_alias = convert_string(
        "INSERT INTO d.t (new_name) " "SELECT CASE WHEN x THEN 1 ELSE 0 END old_name FROM d.s;",
        ConversionOptions(insert_strategy=InsertStrategy.INCREMENTAL),
    )
    assert case_alias.files[0].action_type.value == "incremental"
    assert "END new_name FROM" in case_alias.files[0].content
    assert "old_name AS" not in case_alias.files[0].content


def test_alias_rewrite_falls_back_if_new_alias_shadows_a_column() -> None:
    # BigQuery resolves SELECT-list aliases in preference to FROM columns
    # inside GROUP BY/HAVING/QUALIFY/ORDER BY. Introducing the alias `a`
    # would silently change `ORDER BY a LIMIT 10` from ordering by column
    # s.a to ordering by x - selecting different rows.
    shadowed = convert_string(
        "INSERT INTO d.t (a) SELECT x FROM d.s ORDER BY a LIMIT 10;",
        ConversionOptions(insert_strategy=InsertStrategy.INCREMENTAL),
    )
    assert shadowed.files[0].action_type.value == "operations"
    assert "FALLBACK_SELECT_ALIAS" in _codes(shadowed)

    # Renaming an alias can shadow through the NEW name as well.
    renamed = convert_string(
        "INSERT INTO d.t (a) SELECT x AS old FROM d.s ORDER BY a LIMIT 10;",
        ConversionOptions(insert_strategy=InsertStrategy.INCREMENTAL),
    )
    assert renamed.files[0].action_type.value == "operations"

    # Ordering by the underlying column stays safe and still converts.
    safe = convert_string(
        "INSERT INTO d.t (a) SELECT x FROM d.s ORDER BY x LIMIT 10;",
        ConversionOptions(insert_strategy=InsertStrategy.INCREMENTAL),
    )
    assert safe.files[0].action_type.value == "incremental"
    assert "SELECT x AS a FROM d.s ORDER BY x LIMIT 10" in safe.files[0].content

    # The view column-list path shares the rewrite and must fall back too.
    view = convert_string("CREATE VIEW d.v (a) AS SELECT x FROM d.s ORDER BY a LIMIT 5;")
    assert view.files[0].action_type.value == "operations"
    assert "FALLBACK_SELECT_ALIAS" in _codes(view)


def test_incremental_rebuild_warning_matches_the_protected_flag() -> None:
    # Dataform Core rebuilds an incremental from its query when the target is
    # missing, and on --full-refresh only when the action is not `protected`
    # (shouldWriteIncrementally). The warning must not claim a rebuild trigger
    # this action does not actually have.
    protected = convert_string(
        "INSERT INTO d.t SELECT 1 AS a;",
        ConversionOptions(insert_strategy=InsertStrategy.INCREMENTAL),
    )
    assert "protected: true" in protected.files[0].content
    message = _warning(protected, "INSERT_INCREMENTAL").message
    assert "on its first run" in message
    assert "protected: true keeps a later --full-refresh from rebuilding it." in message

    unprotected = convert_string(
        "INSERT INTO d.t SELECT 1 AS a;",
        ConversionOptions(insert_strategy=InsertStrategy.INCREMENTAL, protect_incrementals=False),
    )
    assert "protected" not in unprotected.files[0].content
    unprotected_message = _warning(unprotected, "INSERT_INCREMENTAL").message
    assert "on first run or --full-refresh" in unprotected_message
    assert "protected: true" not in unprotected_message


def test_safe_merge_requires_the_join_to_pair_identically_named_columns() -> None:
    # Dataform builds its MERGE as `ON T.<key> = S.<key>` from uniqueKey
    # alone, so it can only reproduce a join that pairs same-named columns.
    # `ON T.a = S.b` would silently become `ON T.a = S.a` - a different join,
    # and wrong rows updated - so it must stay an operations action.
    cross_named = convert_string(
        "MERGE d.t T USING (SELECT a, b, v FROM d.s) S ON T.a = S.b "
        "WHEN MATCHED THEN UPDATE SET a = S.a, b = S.b, v = S.v "
        "WHEN NOT MATCHED THEN INSERT (a, b, v) VALUES (S.a, S.b, S.v);",
        ConversionOptions(merge_strategy=MergeStrategy.INCREMENTAL_WHEN_SAFE),
    )
    assert cross_named.files[0].action_type.value == "operations"
    assert "MERGE_FALLBACK" in _codes(cross_named)

    # The same shape joined on matching names is the provable one.
    same_named = convert_string(
        "MERGE d.t T USING (SELECT a, b, v FROM d.s) S ON T.a = S.a "
        "WHEN MATCHED THEN UPDATE SET a = S.a, b = S.b, v = S.v "
        "WHEN NOT MATCHED THEN INSERT (a, b, v) VALUES (S.a, S.b, S.v);",
        ConversionOptions(merge_strategy=MergeStrategy.INCREMENTAL_WHEN_SAFE),
    )
    assert same_named.files[0].action_type.value == "incremental"


def test_if_expression_in_statement_position_cannot_open_a_block_frame() -> None:
    # `IF(a, b, c)` is a function, not the scripting `IF cond THEN`. Opening a
    # frame for it would swallow the following `;` and glue two statements
    # into one action, silently changing what runs.
    inside_block = split_statements(tokenize("BEGIN IF(a,1,2); SELECT 2; END; SELECT 3;"))
    assert len(inside_block) == 2

    top_level = split_statements(tokenize("SELECT IF(a,1,2); SELECT 2;"))
    assert len(top_level) == 2

    # ...while the scripting form still holds its block together.
    scripting = split_statements(tokenize("IF x > 1 THEN SELECT 1; END IF; SELECT 2;"))
    assert len(scripting) == 2


def test_metadata_and_region_schemas_are_never_declared_as_sources() -> None:
    # INFORMATION_SCHEMA views and region qualifiers are not relations a
    # Dataform declaration can name, so --declare-external must skip them and
    # leave the reference literal.
    for path in (
        "ds.INFORMATION_SCHEMA.TABLES",
        "`region-us`.INFORMATION_SCHEMA.JOBS",
        "`region-us.INFORMATION_SCHEMA.JOBS_BY_PROJECT`",
    ):
        result = convert_string(
            f"CREATE OR REPLACE TABLE d.x AS SELECT * FROM {path};",
            ConversionOptions(declare_external=True, default_dataset="ds"),
        )
        assert [file.relpath for file in result.files] == ["x.sqlx"], path
        assert "${ref" not in result.files[0].content, path


def test_comments_outside_a_typed_body_are_reported_not_silently_lost() -> None:
    # A typed action emits only its query body, so a comment written in the
    # surrounding DDL has nowhere to go. The SQL must not change, but the loss
    # has to reach the report - every other lossy step in the pipeline does.
    prefix = convert_string(
        "CREATE OR REPLACE TABLE m.d -- rebuilt nightly\n"
        "PARTITION BY DATE(ts) AS SELECT ts FROM s.o;",
        ConversionOptions(annotate=False),
    )
    assert prefix.files[0].action_type.value == "table"
    assert "rebuilt nightly" not in prefix.files[0].content
    assert _warning(prefix, "COMMENT_DROPPED").line == 1  # type: ignore[attr-defined]

    # A proven MERGE keeps only the source subquery; its WHEN clauses go away.
    merged = convert_string(
        "MERGE m.t T USING (SELECT id, v FROM m.s) S ON T.id=S.id\n"
        "WHEN MATCHED THEN UPDATE SET id=S.id, v=S.v  -- refresh all columns\n"
        "WHEN NOT MATCHED THEN INSERT (id,v) VALUES (S.id,S.v);",
        ConversionOptions(annotate=False, merge_strategy=MergeStrategy.INCREMENTAL_WHEN_SAFE),
    )
    assert merged.files[0].action_type.value == "incremental"
    assert _warning(merged, "COMMENT_DROPPED").line == 2  # type: ignore[attr-defined]

    # Comments that DO get carried must never raise it.
    for sql in (
        "-- header\nCREATE OR REPLACE TABLE m.d AS SELECT 1 AS x;",
        "CREATE OR REPLACE TABLE m.d AS SELECT 1 AS x -- inline\n;",
        "CREATE OR REPLACE TABLE m.d AS SELECT 1 AS x;\n-- tail",
        "CREATE TABLE m.d -- verbatim\n(x INT64);",
        "DECLARE v INT64; -- script\nINSERT INTO m.d SELECT 1;",
    ):
        assert "COMMENT_DROPPED" not in _codes(convert_string(sql, ConversionOptions())), sql


def test_comment_attribution_is_exact_across_statement_boundaries() -> None:
    # Leading/trailing/body comment windows are resolved by binary search over
    # the file's comment spans (a full rescan per draft is quadratic in the
    # number of commented statements). Each comment must still land in exactly
    # one action, and in the same one as before.
    sql = (
        "-- header one\n"
        "-- header two\n"
        "CREATE OR REPLACE TABLE d.a AS SELECT 1 AS x; -- after a\n"
        "/* between */\n"
        "CREATE OR REPLACE TABLE d.b AS SELECT 2 AS y -- inline b\n"
        ";\n"
        "-- tail one\n"
        "/* tail two */\n"
    )
    result = convert_string(sql, ConversionOptions(annotate=False))
    first = _by_name(result, "a").content  # type: ignore[attr-defined]
    second = _by_name(result, "b").content  # type: ignore[attr-defined]

    assert "-- header one\n-- header two" in first
    assert "after a" not in first
    # A comment after the previous terminator belongs to the next statement.
    assert "-- after a" in second
    assert "/* between */" in second
    assert "SELECT 2 AS y -- inline b" in second
    # Comments past the final terminator ride along with the last action.
    assert "-- tail one" in second
    assert "/* tail two */" in second

    joined = "".join(file.content for file in result.files)
    for fragment in (
        "header one",
        "header two",
        "after a",
        "between",
        "inline b",
        "tail one",
        "tail two",
    ):
        assert joined.count(fragment) == 1, fragment


def test_set_operation_order_by_blocks_the_alias_rewrite() -> None:
    # A set operation's trailing ORDER BY can only name the query's OUTPUT
    # columns; unlike a simple query there is no FROM clause left for a
    # renamed bare column to fall back to. Aliasing `c1` to `x` here would
    # leave `ORDER BY c1` naming a column that no longer exists.
    view = convert_string(
        "CREATE VIEW d.v (x, y) AS "
        "SELECT c1, c2 FROM d.t UNION ALL SELECT c3, c4 FROM d.u ORDER BY c1;"
    )
    assert view.files[0].action_type.value == "operations"
    assert "FALLBACK_SELECT_ALIAS" in _codes(view)

    inserted = convert_string(
        "INSERT INTO d.t (x, y) "
        "SELECT c1, c2 FROM d.a UNION ALL SELECT c3, c4 FROM d.b ORDER BY c2;",
        ConversionOptions(insert_strategy=InsertStrategy.INCREMENTAL),
    )
    assert inserted.files[0].action_type.value == "operations"
    assert "FALLBACK_SELECT_ALIAS" in _codes(inserted)

    # Ordinals name positions, not output columns, so they stay convertible.
    ordinal = convert_string(
        "CREATE VIEW d.v (x, y) AS "
        "SELECT c1, c2 FROM d.t UNION ALL SELECT c3, c4 FROM d.u ORDER BY 1;"
    )
    assert ordinal.files[0].action_type.value == "view"
    assert "SELECT c1 AS x, c2 AS y FROM d.t UNION ALL" in ordinal.files[0].content

    # `SELECT * EXCEPT (column)` is a projection modifier, not a set
    # operator, and must not switch the stricter rule on by itself.
    projection = convert_string("CREATE VIEW d.v (x, y) AS SELECT c1, c2 FROM d.t ORDER BY c1;")
    assert projection.files[0].action_type.value == "view"
    assert "SELECT c1 AS x, c2 AS y FROM d.t ORDER BY c1" in projection.files[0].content


def test_case_expression_keyword_column_cannot_glue_statements() -> None:
    # `loop` is unreserved, so it is a valid column name after ELSE in a
    # CASE expression. A frame-tracking slip here once glued the following
    # UPDATE into the typed table body.
    result = convert_string(
        "CREATE TABLE d.t AS "
        "SELECT CASE WHEN a THEN 1 ELSE loop END AS c FROM d.s;\n"
        "UPDATE d.other SET x = 1 WHERE TRUE;"
    )
    assert result.report.statements == 2
    table = _by_name(result, "t")
    assert table.action_type.value == "table"
    assert "UPDATE" not in table.content
    update = _by_name(result, "other_update")
    assert update.action_type.value == "operations"
    assert "UPDATE d.other SET x = 1" in update.content


def test_generated_alias_is_sqlx_safe_and_runtime_quoteable() -> None:
    result = convert_string(
        "INSERT INTO d.t (`${column}`) SELECT 1;",
        ConversionOptions(insert_strategy=InsertStrategy.INCREMENTAL),
    )
    assert result.files[0].action_type.value == "incremental"
    assert 'AS ${"`${column}`"}' in result.files[0].content
    assert "FALLBACK_SELECT_ALIAS" not in _codes(result)

    control = convert_string(
        r"INSERT INTO d.t (`\177`) SELECT 1;",
        ConversionOptions(insert_strategy=InsertStrategy.INCREMENTAL),
    )
    assert control.files[0].action_type.value == "operations"
    assert r"`\177`" in control.files[0].content


def test_literal_sqlx_markers_are_context_aware() -> None:
    result = convert_string(
        "-- ${comment stays a comment}\n"
        "CREATE TABLE d.marker AS "
        "SELECT '${string_marker}' AS s, `${identifier_marker}` AS i;"
    )
    content = result.files[0].content
    assert "-- ${comment stays a comment}" in content
    assert "${\"'${string_marker}'\"}" in content
    assert '${"`${identifier_marker}`"}' in content


def test_google_hash_comments_are_normalized_for_sqlx() -> None:
    result = convert_string(
        "# ${comment_marker}\n"
        "CREATE TABLE d.hash_comments AS\n"
        "SELECT 1 AS id # config {\n"
        "FROM UNNEST([1]);\n"
        "# trailing ${marker}\n"
    )
    content = result.files[0].content
    assert "# ${comment_marker}" not in content
    assert "-- ${comment_marker}" in content
    assert "SELECT 1 AS id -- config {" in content
    assert "-- trailing ${marker}" in content


def test_sqlx_separator_comment_lines_are_neutralized() -> None:
    result = convert_string(
        "CREATE TABLE d.separator_comments AS\n"
        "SELECT 1 AS id;\n"
        "---\n"
        "  ---\n"
        "-- ---\n"
        "---- \n"
        "--- description\n"
    )
    content = result.files[0].content
    assert "\n-- ---\n" in content
    assert content.count("\n-- ---\n") >= 2
    assert "\n-- ---\n---- \n--- description" in content


def test_string_literals_with_sqlx_markers_are_replaced_as_whole_tokens() -> None:
    result = convert_string(
        "CREATE TABLE d.literal_forms AS SELECT "
        "'${single}' AS s, "
        '"${double}" AS d, '
        "r'''${raw_triple}''' AS rt, "
        'B"${bytes}" AS b, '
        "RB'${raw_bytes}' AS rb;"
    )
    content = result.files[0].content
    assert "${\"'${single}'\"}" in content
    assert '${"\\"${double}\\""}' in content
    assert "${\"r'''${raw_triple}'''\"}" in content
    assert '${"B\\"${bytes}\\""}' in content
    assert "${\"RB'${raw_bytes}'\"}" in content
    assert '\'${"${"}' not in content


def test_source_annotation_cannot_be_broken_by_filename_newlines() -> None:
    result = convert_string("SELECT 1;", name="evil\n${danger}.sql")
    content = result.files[0].content
    assert "-- source: evil\\x0a${danger}.sql:1" in content
    assert "\n${danger}.sql:1" not in content


def test_write_result_rejects_generated_path_traversal(tmp_path: Path) -> None:
    result = convert_string("SELECT 1;", name="../escape.sql")
    with pytest.raises(ConversionError, match="escapes"):
        write_result(result, str(tmp_path / "output"))
    assert not (tmp_path / "escape.sqlx").exists()


def test_semantic_path_rewrite_wins_over_narrower_sqlx_escape() -> None:
    result = convert_string(
        "CREATE TABLE `${project}`.d.t AS SELECT 1 AS x;"
        "CREATE TABLE d.r AS SELECT * FROM `${project}`.d.t;"
    )
    reader = _by_name(result, "r")
    assert result.report.refs_rewritten == 1
    assert '${ref({database: "${project}", schema: "d", name: "t"})}' in reader.content
    assert '${"`${project}`"}.d.t' not in reader.content


def test_merge_row_and_reordered_insert_are_not_claimed_equivalent() -> None:
    row_merge = (
        "MERGE d.t t USING (SELECT id, v FROM d.s) s ON t.id = s.id "
        "WHEN MATCHED THEN UPDATE SET v = s.v "
        "WHEN NOT MATCHED THEN INSERT ROW;"
    )
    result = convert_string(
        row_merge,
        ConversionOptions(merge_strategy=MergeStrategy.INCREMENTAL_WHEN_SAFE),
    )
    assert result.files[0].action_type.value == "operations"
    assert "MERGE_FALLBACK" in _codes(result)

    reordered = (
        "MERGE d.t t USING (SELECT v, id FROM d.s) s ON t.id = s.id "
        "WHEN MATCHED THEN UPDATE SET v = s.v "
        "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (s.id, s.v);"
    )
    result = convert_string(
        reordered,
        ConversionOptions(merge_strategy=MergeStrategy.INCREMENTAL_WHEN_SAFE),
    )
    assert result.files[0].action_type.value == "operations"

    duplicate_key = (
        "MERGE d.t t USING (SELECT id, v FROM d.s) s "
        "ON t.id = s.id AND t.id = s.id "
        "WHEN MATCHED THEN UPDATE SET v = s.v "
        "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (s.id, s.v);"
    )
    result = convert_string(
        duplicate_key,
        ConversionOptions(merge_strategy=MergeStrategy.INCREMENTAL_WHEN_SAFE),
    )
    assert result.files[0].action_type.value == "operations"

    alias_collision = (
        "MERGE d.t t USING (SELECT id, v FROM d.s) t ON t.id = t.id "
        "WHEN MATCHED THEN UPDATE SET v = t.v "
        "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (t.id, t.v);"
    )
    result = convert_string(
        alias_collision,
        ConversionOptions(merge_strategy=MergeStrategy.INCREMENTAL_WHEN_SAFE),
    )
    assert result.files[0].action_type.value == "operations"


def test_safe_merge_requires_exact_runtime_update_semantics() -> None:
    missing_key_update = (
        "MERGE d.t t USING (SELECT id, v FROM d.s) s ON t.id = s.id "
        "WHEN MATCHED THEN UPDATE SET v = s.v "
        "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (s.id, s.v);"
    )
    result = convert_string(
        missing_key_update,
        ConversionOptions(merge_strategy=MergeStrategy.INCREMENTAL_WHEN_SAFE),
    )
    assert result.files[0].action_type.value == "operations"
    assert "MERGE_FALLBACK" in _codes(result)

    quoted_key = (
        "MERGE d.t t USING (SELECT id AS `odd-key`, v FROM d.s) s "
        "ON t.`odd-key` = s.`odd-key` "
        "WHEN MATCHED THEN UPDATE SET "
        "`odd-key` = s.`odd-key`, v = s.v "
        "WHEN NOT MATCHED THEN INSERT (`odd-key`, v) "
        "VALUES (s.`odd-key`, s.v);"
    )
    result = convert_string(
        quoted_key,
        ConversionOptions(merge_strategy=MergeStrategy.INCREMENTAL_WHEN_SAFE),
    )
    assert result.files[0].action_type.value == "operations"
    assert "MERGE_FALLBACK" in _codes(result)

    exact = missing_key_update.replace(
        "UPDATE SET v = s.v",
        "UPDATE SET id = s.id, v = s.v",
    )
    result = convert_string(
        exact,
        ConversionOptions(merge_strategy=MergeStrategy.INCREMENTAL_WHEN_SAFE),
    )
    assert result.files[0].action_type.value == "incremental"


def test_incremental_runtime_column_names_are_exact_and_quoteable() -> None:
    for sql in (
        "INSERT INTO d.t SELECT * FROM d.source;",
        "INSERT INTO d.t SELECT 1;",
        "INSERT INTO d.t SELECT a, a FROM d.source;",
    ):
        result = convert_string(
            sql,
            ConversionOptions(insert_strategy=InsertStrategy.INCREMENTAL),
        )
        assert result.files[0].action_type.value == "operations"
        assert "FALLBACK_SELECT_ALIAS" in _codes(result)

    # Dataform backtick-quotes ordinary incremental INSERT columns, so
    # punctuation and reserved words are valid when the names are known.
    for sql in (
        "INSERT INTO d.t (`odd-name`) SELECT 1;",
        "INSERT INTO d.t SELECT 1 AS `odd-name`;",
        "INSERT INTO d.t SELECT 1 AS `GROUP`;",
    ):
        result = convert_string(
            sql,
            ConversionOptions(insert_strategy=InsertStrategy.INCREMENTAL),
        )
        assert result.files[0].action_type.value == "incremental"

    reserved_merge = (
        "MERGE d.t t USING (SELECT id AS `GROUP` FROM d.s) s "
        "ON t.`GROUP` = s.`GROUP` "
        "WHEN MATCHED THEN UPDATE SET `GROUP` = s.`GROUP` "
        "WHEN NOT MATCHED THEN INSERT (`GROUP`) VALUES (s.`GROUP`);"
    )
    result = convert_string(
        reserved_merge,
        ConversionOptions(merge_strategy=MergeStrategy.INCREMENTAL_WHEN_SAFE),
    )
    assert result.files[0].action_type.value == "operations"
    assert "MERGE_FALLBACK" in _codes(result)


def test_non_finite_option_number_cannot_break_generated_javascript() -> None:
    result = convert_string(
        "CREATE TABLE d.t OPTIONS(partition_expiration_days=1e9999) " "AS SELECT 1 AS x;"
    )
    content = result.files[0].content
    assert "partitionExpirationDays: inf" not in content
    assert 'partition_expiration_days: "1e9999"' in content


def test_reader_does_not_depend_on_a_future_writer() -> None:
    result = convert_string(
        "CREATE TABLE d.t AS SELECT 1 AS x;"
        "CREATE TABLE d.r AS SELECT * FROM d.t;"
        "UPDATE d.t SET x = 2 WHERE TRUE;"
    )
    reader = _by_name(result, "r")
    future_writer = _by_name(result, "t_update")
    assert '${ref("d", "t")}' in reader.content
    assert "t_update" not in reader.content
    assert "d.r" in future_writer.content


def test_reader_does_not_ref_a_future_creator() -> None:
    result = convert_string("SELECT * FROM d.t;" "CREATE TABLE d.t AS SELECT 1 AS x;")
    reader = _by_name(result, "input")
    assert '${ref("d", "t")}' not in reader.content
    assert "FROM d.t" in reader.content
    assert "FUTURE_CREATOR" in _codes(result)
    assert 'dependencies: ["input"]' in _by_name(result, "t").content


def test_mutual_reads_preserve_corpus_order_without_a_cycle() -> None:
    result = convert_string(
        "CREATE TABLE d.a AS SELECT * FROM d.b;" "CREATE TABLE d.b AS SELECT * FROM d.a;"
    )
    assert result.report.refs_rewritten == 1
    assert "FUTURE_CREATOR" in _codes(result)
    bodies = [_by_name(result, name).content for name in ("a", "b")]
    assert sum('${ref("d",' in body for body in bodies) == 1


def test_cross_file_dependency_cycle_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "sql"
    source.mkdir()
    (source / "a.sql").write_text("CREATE TABLE d.a AS SELECT * FROM d.b;", encoding="utf-8")
    (source / "b.sql").write_text("CREATE TABLE d.b AS SELECT * FROM d.a;", encoding="utf-8")
    result = convert_directory(str(source), options=ConversionOptions(jobs=1))
    assert result.report.refs_rewritten == 1
    assert "DEPENDENCY_CYCLE" in _codes(result)


def test_explicit_cross_project_dependency_is_fully_qualified() -> None:
    result = convert_string(
        "CREATE TABLE `project-a`.d.t AS SELECT 1 AS x;"
        "UPDATE `project-a`.d.t SET x = 2 WHERE TRUE;"
    )
    update = _by_name(result, "t_update")
    assert 'dependencies: ["project-a.d.t"]' in update.content


def test_string_enum_options_are_normalized_and_invalid_jobs_rejected() -> None:
    options = ConversionOptions(insert_strategy="operations")  # type: ignore[arg-type]
    assert options.insert_strategy is InsertStrategy.OPERATIONS
    with pytest.raises(ValueError, match="jobs"):
        ConversionOptions(jobs=-1)
    with pytest.raises(ValueError, match="tags"):
        ConversionOptions(tags="not-a-list")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="encoding"):
        ConversionOptions(encoding="definitely-not-a-codec")
    with pytest.raises(ValueError, match="text codec"):
        ConversionOptions(encoding="base64_codec")
    with pytest.raises(ValueError, match="boolean"):
        ConversionOptions(annotate="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="input directory"):
        ConversionOptions(include_glob="../*.sql")


def test_generated_file_stems_are_portably_bounded() -> None:
    stem = sanitize_filename("table-" * 100)
    assert len(stem) <= 120
    assert stem == sanitize_filename("table-" * 100)
    assert sanitize_filename("CON") == "_CON"

    result = convert_string("CREATE TABLE d.Foo AS SELECT 1;" "CREATE TABLE d.foo AS SELECT 2;")
    relpaths = [file.relpath for file in result.files]
    assert len({path.casefold() for path in relpaths}) == 2


def test_many_unnamed_actions_receive_unique_stable_names() -> None:
    result = convert_string(";".join("SELECT 1" for _ in range(500)))
    names = [file.action_name for file in result.files]
    assert len(names) == len(set(names)) == 500
    assert {"input", "input_2", "input_500"}.issubset(names)


def test_merge_branch_keywords_do_not_register_phantom_script_writes() -> None:
    # Inside a script, `WHEN NOT MATCHED THEN INSERT ROW` / `INSERT VALUES`
    # are branches of the MERGE, not independent INSERT statements; they
    # must not register phantom write targets named ROW or VALUES.
    result = convert_string(
        "DECLARE x INT64;\n"
        "MERGE ds.t T USING (SELECT 1 AS id, 2 AS a) S ON T.id = S.id\n"
        "WHEN MATCHED THEN UPDATE SET a = S.a\n"
        "WHEN NOT MATCHED THEN INSERT ROW;\n"
        "MERGE INTO ds.u T USING (SELECT 1 AS id, 2 AS a) S ON T.id = S.id\n"
        "WHEN NOT MATCHED THEN INSERT VALUES (S.id, S.a);\n"
    )
    [script_writes] = [
        warning.message for warning in result.report.warnings if warning.code == "SCRIPT_WRITES"
    ]
    assert "ds.t" in script_writes and "ds.u" in script_writes
    assert "ROW" not in script_writes and "VALUES" not in script_writes


def test_writes_after_a_merge_in_the_same_block_are_still_tracked() -> None:
    result = convert_string(
        "DECLARE x INT64;\n"
        "BEGIN\n"
        "  MERGE ds.t T USING (SELECT 1 AS id) S ON T.id = S.id\n"
        "  WHEN NOT MATCHED THEN INSERT ROW;\n"
        "  INSERT INTO ds.second SELECT 1;\n"
        "END;\n"
    )
    [script_writes] = [
        warning.message for warning in result.report.warnings if warning.code == "SCRIPT_WRITES"
    ]
    assert "ds.t" in script_writes and "ds.second" in script_writes
    assert "ROW" not in script_writes


def test_script_write_scan_uses_block_aware_statement_positions() -> None:
    result = convert_string(
        "DECLARE guard BOOL DEFAULT TRUE;\n"
        "SELECT update phantom FROM d.source;\n"
        "BEGIN\n"
        "  SELECT CASE WHEN guard THEN update phantom ELSE 0 END;\n"
        "  UPDATE d.real_target SET x = 1 WHERE TRUE;\n"
        "END;\n"
    )
    [script_writes] = [
        warning.message for warning in result.report.warnings if warning.code == "SCRIPT_WRITES"
    ]
    assert "d.real_target" in script_writes
    assert "phantom" not in script_writes


def test_assert_keeps_guarded_statements_in_one_script() -> None:
    result = convert_string(
        "INSERT INTO d.before_guard SELECT 1;\n"
        "ASSERT FALSE AS 'stop';\n"
        "INSERT INTO d.must_not_run SELECT 2;\n"
    )
    assert len(result.files) == 1
    assert result.files[0].action_type.value == "operations"
    assert "ASSERT FALSE" in result.files[0].content
    assert "d.must_not_run" in result.files[0].content
    assert "SCRIPT_FILE" in _codes(result)


def test_alter_rename_orders_readers_of_the_new_name() -> None:
    result = convert_string(
        "ALTER TABLE d.old_name RENAME TO new_name;\n"
        "CREATE TABLE d.reader AS SELECT * FROM d.new_name;"
    )
    rename = _by_name(result, "old_name_alter")
    reader = _by_name(result, "reader")
    assert rename.action_name in reader.content
    assert "FROM d.new_name" in reader.content


def test_top_level_return_keeps_the_whole_file_as_one_script() -> None:
    # RETURN ends a BigQuery script; splitting the file into independent
    # actions would unconditionally run statements the original script
    # never reached.
    result = convert_string(
        "INSERT INTO ds.a SELECT 1;\n" "RETURN;\n" "INSERT INTO ds.b SELECT 2;\n"
    )
    assert len(result.files) == 1
    assert result.files[0].action_type.value == "operations"
    content = result.files[0].content
    assert "RETURN;" in content and "ds.b" in content
    assert "SCRIPT_FILE" in _codes(result)


def test_merge_into_session_temp_table_stays_script_local() -> None:
    # An explicitly qualified `_SESSION` MERGE target is script-local: it
    # must not surface as a persistent script write or join writer chains.
    result = convert_string(
        "DECLARE x INT64;\n"
        "CREATE TEMP TABLE tmp AS SELECT 1 AS id;\n"
        "MERGE _SESSION.tmp T USING (SELECT 1 AS id) S ON T.id = S.id\n"
        "WHEN MATCHED THEN UPDATE SET id = S.id\n"
        "WHEN NOT MATCHED THEN INSERT ROW;\n"
        "INSERT INTO ds.persist SELECT * FROM tmp;\n"
    )
    [script_writes] = [
        warning.message for warning in result.report.warnings if warning.code == "SCRIPT_WRITES"
    ]
    assert "ds.persist" in script_writes
    assert "tmp" not in script_writes and "_SESSION" not in script_writes


def test_truncated_script_statement_heads_never_crash_conversion() -> None:
    # Script statement slices are not EOF-terminated, so a truncated final
    # statement used to push ``parse_table_path`` past the end of the token
    # list (IndexError) from three separate scanners: the UPDATE/DELETE/MERGE
    # write-target alias exclusion, the CLONE/LIKE DDL read scan, and the
    # ALTER ... RENAME TO target parse.  A directory conversion must isolate
    # such a file instead of aborting the whole corpus.
    for tail in (
        "DELETE",
        "UPDATE",
        "MERGE",
        "MERGE INTO",
        "CREATE TABLE",
        "CREATE TABLE t LIKE",
        "ALTER TABLE t RENAME TO",
    ):
        result = convert_string(f"IF x THEN SELECT 1; END IF; {tail}")
        assert not result.report.failures
        assert [file.action_type.value for file in result.files] == ["operations"]
        assert tail in result.files[0].content


def test_parse_table_path_is_total_over_token_indexes() -> None:
    tokens = tokenize("d.t")
    assert parse_table_path(tokens, len(tokens)) is None
    assert parse_table_path(tokens, -1) is None
    match = parse_table_path(tokens, 0)
    assert match is not None and match.parts == ["d", "t"]
