"""Unit tests for GenericSQLBuilder AST compilation and transpilation (Detroit-style)."""

import json
from pathlib import Path

import pytest
import sqlglot

from deterministic_text_to_sql.compiler import (
    DEFAULT_TPCH_HIERARCHY_RULES,
    FilterSlot,
    GenericSQLBuilder,
    MetricSlot,
    OntologyDeduplicator,
    OrderBySlot,
    QueryIntent,
    SQLBuildError,
)
from deterministic_text_to_sql.graph import SchemaGraph
from deterministic_text_to_sql.schema import SchemaMetadata, load_schema_from_json

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "tpch_schema.json"


@pytest.fixture
def tpch_schema() -> SchemaMetadata:
    """Fixture providing loaded TPC-H SchemaMetadata."""
    return load_schema_from_json(FIXTURE_PATH)


@pytest.fixture
def schema_graph(tpch_schema: SchemaMetadata) -> SchemaGraph:
    """Fixture providing initialized SchemaGraph for TPC-H."""
    return SchemaGraph(tpch_schema)


@pytest.fixture
def deduplicator() -> OntologyDeduplicator:
    """Fixture providing OntologyDeduplicator with standard TPC-H hierarchy rules."""
    return OntologyDeduplicator(DEFAULT_TPCH_HIERARCHY_RULES)


@pytest.fixture
def sql_builder(schema_graph: SchemaGraph, deduplicator: OntologyDeduplicator) -> GenericSQLBuilder:
    """Fixture providing GenericSQLBuilder configured with graph and deduplicator."""
    return GenericSQLBuilder(schema_graph=schema_graph, deduplicator=deduplicator)


@pytest.fixture
def raw_builder(schema_graph: SchemaGraph) -> GenericSQLBuilder:
    """Fixture providing GenericSQLBuilder without deduplicator."""
    return GenericSQLBuilder(schema_graph=schema_graph, deduplicator=None)


class TestSingleTableAggregation:
    """Tests for single table queries with aggregations and filters."""

    def test_single_table_sum_with_filter(self, sql_builder: GenericSQLBuilder) -> None:
        intent = QueryIntent(
            primary_table="orders",
            metrics=[("o_totalprice", "SUM")],
            filters=[
                FilterSlot(
                    table="orders",
                    column="o_orderstatus",
                    operator="=",
                    value="F",
                )
            ],
        )

        sql = sql_builder.build(intent, dialect="postgres")

        parsed = sqlglot.parse_one(sql, read="postgres")
        assert parsed is not None
        assert "orders" in sql
        assert "SUM(orders.o_totalprice)" in sql or "SUM(o_totalprice)" in sql
        assert "orders.o_orderstatus = 'F'" in sql
        assert "JOIN" not in sql
        assert "GROUP BY" not in sql

    def test_single_table_with_metric_slot_and_alias(self, sql_builder: GenericSQLBuilder) -> None:
        intent = QueryIntent(
            primary_table="orders",
            metrics=[MetricSlot(column="o_totalprice", agg="SUM", alias="total_revenue")],
            filters=[
                FilterSlot(
                    table="orders",
                    column="o_totalprice",
                    operator=">=",
                    value=1000,
                )
            ],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "AS total_revenue" in sql
        assert "orders.o_totalprice >= 1000" in sql


class TestMultiHopJoinAggregation:
    """Tests for complex queries requiring multi-hop joins across relations."""

    def test_customer_nation_region_aggregation(self, sql_builder: GenericSQLBuilder) -> None:
        # Aggregating customer count by region: customer -> nation -> region
        intent = QueryIntent(
            primary_table="customer",
            dimensions=["region.r_name"],
            metrics=[("c_custkey", "COUNT", "customer_count")],
            filters=[
                FilterSlot(
                    table="region",
                    column="r_name",
                    operator="IN",
                    value=["ASIA", "EUROPE"],
                )
            ],
        )

        sql = sql_builder.build(intent, dialect="postgres")

        parsed = sqlglot.parse_one(sql, read="postgres")
        assert parsed is not None
        assert "FROM customer" in sql
        assert "JOIN nation" in sql
        assert "JOIN region" in sql
        assert "GROUP BY" in sql
        assert "region.r_name" in sql
        assert "IN ('ASIA', 'EUROPE')" in sql


class TestOntologyDeduplicationIntegration:
    """Tests for queries combining parent-child ontology deduplication."""

    def test_japan_and_asia_filters_prune_region_join(self, sql_builder: GenericSQLBuilder) -> None:
        # Both ASIA (parent) and JAPAN (child) filters are present.
        # Deduplicator should eliminate ASIA (region.r_name).
        # Since region is no longer referenced anywhere, region table should NOT be joined!
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["nation.n_name"],
            metrics=[("o_totalprice", "SUM")],
            filters=[
                FilterSlot(table="region", column="r_name", operator="=", value="ASIA"),
                FilterSlot(table="nation", column="n_name", operator="=", value="JAPAN"),
            ],
        )

        sql = sql_builder.build(intent, dialect="postgres")

        parsed = sqlglot.parse_one(sql, read="postgres")
        assert parsed is not None
        # Parent filter and join should be eliminated
        assert "region" not in sql.lower()
        assert "nation.n_name = 'JAPAN'" in sql
        assert "customer" in sql.lower()  # intermediary join orders -> customer -> nation
        assert "nation" in sql.lower()

    def test_without_deduplicator_keeps_both_filters_and_joins(
        self, raw_builder: GenericSQLBuilder
    ) -> None:
        # Builder without deduplicator should preserve both filters and join region
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["nation.n_name"],
            metrics=[("o_totalprice", "SUM")],
            filters=[
                FilterSlot(table="region", column="r_name", operator="=", value="ASIA"),
                FilterSlot(table="nation", column="n_name", operator="=", value="JAPAN"),
            ],
        )

        sql = raw_builder.build(intent, dialect="postgres")
        assert "region" in sql.lower()
        assert "ASIA" in sql


