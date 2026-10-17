"""Unit tests for semantic metric AST expansion in GenericSQLBuilder (Detroit-style).

Tests cover Ossie semantic metrics expansion, auto-join resolution across multiple tables,
direct metric expressions, syntax error handling, ORDER BY / GROUP BY integration,
and multi-dialect transpilation.
"""

from pathlib import Path

import pytest
import sqlglot

from deterministic_text_to_sql.compiler import (
    GenericSQLBuilder,
    MetricSlot,
    OrderBySlot,
    QueryIntent,
    SQLBuildError,
)
from deterministic_text_to_sql.graph import SchemaGraph
from deterministic_text_to_sql.schema import SchemaMetadata, load_schema

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
TPCH_OSSIE_YAML = FIXTURES_DIR / "tpch_ossie.yaml"


@pytest.fixture(scope="module")
def ossie_schema() -> SchemaMetadata:
    """Fixture providing loaded SchemaMetadata from tpch_ossie.yaml."""
    assert TPCH_OSSIE_YAML.exists(), f"Missing fixture file: {TPCH_OSSIE_YAML}"
    return load_schema(TPCH_OSSIE_YAML)


@pytest.fixture(scope="module")
def schema_graph(ossie_schema: SchemaMetadata) -> SchemaGraph:
    """Fixture providing initialized SchemaGraph for TPC-H Ossie schema."""
    return SchemaGraph(ossie_schema)


@pytest.fixture
def sql_builder(schema_graph: SchemaGraph) -> GenericSQLBuilder:
    """Fixture providing GenericSQLBuilder configured with TPC-H SchemaGraph."""
    return GenericSQLBuilder(schema_graph=schema_graph)


