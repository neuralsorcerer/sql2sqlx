# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""Large-scale SQL conversion scenarios covering broad GoogleSQL edge cases."""

from __future__ import annotations

from pathlib import Path

from sql2sqlx import ConversionOptions, InsertStrategy, MergeStrategy, convert_directory


def _contents(out: Path) -> dict[str, str]:
    return {
        path.relative_to(out).as_posix(): path.read_text() for path in sorted(out.rglob("*.sqlx"))
    }


def test_large_directory_conversion_preserves_dependencies_metadata_and_fallbacks(
    tmp_path: Path,
) -> None:
    src = tmp_path / "sql"
    out = tmp_path / "definitions"
    for folder in ("00_sources", "10_staging", "20_marts", "30_ops", "40_security"):
        (src / folder).mkdir(parents=True)

    (src / "00_sources" / "raw.sql").write_text(
        """
        CREATE TABLE IF NOT EXISTS `proj-prod.raw.events` (
          id INT64,
          payload JSON,
          event_ts TIMESTAMP,
          repeated ARRAY<STRUCT<sku STRING, qty INT64>>
        )
        PARTITION BY DATE(event_ts)
        CLUSTER BY id
        OPTIONS(
          description = 'raw event stream',
          labels = [('domain', 'commerce'), ('tier', 'bronze')],
          require_partition_filter = true
        );

        CREATE TABLE IF NOT EXISTS `proj-prod.raw.customers` (
          customer_id STRING,
          email STRING,
          updated_at TIMESTAMP
        );
        """,
        encoding="utf-8",
    )
    (src / "10_staging" / "events.sql").write_text(
        """
        CREATE OR REPLACE TABLE `proj-prod.staging.events`
        PARTITION BY DATE(event_ts)
        CLUSTER BY customer_id, event_name
        OPTIONS(description = 'typed event facts') AS
        WITH source AS (
          SELECT
            id,
            JSON_VALUE(payload, '$.customer.id') AS customer_id,
            JSON_VALUE(payload, '$.name') AS event_name,
            event_ts,
            repeated
          FROM `proj-prod.raw.events`
          WHERE DATE(event_ts) >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
        )
        SELECT
          source.id,
          source.customer_id,
          source.event_name,
          source.event_ts,
          item.sku,
          item.qty
        FROM source, UNNEST(source.repeated) AS item;
        """,
        encoding="utf-8",
    )
    (src / "20_marts" / "customer_daily.sql").write_text(
        """
        CREATE OR REPLACE VIEW `proj-prod.marts.customer_daily`
        (customer_id OPTIONS(description = 'Customer id'), activity_date, events) AS
        SELECT
          customer_id,
          DATE(event_ts),
          COUNT(*)
        FROM `proj-prod.staging.events`
        GROUP BY 1, 2;
        """,
        encoding="utf-8",
    )
    (src / "20_marts" / "incremental.sql").write_text(
        """
        INSERT INTO `proj-prod.marts.customer_fact` (customer_id, activity_date, events)
        WITH ranked AS (
          SELECT customer_id, DATE(event_ts) AS activity_date, COUNT(*) AS events
          FROM `proj-prod.staging.events`
          GROUP BY 1, 2
        )
        SELECT customer_id, activity_date, events FROM ranked;
        """,
        encoding="utf-8",
    )
    (src / "30_ops" / "maintenance.sql").write_text(
        """
        BEGIN TRANSACTION;
        DELETE FROM `proj-prod.marts.customer_fact`
        WHERE activity_date < DATE_SUB(CURRENT_DATE(), INTERVAL 400 DAY);
        INSERT INTO `proj-prod.audit.maintenance_log`
        SELECT CURRENT_TIMESTAMP(), 'customer_fact_retention';
        COMMIT TRANSACTION;
        """,
        encoding="utf-8",
    )
    (src / "30_ops" / "ddl.sql").write_text(
        """
        CREATE TABLE `proj-prod.snapshots.events_clone`
        CLONE `proj-prod.staging.events`;
        CREATE SEARCH INDEX events_idx
        ON `proj-prod.staging.events`(ALL COLUMNS);
        """,
        encoding="utf-8",
    )
    (src / "40_security" / "policy.sql").write_text(
        """
        CREATE ROW ACCESS POLICY active_customers
        ON `proj-prod.marts.customer_fact`
        GRANT TO ('group:analysts@example.com')
        FILTER USING (customer_id IS NOT NULL);
        GRANT SELECT ON TABLE `proj-prod.marts.customer_fact`
        TO 'group:analysts@example.com';
        """,
        encoding="utf-8",
    )

    result = convert_directory(
        str(src),
        str(out),
        ConversionOptions(
            jobs=2,
            insert_strategy=InsertStrategy.INCREMENTAL,
            merge_strategy=MergeStrategy.INCREMENTAL_WHEN_SAFE,
        ),
    )

    assert not result.report.failures
    assert result.report.files_read == 7
    assert result.report.statements == 13
    assert result.report.refs_rewritten >= 4
    assert result.report.actions_by_type["table"] == 1
    assert result.report.actions_by_type["view"] == 1
    assert result.report.actions_by_type["incremental"] == 1
    files = _contents(out)
    assert set(files) == {
        "00_sources/customers.sqlx",
        "00_sources/events.sqlx",
        "10_staging/events.sqlx",
        "20_marts/customer_daily.sqlx",
        "20_marts/customer_fact.sqlx",
        "30_ops/events_clone.sqlx",
        "30_ops/events_create.sqlx",
        "30_ops/maintenance.sqlx",
        "40_security/customer_fact_create.sqlx",
        "40_security/customer_fact_grant.sqlx",
    }
    staging = files["10_staging/events.sqlx"]
    assert 'type: "table"' in staging
    assert 'partitionBy: "DATE(event_ts)"' in staging
    assert 'clusterBy: ["customer_id", "event_name"]' in staging
    assert '${ref({database: "proj-prod", schema: "raw", name: "events"})}' in staging
    assert "UNNEST(source.repeated) AS item" in staging
    view = files["20_marts/customer_daily.sqlx"]
    assert "columns: {" in view
    assert 'customer_id: "Customer id"' in view
    assert "DATE(event_ts) AS activity_date" in view
    assert "COUNT(*) AS events" in view
    assert '${ref({database: "proj-prod", schema: "staging", name: "events"})}' in view
    incremental = files["20_marts/customer_fact.sqlx"]
    assert 'type: "incremental"' in incremental
    assert "dependencies" not in incremental
    assert '${ref({database: "proj-prod", schema: "staging", name: "events"})}' in incremental
    maintenance = files["30_ops/maintenance.sqlx"]
    assert "BEGIN TRANSACTION" in maintenance and "COMMIT TRANSACTION" in maintenance
    assert 'dependencies: ["proj-prod.marts.customer_fact"]' in maintenance
    clone = files["30_ops/events_clone.sqlx"]
    assert 'CLONE ${ref({database: "proj-prod", schema: "staging", name: "events"})}' in clone
    policy = files["40_security/customer_fact_create.sqlx"]
    assert 'dependencies: ["maintenance"]' in policy
    codes = {warning.code for warning in result.report.warnings}
    assert {
        "SCRIPT_FILE",
        "SCRIPT_WRITES",
        "FALLBACK_OPERATIONS",
        "INDEX_DDL",
        "ROW_ACCESS_POLICY_DDL",
        "GRANT_REVOKE_DCL",
    } <= codes


