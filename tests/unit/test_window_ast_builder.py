"""Unit tests for sqlglot 2-pass CTE and Window Functions AST synthesis (Detroit-style).

Verifies TASK-29 requirements:
1. Scenario 1: Partitioned intra-group Top-N (ROW_NUMBER + WHERE rank_num <= limit)
2. Scenario 2: Time-series window shift (LAG + YoY growth rate formula)
3. Scenario 3: Multi-dialect transpilation (PostgreSQL, DuckDB, SQLite, Snowflake)
4. Scenario 4: Error handling and validation on invalid window configurations
5. Scenario 5: Backward compatibility for single-pass queries when window is None
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlglot

from deterministic_text_to_sql.compiler import (
    GenericSQLBuilder,
    MetricSlot,
    OrderBySlot,
    QueryIntent,
    SQLBuildError,
    WindowSlot,
)
from deterministic_text_to_sql.graph import SchemaGraph
from deterministic_text_to_sql.schema.models import SchemaMetadata
from deterministic_text_to_sql.schema.ossie import OssieSchemaAdapter

TPCH_OSSIE_PATH = Path("examples/tpch/tpch_semantic_model.yaml")


@pytest.fixture
def tpch_semantic_schema() -> SchemaMetadata:
    """Fixture providing loaded TPC-H SchemaMetadata with semantic metrics and dimensions."""
    return OssieSchemaAdapter.load_from_yaml(TPCH_OSSIE_PATH)


@pytest.fixture
def schema_graph(tpch_semantic_schema: SchemaMetadata) -> SchemaGraph:
    """Fixture providing SchemaGraph initialized with TPC-H semantic schema."""
    return SchemaGraph(tpch_semantic_schema)


@pytest.fixture
def sql_builder(schema_graph: SchemaGraph) -> GenericSQLBuilder:
    """Fixture providing GenericSQLBuilder instance."""
    return GenericSQLBuilder(schema_graph=schema_graph)


class TestScenario1PartitionedTopN:
    """Scenario 1: Intra-group Top-N with ROW_NUMBER() and 2-pass CTE synthesis."""

    def test_partitioned_rank_ast_postgres(self, sql_builder: GenericSQLBuilder) -> None:
        """Test partitioned rank CTE AST compilation for PostgreSQL."""
        intent = QueryIntent(
            primary_table="lineitem",
            dimensions=["region.r_name", "customer.c_name"],
            metrics=[MetricSlot(column="total_revenue")],
            window=WindowSlot(
                kind="partitioned_rank",
                function="ROW_NUMBER",
                partition_by=["region.r_name"],
                limit=3,
            ),
        )

        sql = sql_builder.build(intent, dialect="postgres")

        # Verify CTE structure and window expressions
        assert "WITH ranked_base AS (" in sql
        assert "ROW_NUMBER() OVER (" in sql
        assert "PARTITION BY region.r_name" in sql
        assert "AS rank_num" in sql
        assert "FROM ranked_base" in sql
        assert "rank_num <= 3" in sql
        assert "r_name" in sql and "rank_num" in sql

        # Verify parsed AST is valid SQL
        parsed = sqlglot.parse_one(sql, read="postgres")
        assert parsed is not None

    def test_partitioned_rank_ast_sqlite(self, sql_builder: GenericSQLBuilder) -> None:
        """Test partitioned rank CTE AST compilation for SQLite."""
        intent = QueryIntent(
            primary_table="lineitem",
            dimensions=["region.r_name", "customer.c_name"],
            metrics=[MetricSlot(column="total_revenue")],
            window=WindowSlot(
                kind="partitioned_rank",
                function="ROW_NUMBER",
                partition_by=["region.r_name"],
                limit=2,
            ),
        )

        sql = sql_builder.build(intent, dialect="sqlite")
        assert "WITH ranked_base AS (" in sql
        assert "rank_num <= 2" in sql

        parsed = sqlglot.parse_one(sql, read="sqlite")
        assert parsed is not None

    def test_partitioned_rank_custom_order_by(self, sql_builder: GenericSQLBuilder) -> None:
        """Test partitioned rank with explicit custom order_by in window slot."""
        intent = QueryIntent(
            primary_table="lineitem",
            dimensions=["nation.n_name", "customer.c_name"],
            metrics=[MetricSlot(column="total_revenue")],
            window=WindowSlot(
                kind="partitioned_rank",
                function="DENSE_RANK",
                partition_by=["nation.n_name"],
                order_by=[OrderBySlot(column="total_revenue", direction="ASC")],
                limit=5,
            ),
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "DENSE_RANK() OVER (" in sql
        assert "PARTITION BY nation.n_name" in sql
        assert "ASC" in sql
        assert "AS rank_num" in sql
        assert "rank_num <= 5" in sql


class TestScenario2TimeSeriesWindow:
    """Scenario 2: Time-series window shift with LAG() and YoY growth calculation."""

    def test_time_shift_ast_postgres(self, sql_builder: GenericSQLBuilder) -> None:
        """Test YoY time series shift CTE AST compilation for PostgreSQL."""
        intent = QueryIntent(
            primary_table="lineitem",
            dimensions=["orders_year"],
            metrics=[MetricSlot(column="total_revenue")],
            window=WindowSlot(
                kind="time_shift",
                function="LAG",
                offset=1,
            ),
        )

        sql = sql_builder.build(intent, dialect="postgres")

        # Verify CTE structure
        assert "WITH time_series_base AS (" in sql
        assert "FROM time_series_base" in sql
        assert "LAG(total_revenue, 1) OVER (ORDER BY orders_year)" in sql
        assert "AS prev_total_revenue" in sql
        assert "ROUND(" in sql
        assert "LAG(total_revenue, 1) OVER (ORDER BY orders_year)" in sql
        assert "AS total_revenue_yoy_growth_pct" in sql
        assert "orders_year" in sql

        # Verify AST syntax
        parsed = sqlglot.parse_one(sql, read="postgres")
        assert parsed is not None

    def test_time_shift_ast_sqlite(self, sql_builder: GenericSQLBuilder) -> None:
        """Test YoY time series shift CTE AST compilation for SQLite."""
        intent = QueryIntent(
            primary_table="lineitem",
            dimensions=["orders_year"],
            metrics=[MetricSlot(column="total_revenue")],
            window=WindowSlot(
                kind="time_shift",
                function="LAG",
                offset=1,
            ),
        )

        sql = sql_builder.build(intent, dialect="sqlite")
        assert "WITH time_series_base AS (" in sql
        assert "STRFTIME('%Y', orders.o_orderdate) AS orders_year" in sql
        assert "LAG(total_revenue, 1) OVER (ORDER BY orders_year)" in sql

        parsed = sqlglot.parse_one(sql, read="sqlite")
        assert parsed is not None


class TestScenario3MultiDialectTranspilation:
    """Scenario 3: Multi-dialect transpilation verification across enterprise engines."""

    @pytest.mark.parametrize("dialect", ["postgres", "duckdb", "sqlite", "snowflake"])
    def test_partitioned_rank_all_dialects(
        self, sql_builder: GenericSQLBuilder, dialect: str
    ) -> None:
        """Verify partitioned ranking SQL generates and parses on all supported dialects."""
        intent = QueryIntent(
            primary_table="lineitem",
            dimensions=["region.r_name", "customer.c_name"],
            metrics=[MetricSlot(column="total_revenue")],
            window=WindowSlot(
                kind="partitioned_rank",
                function="ROW_NUMBER",
                partition_by=["region.r_name"],
                limit=3,
            ),
        )

        sql = sql_builder.build(intent, dialect=dialect)
        assert bool(sql)
        parsed = sqlglot.parse_one(sql, read=dialect)
        assert parsed is not None

    @pytest.mark.parametrize("dialect", ["postgres", "duckdb", "sqlite", "snowflake"])
    def test_time_shift_all_dialects(
        self, sql_builder: GenericSQLBuilder, dialect: str
    ) -> None:
        """Verify time-series LAG SQL generates and parses on all supported dialects."""
        intent = QueryIntent(
            primary_table="lineitem",
            dimensions=["orders_year"],
            metrics=[MetricSlot(column="total_revenue")],
            window=WindowSlot(
                kind="time_shift",
                function="LAG",
                offset=1,
            ),
        )

        sql = sql_builder.build(intent, dialect=dialect)
        assert bool(sql)
        parsed = sqlglot.parse_one(sql, read=dialect)
        assert parsed is not None


class TestScenario4ErrorHandlingAndValidation:
    """Scenario 4: Error handling and validation for invalid window configurations."""

    def test_unsupported_window_kind_raises_error(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        """Verify unsupported window kind raises SQLBuildError."""
        # Using dict bypass to test runtime validator in builder
        intent_dict = {
            "primary_table": "lineitem",
            "dimensions": ["orders_year"],
            "metrics": [{"column": "total_revenue"}],
            "window": {
                "kind": "moving_avg",  # Currently unsupported in 2-pass CTE builder
            },
        }

        with pytest.raises(SQLBuildError, match="Unsupported window kind"):
            sql_builder.build(intent_dict)

    def test_negative_window_limit_raises_error(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        """Verify negative window limit raises SQLBuildError."""
        intent = QueryIntent(
            primary_table="lineitem",
            dimensions=["region.r_name", "customer.c_name"],
            metrics=[MetricSlot(column="total_revenue")],
            window=WindowSlot(
                kind="partitioned_rank",
                partition_by=["region.r_name"],
                limit=-1,
            ),
        )

        with pytest.raises(SQLBuildError, match="Window limit cannot be negative"):
            sql_builder.build(intent)

    def test_non_positive_window_offset_raises_error(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        """Verify non-positive offset raises SQLBuildError."""
        intent = QueryIntent(
            primary_table="lineitem",
            dimensions=["orders_year"],
            metrics=[MetricSlot(column="total_revenue")],
            window=WindowSlot(
                kind="time_shift",
                offset=0,
            ),
        )

        with pytest.raises(SQLBuildError, match="Window offset must be positive"):
            sql_builder.build(intent)

    def test_time_shift_without_dimension_raises_error(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        """Verify time shift without dimensions raises SQLBuildError."""
        intent = QueryIntent(
            primary_table="lineitem",
            dimensions=[],
            metrics=[MetricSlot(column="total_revenue")],
            window=WindowSlot(
                kind="time_shift",
            ),
        )

        with pytest.raises(SQLBuildError, match="requires at least one dimension"):
            sql_builder.build(intent)

    def test_time_shift_without_metric_raises_error(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        """Verify time shift without metrics raises SQLBuildError."""
        intent = QueryIntent(
            primary_table="lineitem",
            dimensions=["orders_year"],
            metrics=[],
            window=WindowSlot(
                kind="time_shift",
            ),
        )

        with pytest.raises(SQLBuildError, match="requires at least one metric"):
            sql_builder.build(intent)


class TestScenario5BackwardCompatibility:
    """Scenario 5: Full backward compatibility for queries without window operations."""

    def test_window_none_produces_single_pass_query(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        """Verify window=None produces classic single-pass SELECT query without CTE."""
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["orders.o_orderstatus"],
            metrics=[MetricSlot(column="o_totalprice", agg="SUM", alias="sum_totalprice")],
            window=None,
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "WITH " not in sql
        assert "ranked_base" not in sql
        assert "time_series_base" not in sql
        assert "SELECT" in sql
        assert "GROUP BY" in sql and "orders.o_orderstatus" in sql