class TestSemanticMetricASTExpansion:
    """Tests for expanding semantic metrics defined in Apache Ossie schema."""

    def test_customer_total_revenue_auto_joins_orders_and_lineitem(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        """Verify customer-based total_revenue metric auto-joins customer -> orders -> lineitem."""
        intent = QueryIntent(
            primary_table="customer",
            dimensions=["c_custkey", "c_name"],
            metrics=[MetricSlot(column="total_revenue")],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        parsed = sqlglot.parse_one(sql, read="postgres")
        assert parsed is not None

        # Verify FROM and JOIN paths
        assert "FROM customer" in sql
        assert "JOIN orders" in sql
        assert "JOIN lineitem" in sql

        # Verify ON conditions for shortest join path
        assert (
            "customer.c_custkey = orders.o_custkey" in sql
            or "orders.o_custkey = customer.c_custkey" in sql
        )
        assert (
            "orders.o_orderkey = lineitem.l_orderkey" in sql
            or "lineitem.l_orderkey = orders.o_orderkey" in sql
        )

        # Verify SELECT expression AST and default alias
        assert "AS total_revenue" in sql
        assert "lineitem.l_extendedprice" in sql
        assert "lineitem.l_discount" in sql

        # Verify GROUP BY dimensions
        assert "GROUP BY" in sql
        assert "customer.c_custkey" in sql
        assert "customer.c_name" in sql

    def test_metric_tuple_shorthand_resolution(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify metric specified as a tuple ('total_revenue', 'SUM') expands properly."""
        intent = QueryIntent(
            primary_table="customer",
            dimensions=["c_name"],
            metrics=[("total_revenue", "SUM")],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "AS total_revenue" in sql
        assert "JOIN orders" in sql
        assert "JOIN lineitem" in sql
        assert "GROUP BY" in sql
        assert "customer.c_name" in sql

    def test_metric_custom_alias_override(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify that explicit alias overrides the default metric name."""
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["o_orderkey"],
            metrics=[MetricSlot(column="total_revenue", alias="custom_rev")],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "AS custom_rev" in sql
        assert "AS total_revenue" not in sql
        assert "JOIN lineitem" in sql

    def test_metric_synonym_lookup_resolution(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify lookup of semantic metric by its natural language synonym ('売上')."""
        intent = QueryIntent(
            primary_table="orders",
            metrics=[("売上", "SUM")],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "AS total_revenue" in sql
        assert "lineitem.l_extendedprice" in sql
        assert "JOIN lineitem" in sql

    def test_multi_metric_expansion(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify multiple semantic metrics in a single query."""
        intent = QueryIntent(
            primary_table="customer",
            dimensions=["c_name"],
            metrics=[
                MetricSlot(column="total_revenue"),
                MetricSlot(column="total_acctbal"),
            ],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "AS total_revenue" in sql
        assert "AS total_acctbal" in sql
        assert "customer.c_acctbal" in sql
        assert "lineitem.l_extendedprice" in sql
        assert "JOIN orders" in sql
        assert "JOIN lineitem" in sql


class TestDirectMetricExpressionAST:
    """Tests for MetricSlot with directly supplied SQL expressions."""

    def test_direct_expression_with_multi_table_columns(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        """Verify multi-table ratio metric extracts referenced tables and resolves JOINs."""
        intent = QueryIntent(
            primary_table="customer",
            dimensions=["c_name"],
            metrics=[
                MetricSlot(
                    expression="SUM(lineitem.l_extendedprice) / COUNT(DISTINCT orders.o_orderkey)",
                    alias="avg_order_value",
                )
            ],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        parsed = sqlglot.parse_one(sql, read="postgres")
        assert parsed is not None

        # Both tables must be automatically joined
        assert "JOIN orders" in sql
        assert "JOIN lineitem" in sql
        assert "AS avg_order_value" in sql
        assert "COUNT(DISTINCT orders.o_orderkey)" in sql
        assert "SUM(lineitem.l_extendedprice)" in sql

    def test_direct_expression_unqualified_column_resolution(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        """Verify that unqualified columns in expression are resolved and qualified via schema."""
        intent = QueryIntent(
            primary_table="customer",
            dimensions=["c_name"],
            metrics=[
                MetricSlot(
                    expression="SUM(l_extendedprice * (1 - l_discount))",
                    alias="net_sales",
                )
            ],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        # Column l_extendedprice belongs to lineitem, so lineitem must be joined and qualified
        assert "JOIN lineitem" in sql
        assert "lineitem.l_extendedprice" in sql
        assert "lineitem.l_discount" in sql
        assert "AS net_sales" in sql

    def test_direct_expression_without_alias(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify that direct expression without alias renders expression directly in SELECT."""
        intent = QueryIntent(
            primary_table="orders",
            metrics=[MetricSlot(expression="SUM(orders.o_totalprice * 1.1)")],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "SUM(orders.o_totalprice * 1.1)" in sql
        assert "JOIN" not in sql


class TestMetricOrderByAndGroupBy:
    """Tests for integration between expanded metrics, GROUP BY, and ORDER BY."""

    def test_order_by_metric_default_alias(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify sorting by metric default alias (total_revenue DESC)."""
        intent = QueryIntent(
            primary_table="customer",
            dimensions=["c_name"],
            metrics=[MetricSlot(column="total_revenue")],
            order_by=[("total_revenue", "DESC")],
            limit=10,
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "ORDER BY" in sql
        assert "total_revenue DESC" in sql
        assert "LIMIT 10" in sql

    def test_order_by_custom_metric_alias(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify sorting by custom metric alias."""
        intent = QueryIntent(
            primary_table="customer",
            dimensions=["c_name"],
            metrics=[MetricSlot(column="total_revenue", alias="rev")],
            order_by=[("rev", "DESC")],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "ORDER BY" in sql
        assert "rev DESC" in sql

    def test_order_by_metric_name_when_aliased(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify that specifying the metric name in ORDER BY redirects to the custom alias."""
        intent = QueryIntent(
            primary_table="customer",
            dimensions=["c_name"],
            metrics=[MetricSlot(column="total_revenue", alias="rev")],
            order_by=[("total_revenue", "DESC")],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "ORDER BY" in sql
        assert "rev DESC" in sql

    def test_order_by_metric_slot_object(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify sorting using OrderBySlot object with metric alias."""
        intent = QueryIntent(
            primary_table="customer",
            dimensions=["c_custkey"],
            metrics=[MetricSlot(column="total_revenue")],
            order_by=[OrderBySlot(column="total_revenue", direction="ASC")],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "ORDER BY" in sql
        assert "total_revenue ASC" in sql

    def test_order_by_direct_expression_without_alias(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        """Verify sorting by exact expression when metric has no alias."""
        expr = "SUM(orders.o_totalprice)"
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["o_orderstatus"],
            metrics=[MetricSlot(expression=expr)],
            order_by=[(expr, "DESC")],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "ORDER BY" in sql
        assert "SUM(orders.o_totalprice) DESC" in sql

    def test_no_group_by_when_no_dimensions(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify that single overall aggregation metric without dimensions has no GROUP BY."""
        intent = QueryIntent(
            primary_table="customer",
            metrics=[MetricSlot(column="total_revenue")],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "GROUP BY" not in sql
        assert "JOIN orders" in sql
        assert "JOIN lineitem" in sql


class TestMetricErrorHandling:
    """Tests for error cases in metric expressions and column resolution."""

    def test_invalid_sql_syntax_in_expression_raises_error(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        """Verify that malformed SQL expression raises SQLBuildError."""
        intent = QueryIntent(
            primary_table="orders",
            metrics=[MetricSlot(expression="SUM(lineitem.l_extendedprice * )")],
        )

        with pytest.raises(SQLBuildError, match="Failed to parse metric expression"):
            sql_builder.build(intent)

    def test_nonexistent_table_in_metric_expression_raises_error(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        """Verify that referencing an unknown table in expression raises SQLBuildError."""
        intent = QueryIntent(
            primary_table="orders",
            metrics=[MetricSlot(expression="SUM(unknown_tbl.col)")],
        )

        with pytest.raises(
            SQLBuildError, match="Table 'unknown_tbl' in metric expression does not exist"
        ):
            sql_builder.build(intent)

    def test_nonexistent_column_in_metric_expression_raises_error(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        """Verify that referencing an unknown column in expression raises SQLBuildError."""
        intent = QueryIntent(
            primary_table="orders",
            metrics=[MetricSlot(expression="SUM(orders.nonexistent_col)")],
        )

        with pytest.raises(
            SQLBuildError,
            match="Column 'nonexistent_col' does not exist in table 'orders'",
        ):
            sql_builder.build(intent)

    def test_empty_metric_raises_error(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify that MetricSlot with neither column nor expression raises SQLBuildError."""
        intent = QueryIntent(
            primary_table="orders",
            metrics=[MetricSlot(column="", expression=None)],
        )

        with pytest.raises(
            SQLBuildError,
            match="Metric must specify either 'expression' or a valid 'column'",
        ):
            sql_builder.build(intent)


class TestMultiDialectMetricTranspilation:
    """Tests for multi-dialect SQL transpilation with expanded metrics."""

    @pytest.mark.parametrize("dialect", ["postgres", "duckdb", "sqlite"])
    def test_metric_transpiles_across_dialects(
        self, sql_builder: GenericSQLBuilder, dialect: str
    ) -> None:
        """Verify that generated SQL with expanded metrics parses cleanly in target dialect."""
        intent = QueryIntent(
            primary_table="customer",
            dimensions=["c_name"],
            metrics=[MetricSlot(column="total_revenue")],
            order_by=[("total_revenue", "DESC")],
            limit=5,
        )

        sql = sql_builder.build(intent, dialect=dialect)
        parsed = sqlglot.parse_one(sql, read=dialect)
        assert parsed is not None
        assert "customer" in sql
        assert "orders" in sql
        assert "lineitem" in sql
        assert "total_revenue" in sql