class TestAdHocNonAggregateListing:
    """Tests for non-aggregate listing queries (dimensions only with LIMIT)."""

    def test_dimensions_only_with_limit(self, sql_builder: GenericSQLBuilder) -> None:
        intent = QueryIntent(
            primary_table="customer",
            dimensions=["customer.c_name", "customer.c_phone", "nation.n_name"],
            limit=50,
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "SELECT" in sql
        assert "customer.c_name" in sql
        assert "nation.n_name" in sql
        assert "FROM customer" in sql
        assert "JOIN nation" in sql
        assert "GROUP BY" not in sql
        assert "LIMIT 50" in sql

    def test_empty_dimensions_and_metrics_defaults_to_star(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        intent = QueryIntent(
            primary_table="part",
            limit=10,
        )
        sql = sql_builder.build(intent, dialect="postgres")
        assert "SELECT" in sql
        assert "*" in sql
        assert "FROM part" in sql
        assert "LIMIT 10" in sql


class TestOrderByAndRanking:
    """Tests for sorting, ordering by alias or metric, and top-N ranking."""

    def test_order_by_alias_and_limit(self, sql_builder: GenericSQLBuilder) -> None:
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["c_name"],
            metrics=[("o_totalprice", "SUM", "revenue")],
            order_by=[("revenue", "DESC")],
            limit=5,
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "ORDER BY" in sql
        assert "revenue DESC" in sql
        assert "LIMIT 5" in sql

    def test_order_by_slot_object(self, sql_builder: GenericSQLBuilder) -> None:
        intent = QueryIntent(
            primary_table="lineitem",
            dimensions=["l_shipmode"],
            metrics=[("l_quantity", "SUM")],
            order_by=[
                OrderBySlot(column="l_shipmode", direction="ASC"),
                OrderBySlot(column="l_quantity", direction="DESC"),
            ],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "ORDER BY" in sql
        assert "lineitem.l_shipmode ASC" in sql or "l_shipmode ASC" in sql


class TestMultiDialectTranspilation:
    """Tests for SQL generation across various target dialects."""

    @pytest.mark.parametrize(
        ("dialect", "expected_fragment"),
        [
            ("postgres", "LIMIT 10"),
            ("duckdb", "LIMIT 10"),
            ("sqlite", "LIMIT 10"),
            ("bigquery", "LIMIT 10"),
            ("snowflake", "LIMIT 10"),
        ],
    )
    def test_supported_dialects_syntax(
        self, sql_builder: GenericSQLBuilder, dialect: str, expected_fragment: str
    ) -> None:
        intent = QueryIntent(
            primary_table="nation",
            dimensions=["n_name"],
            filters=[FilterSlot(table="region", column="r_name", operator="=", value="ASIA")],
            limit=10,
        )

        sql = sql_builder.build(intent, dialect=dialect)
        assert expected_fragment in sql
        # Parse result back with sqlglot for syntax check
        parsed = sqlglot.parse_one(sql, read=dialect)
        assert parsed is not None


class TestBuildErrorsAndValidation:
    """Tests for error handling on invalid schemas, columns, or inputs."""

    def test_invalid_primary_table_raises_error(self, sql_builder: GenericSQLBuilder) -> None:
        intent = QueryIntent(primary_table="non_existent_table")
        with pytest.raises(SQLBuildError, match="does not exist in schema"):
            sql_builder.build(intent)

    def test_non_existent_column_in_dimension_raises_error(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["orders.invalid_col"],
        )
        with pytest.raises(SQLBuildError, match="Column 'invalid_col' does not exist"):
            sql_builder.build(intent)

    def test_non_existent_table_in_dimension_raises_error(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["ghost_table.col"],
        )
        with pytest.raises(SQLBuildError, match="Table 'ghost_table'"):
            sql_builder.build(intent)

    def test_non_existent_column_without_prefix_raises_error(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["completely_fake_column_name"],
        )
        with pytest.raises(SQLBuildError, match="does not exist in any schema table"):
            sql_builder.build(intent)

    def test_unsupported_filter_operator_raises_error(self, sql_builder: GenericSQLBuilder) -> None:
        intent = QueryIntent(
            primary_table="orders",
            filters=[
                FilterSlot(
                    table="orders",
                    column="o_orderstatus",
                    operator="REGEX_MATCH",
                    value="^F",
                )
            ],
        )
        with pytest.raises(SQLBuildError, match="Unsupported filter operator"):
            sql_builder.build(intent)

    def test_negative_limit_raises_error(self, sql_builder: GenericSQLBuilder) -> None:
        intent = QueryIntent(primary_table="orders", limit=-5)
        with pytest.raises(SQLBuildError, match="LIMIT cannot be negative"):
            sql_builder.build(intent)

    def test_invalid_intent_type_raises_error(self, sql_builder: GenericSQLBuilder) -> None:
        with pytest.raises(SQLBuildError, match="Invalid intent type"):
            sql_builder.build(["not", "an", "intent"])  # type: ignore[arg-type]

    def test_build_from_dict_success(self, sql_builder: GenericSQLBuilder) -> None:
        intent_dict = {
            "primary_table": "orders",
            "dimensions": ["o_orderstatus"],
            "metrics": [("o_totalprice", "SUM")],
            "limit": 10,
        }
        sql = sql_builder.build(intent_dict, dialect="postgres")
        assert "SELECT" in sql
        assert "FROM orders" in sql

    def test_unconnected_schema_join_raises_sql_build_error(self) -> None:
        # Create schema with an isolated table
        data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        data["tables"].append(
            {
                "name": "isolated_table",
                "description": "孤立テーブル",
                "aliases": ["isolated"],
                "primary_key": ["iso_id"],
                "foreign_keys": [],
                "columns": [
                    {
                        "name": "iso_id",
                        "type": "BIGINT",
                        "description": "ID",
                        "role": "id",
                    },
                    {
                        "name": "iso_name",
                        "type": "VARCHAR(50)",
                        "description": "Name",
                        "role": "dimension",
                    },
                ],
            }
        )
        disconnected_graph = SchemaGraph(data)
        builder = GenericSQLBuilder(schema_graph=disconnected_graph)

        intent = QueryIntent(
            primary_table="orders",
            dimensions=["isolated_table.iso_name"],
        )
        with pytest.raises(SQLBuildError, match="Failed to resolve JOIN path"):
            builder.build(intent)
