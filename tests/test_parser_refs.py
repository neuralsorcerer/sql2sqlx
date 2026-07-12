# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""Classifier, reference-scanner and linker tests.

Everything here goes through :func:`sql2sqlx.convert_string` so the
assertions cover the *actual emitted files* - config blocks, rewritten
bodies, dependency wiring and warnings - not internal intermediates.
"""

from __future__ import annotations

from sql2sqlx import (
    ConversionOptions,
    IfNotExistsStrategy,
    InsertStrategy,
    MergeStrategy,
    PlainCreateStrategy,
    convert_string,
)


def conv(sql, **kw):
    """Convert with options built from keyword overrides."""
    result = convert_string(sql, ConversionOptions(**kw))
    assert not result.report.failures, result.report.failures
    return result


def by_name(result, name):
    """The generated file whose action name is ``name``."""
    for f in result.files:
        if f.action_name == name:
            return f
    raise AssertionError(
        f"no action named {name!r}; have " f"{[(f.action_name, f.relpath) for f in result.files]}"
    )


def codes(result):
    """Set of warning codes in the report."""
    return {w.code for w in result.report.warnings}


# ---------------------------------------------------------------------------
# CREATE TABLE ... AS
# ---------------------------------------------------------------------------


def test_ctas_full_metadata_mapping():
    r = conv(
        "CREATE OR REPLACE TABLE analytics.daily\n"
        "PARTITION BY DATE(order_ts)\n"
        "CLUSTER BY region, status\n"
        'OPTIONS(description="Daily rollup", labels=[("team","data")],\n'
        "        partition_expiration_days=90,\n"
        "        require_partition_filter=true,\n"
        '        kms_key_name="projects/p/keys/k")\n'
        "AS SELECT 1 AS x;"
    )
    f = by_name(r, "daily")
    assert 'type: "table"' in f.content
    assert 'schema: "analytics"' in f.content
    assert 'partitionBy: "DATE(order_ts)"' in f.content
    assert 'clusterBy: ["region", "status"]' in f.content
    assert 'description: "Daily rollup"' in f.content
    assert 'team: "data"' in f.content
    assert "partitionExpirationDays: 90" in f.content
    assert "requirePartitionFilter: true" in f.content
    # Unknown option preserved raw (with its original quotes).
    assert "additionalOptions" in f.content and "kms_key_name" in f.content
    assert f.content.rstrip().endswith("SELECT 1 AS x")


def test_temp_table_falls_back():
    r = conv("CREATE TEMP TABLE t AS SELECT 1;")
    assert r.files[0].action_type.value == "operations"
    assert "CREATE TEMP TABLE" in r.files[0].content
    assert "TEMP_TABLE" in codes(r)


def test_ctas_with_column_list_falls_back():
    r = conv("CREATE TABLE d.t (a INT64) AS SELECT 1;")
    assert r.files[0].action_type.value == "operations"
    assert "COLUMN_DDL" in codes(r)


def test_create_table_like_falls_back():
    r = conv("CREATE TABLE d.t LIKE d.other;")
    assert r.files[0].action_type.value == "operations"
    assert "FALLBACK_OPERATIONS" in codes(r)


def test_if_not_exists_strategies():
    sql = "CREATE TABLE IF NOT EXISTS d.t AS SELECT 1;"
    r = conv(sql)
    assert r.files[0].action_type.value == "operations"
    assert "IF_NOT_EXISTS" in codes(r)
    assert "IF NOT EXISTS" in r.files[0].content
    r = conv(sql, if_not_exists_strategy=IfNotExistsStrategy.TABLE)
    assert r.files[0].action_type.value == "table"


def test_plain_create_strategies():
    sql = "CREATE TABLE d.t (a INT64, b STRING);"
    r = conv(sql)
    f = r.files[0]
    assert f.action_type.value == "operations"
    # Sole writer of an un-created table: elected as its ref-able producer.
    assert "hasOutput: true" in f.content
    assert "CREATE TABLE ${self()}" in f.content
    assert {"CREATE_NO_AS", "RERUN_RISK"} <= codes(r)
    r = conv(sql, plain_create_strategy=PlainCreateStrategy.DECLARATION)
    f = r.files[0]
    assert f.action_type.value == "declaration"
    assert 'type: "declaration"' in f.content
    assert 'schema: "d"' in f.content and 'name: "t"' in f.content
    assert "SELECT" not in f.content
    assert "DECLARATION_DROPPED_DDL" in codes(r)


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def test_materialized_view():
    r = conv(
        "CREATE MATERIALIZED VIEW d.mv\n"
        "PARTITION BY DATE(ts)\n"
        "OPTIONS(enable_refresh=true)\n"
        "AS SELECT ts FROM d.src;"
    )
    f = by_name(r, "mv")
    assert 'type: "view"' in f.content
    assert "materialized: true" in f.content
    assert 'partitionBy: "DATE(ts)"' in f.content
    assert "enable_refresh" in f.content  # via additionalOptions


def test_view_column_list_aliases_select_and_documents():
    r = conv('CREATE VIEW d.v (a, b OPTIONS(description="Bee")) AS ' "SELECT x, y FROM d.s;")
    f = by_name(r, "v")
    assert "x AS a" in f.content and "y AS b" in f.content
    assert 'b: "Bee"' in f.content  # columns doc


def test_view_column_list_with_star_falls_back():
    r = conv("CREATE VIEW d.v (a) AS SELECT * FROM d.s;")
    assert r.files[0].action_type.value == "operations"
    assert "FALLBACK_SELECT_ALIAS" in codes(r)


# ---------------------------------------------------------------------------
# INSERT -> incremental
# ---------------------------------------------------------------------------


def test_insert_without_columns_becomes_incremental():
    r = conv(
        "INSERT INTO d.t SELECT id, d1 FROM d.s WHERE d1 > '2024-01-01';",
        insert_strategy=InsertStrategy.INCREMENTAL,
    )
    f = by_name(r, "t")
    assert 'type: "incremental"' in f.content
    assert "protected: true" in f.content
    assert f.content.rstrip().endswith("WHERE d1 > '2024-01-01'")
    assert "INSERT_INCREMENTAL" in codes(r)


def test_insert_column_list_aliasing_all_modes():
    r = conv(
        "INSERT INTO d.t (a, b, c, d, e) " "SELECT x AS old, s.q, 1 two, colE, f(x) FROM d.s;",
        insert_strategy=InsertStrategy.INCREMENTAL,
    )
    body = by_name(r, "t").content
    assert "x AS a" in body  # explicit alias replaced
    assert "s.q AS b" in body  # dotted path: alias appended
    assert "1 c" in body  # implicit alias replaced
    assert "colE AS d" in body  # bare column renamed
    assert "f(x) AS e" in body  # anonymous expression aliased


def test_insert_equal_names_left_untouched():
    r = conv(
        "INSERT INTO d.t (Region) SELECT region FROM d.s;",
        insert_strategy=InsertStrategy.INCREMENTAL,
    )
    body = by_name(r, "t").content
    assert "SELECT region FROM" in body
    assert "region AS" not in body


def test_insert_trailing_comma_supported():
    r = conv("INSERT INTO d.t (a) SELECT x, FROM d.s;", insert_strategy=InsertStrategy.INCREMENTAL)
    assert "x AS a," in by_name(r, "t").content


def test_insert_unsafe_select_lists_fall_back():
    for sql in (
        "INSERT INTO d.t (a) SELECT * FROM d.s;",  # star
        "INSERT INTO d.t (a) SELECT INTERVAL 1 DAY FROM d.s;",  # interval
        "INSERT INTO d.t (a, b) SELECT 1 FROM d.s;",  # arity
        "INSERT INTO d.t (a) SELECT AS STRUCT 1 FROM d.s;",  # AS STRUCT
    ):
        r = conv(sql, insert_strategy=InsertStrategy.INCREMENTAL)
        assert r.files[0].action_type.value == "operations", sql
        assert "FALLBACK_SELECT_ALIAS" in codes(r), sql


def test_insert_interval_with_explicit_alias_is_safe():
    r = conv(
        "INSERT INTO d.t (a) SELECT INTERVAL 1 DAY AS dur FROM d.s;",
        insert_strategy=InsertStrategy.INCREMENTAL,
    )
    assert "INTERVAL 1 DAY AS a" in by_name(r, "t").content


def test_insert_values_and_operations_strategy():
    r = conv("INSERT INTO d.t VALUES (1, 2);")
    assert r.files[0].action_type.value == "operations"
    assert "INSERT_VALUES" in codes(r)
    r = conv("INSERT INTO d.t SELECT 1;", insert_strategy=InsertStrategy.OPERATIONS)
    f = r.files[0]
    assert f.action_type.value == "operations"
    # Mutating DML does not satisfy Dataform's hasOutput creation contract.
    assert "hasOutput" not in f.content
    assert "INSERT INTO d.t SELECT 1" in f.content


# ---------------------------------------------------------------------------
# MERGE
# ---------------------------------------------------------------------------

SAFE_MERGE = (
    "CREATE TABLE d.src AS SELECT 1 AS id, 2 AS amount;\n"
    "MERGE d.tgt t USING (\n"
    "  SELECT id, amount FROM d.src\n"
    ") s ON t.id = s.id\n"
    "WHEN MATCHED THEN UPDATE SET id = s.id, amount = s.amount\n"
    "WHEN NOT MATCHED THEN INSERT (id, amount) VALUES (s.id, s.amount);"
)


def test_merge_default_stays_operations():
    r = conv(SAFE_MERGE)
    f = by_name(r, "tgt_merge")
    assert f.action_type.value == "operations"
    assert "hasOutput" not in f.content
    assert "MERGE d.tgt t USING" in f.content


def test_merge_safe_mode_converts_to_incremental():
    r = conv(SAFE_MERGE, merge_strategy=MergeStrategy.INCREMENTAL_WHEN_SAFE)
    f = by_name(r, "tgt")
    assert 'type: "incremental"' in f.content
    assert 'uniqueKey: ["id"]' in f.content
    # Body is exactly the USING subquery, with the source ref rewritten.
    assert 'FROM ${ref("d", "src")}' in f.content
    assert "MERGE" not in f.content.split("config", 1)[1].split("--")[0]
    assert "MERGE_INCREMENTAL" in codes(r)
    assert "TARGET_SCHEMA_REQUIRED" in codes(r)


def test_merge_insert_row_variant():
    sql = (
        "MERGE d.tgt t USING (SELECT id, v FROM d.src) s ON t.id = s.id\n"
        "WHEN MATCHED THEN UPDATE SET v = s.v\n"
        "WHEN NOT MATCHED THEN INSERT ROW;"
    )
    r = conv(sql, merge_strategy=MergeStrategy.INCREMENTAL_WHEN_SAFE)
    assert r.files[0].action_type.value == "operations"
    assert "MERGE_FALLBACK" in codes(r)


def test_merge_unsafe_reasons_fall_back():
    cases = {
        # extra predicate in ON
        "MERGE d.t t USING (SELECT id, v FROM d.s) s "
        "ON t.id = s.id AND t.v > 1 "
        "WHEN MATCHED THEN UPDATE SET v = s.v "
        "WHEN NOT MATCHED THEN INSERT ROW;": "MERGE_FALLBACK",
        # updates an expression, not a source column
        "MERGE d.t t USING (SELECT id, v FROM d.s) s ON t.id = s.id "
        "WHEN MATCHED THEN UPDATE SET v = s.v + 1 "
        "WHEN NOT MATCHED THEN INSERT ROW;": "MERGE_FALLBACK",
        # source columns exceed keys + updated columns
        "MERGE d.t t USING (SELECT id, v, extra FROM d.s) s ON t.id = s.id "
        "WHEN MATCHED THEN UPDATE SET v = s.v "
        "WHEN NOT MATCHED THEN INSERT ROW;": "MERGE_FALLBACK",
    }
    for sql, code in cases.items():
        r = conv(sql, merge_strategy=MergeStrategy.INCREMENTAL_WHEN_SAFE)
        assert r.files[0].action_type.value == "operations", sql
        assert code in codes(r), sql


# ---------------------------------------------------------------------------
# Reference rewriting
# ---------------------------------------------------------------------------


def test_cte_alias_and_extract_guards():
    r = conv(
        "CREATE TABLE d.orders AS SELECT 1 AS id;\n"
        "CREATE TABLE t.arr AS SELECT 1 AS id;\n"
        "CREATE TABLE order_date AS SELECT 1 AS id;\n"
        "CREATE TABLE d.reader AS\n"
        "WITH orders AS (SELECT 2 AS id)\n"
        "SELECT EXTRACT(DAY FROM order_date) AS dd, o.id\n"
        "FROM d.x t, t.arr, orders o;\n"
        "CREATE TABLE d.reader2 AS SELECT * FROM d.orders;"
    )
    reader = by_name(r, "reader").content
    # CTE `orders`, alias path `t.arr`, and EXTRACT's FROM stay literal.
    assert "FROM d.x t, t.arr, orders o" in reader
    assert "EXTRACT(DAY FROM order_date)" in reader
    assert "${ref" not in reader
    assert "d.x" in r.report.refs_unresolved
    # ...while a real read of the produced table is rewritten.
    assert '${ref("d", "orders")}' in by_name(r, "reader2").content


def test_join_comma_unnest_and_tvf_contexts():
    r = conv(
        "CREATE TABLE d.a AS SELECT 1 AS id, [] AS arr;\n"
        "CREATE TABLE d.b AS SELECT 1 AS id;\n"
        "CREATE TABLE d.out AS\n"
        "SELECT * FROM d.a x\n"
        "JOIN d.b ON x.id = b.id\n"
        "CROSS JOIN UNNEST(x.arr) AS u,\n"
        "my_tvf(1) f;"
    )
    body = by_name(r, "out").content
    assert 'FROM ${ref("d", "a")} x' in body
    assert 'JOIN ${ref("d", "b")}' in body
    assert "UNNEST(x.arr)" in body  # untouched
    assert "my_tvf(1) f" in body  # table function untouched
    assert r.report.refs_rewritten >= 2


def test_backtick_and_default_dataset_resolution():
    r = conv(
        "CREATE TABLE t AS SELECT 1 AS id;\n"
        "CREATE TABLE d.reader AS SELECT * FROM `d.t2`;\n"
        "CREATE TABLE d.t2 AS SELECT * FROM d.t;",
        default_dataset="d",
    )
    # single-part producer `t` resolved as d.t via the default dataset
    assert 'FROM ${ref("t")}' in by_name(r, "t2").content
    # A future owner in the same SQL file is not pulled ahead of this read.
    assert '${ref("d", "t2")}' not in by_name(r, "reader").content
    assert "FROM `d.t2`" in by_name(r, "reader").content
    assert "FUTURE_CREATOR" in codes(r)


def test_self_reference_stays_literal_with_warning():
    r = conv("CREATE OR REPLACE TABLE d.t AS SELECT * FROM d.t WHERE x;")
    body = by_name(r, "t").content
    assert "FROM d.t" in body and "${ref" not in body
    assert "SELF_REFERENCE" in codes(r)


def test_dollar_brace_is_escaped():
    r = conv("CREATE TABLE d.t AS SELECT '${literal}' AS v;")
    assert "${\"'${literal}'\"}" in by_name(r, "t").content


def test_delete_target_not_treated_as_read():
    r = conv(
        "CREATE TABLE d.t AS SELECT 1 AS id;\n"
        "CREATE TABLE d.bad AS SELECT 2 AS id;\n"
        "DELETE FROM d.t WHERE id IN (SELECT id FROM d.bad);"
    )
    op = by_name(r, "t_delete").content
    assert "DELETE FROM d.t" in op  # target literal
    assert '(SELECT id FROM ${ref("d", "bad")})' in op  # subquery rewritten


# ---------------------------------------------------------------------------
# Linker: chains, election, demotion, declarations, scripts
# ---------------------------------------------------------------------------


def test_writer_chain_and_reader_last_writer_dependency():
    r = conv(
        "CREATE TABLE d.t AS SELECT 1 AS x;\n"
        "UPDATE d.t SET x = 2 WHERE TRUE;\n"
        "CREATE TABLE d.r AS SELECT * FROM d.t;"
    )
    update = by_name(r, "t_update").content
    assert 'dependencies: ["d.t"]' in update
    reader = by_name(r, "r").content
    assert '${ref("d", "t")}' in reader
    assert '"t_update"' in reader  # reader ordered after its latest prior writer


def test_mutating_merge_is_not_elected_as_output():
    r = conv(
        "MERGE INTO d.m USING (SELECT 1 AS id) s ON m.id = s.id\n"
        "WHEN MATCHED THEN UPDATE SET id = s.id\n"
        "WHEN NOT MATCHED THEN INSERT ROW;"
    )
    f = by_name(r, "m_merge")
    assert "hasOutput" not in f.content
    assert "MERGE INTO d.m" in f.content


def test_duplicate_creator_demoted_and_chained():
    r = conv(
        "INSERT INTO d.t SELECT 1 AS x;\n" "INSERT INTO d.t SELECT 2 AS x;",
        insert_strategy=InsertStrategy.INCREMENTAL,
    )
    first = by_name(r, "t")
    assert 'type: "incremental"' in first.content
    second = by_name(r, "t_insert")
    assert 'type: "operations"' in second.content
    assert "INSERT INTO d.t SELECT 2" in second.content
    assert 'dependencies: ["d.t"]' in second.content
    assert "DUPLICATE_TARGET" in codes(r)


def test_declare_external_generates_declarations():
    r = conv(
        "CREATE TABLE d.out AS "
        "SELECT * FROM extds.tbl "
        "JOIN d.x.INFORMATION_SCHEMA.COLUMNS c ON TRUE;",
        declare_external=True,
    )
    decls = [f for f in r.files if f.action_type.value == "declaration"]
    assert [d.action_name for d in decls] == ["tbl"]
    assert decls[0].relpath == "sources/tbl.sqlx"
    assert '${ref("extds", "tbl")}' in by_name(r, "out").content


def test_declare_script_mode_keeps_whole_file():
    r = conv(
        "DECLARE run_date DATE DEFAULT CURRENT_DATE();\n"
        "SET run_date = DATE '2024-01-01';\n"
        "DELETE FROM d.t WHERE dt = run_date;\n"
        "INSERT INTO d.t SELECT * FROM d.s WHERE dt = run_date;",
    )
    assert len(r.files) == 1
    f = r.files[0]
    assert f.action_type.value == "operations"
    assert f.action_name == "input"
    assert "DECLARE run_date" in f.content
    assert "DELETE FROM d.t" in f.content and "INSERT INTO d.t" in f.content
    assert {"SCRIPT_FILE", "SCRIPT_WRITES"} <= codes(r)


def test_orphan_select_and_unknown_statement():
    r = conv("SELECT 1;\nGRANT `roles/bigquery.dataViewer` " "ON TABLE d.t TO 'user:a@b.c';")
    assert all(f.action_type.value == "operations" for f in r.files)
    assert "ORPHAN_SELECT" in codes(r)


def test_no_protected_option():
    r = conv(
        "INSERT INTO d.t SELECT 1 AS x;",
        protect_incrementals=False,
        insert_strategy=InsertStrategy.INCREMENTAL,
    )
    assert "protected" not in by_name(r, "t").content


def test_tags_and_no_annotate():
    r = conv("CREATE TABLE d.t AS SELECT 1;", tags=["migrated", "batch1"], annotate=False)
    f = by_name(r, "t")
    assert 'tags: ["migrated", "batch1"]' in f.content
    assert "-- source:" not in f.content