def test_large_single_file_script_handles_nested_blocks_ctes_pipes_and_literals(
    tmp_path: Path,
) -> None:
    src = tmp_path / "sql"
    out = tmp_path / "definitions"
    src.mkdir()
    statements = [
        "CREATE TABLE d.seed AS SELECT 1 AS id, ['a', 'b'] AS tags;",
        "CREATE OR REPLACE TABLE d.pipe AS FROM d.seed |> SELECT id, tag FROM UNNEST(tags) AS tag;",
        "CREATE OR REPLACE VIEW d.quoted AS WITH `local-rows` AS (SELECT * FROM d.pipe) SELECT * FROM `local-rows`;",
    ]
    for i in range(30):
        statements.append(
            f"CREATE OR REPLACE TABLE d.chain_{i} AS "
            f"SELECT id + {i} AS id, r'''not;a;split''' AS raw_literal "
            f"FROM {'d.quoted' if i == 0 else f'd.chain_{i - 1}'};"
        )
    (src / "models.sql").write_text("\n".join(statements), encoding="utf-8")
    (src / "script.sql").write_text(
        """
        DECLARE cutoff INT64 DEFAULT 10;
        BEGIN
          IF cutoff > 0 THEN
            MERGE d.chain_29 T
            USING (SELECT id FROM d.seed) S
            ON T.id = S.id
            WHEN MATCHED THEN UPDATE SET id = S.id
            WHEN NOT MATCHED THEN INSERT (id) VALUES (S.id);
          END IF;
        END;
        """,
        encoding="utf-8",
    )

    result = convert_directory(str(src), str(out), ConversionOptions(jobs=1))

    assert not result.report.failures
    assert len(result.files) == 34
    assert result.report.refs_rewritten == 33
    assert result.report.actions_by_type["table"] == 32
    assert result.report.actions_by_type["view"] == 1
    assert result.report.actions_by_type["operations"] == 1
    rendered = {file.action_name: file.content for file in result.files}
    assert '${ref("d", "seed")}' in rendered["pipe"]
    assert '${ref("d", "pipe")}' in rendered["quoted"]
    assert "r'''not;a;split'''" in rendered["chain_0"]
    assert '${ref("d", "chain_28")}' in rendered["chain_29"]
    assert 'dependencies: ["d.chain_29"]' in rendered["script"]
    assert "MERGE d.chain_29" in rendered["script"]
