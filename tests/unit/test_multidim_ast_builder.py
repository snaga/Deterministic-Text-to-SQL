"""Unit tests for multi-dimensional GROUP BY and semantic time dimension AST expansion.

Tests verify F-COMP-04 requirements:
- Multi-axis physical dimension expansion (GROUP BY col1, col2)
- Semantic time dimension expression parsing and AST assembly (EXTRACT)
- Shortest-path auto-JOIN resolution for multi-table dimensions
- Synonym resolution and alias overrides for dimensions
- ORDER BY integration with dimension names, aliases, and synonyms
- Dimensions-only queries without GROUP BY
- Multi-dialect transpilation (Postgres, DuckDB, SQLite)
- Comprehensive error handling for invalid dimension expressions
"""

from pathlib import Path

import pytest
import sqlglot

from deterministic_text_to_sql.compiler import (
    GenericSQLBuilder,
    MetricSlot,
    QueryIntent,
    SQLBuildError,
)
from deterministic_text_to_sql.graph import SchemaGraph
from deterministic_text_to_sql.schema import (
    ColumnMetadata,
    DimensionMetadata,
    SchemaMetadata,
    TableMetadata,
    load_schema,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
TPCH_OSSIE_YAML = FIXTURES_DIR / "tpch_ossie.yaml"


@pytest.fixture(scope="module")
def ossie_schema_with_dimensions() -> SchemaMetadata:
    """Fixture providing TPC-H SchemaMetadata enriched with semantic DimensionMetadata."""
    schema = load_schema(TPCH_OSSIE_YAML)

    dim_year = DimensionMetadata(
        name="orders_year",
        expression="EXTRACT(year FROM orders.o_orderdate)",
        description="注文年（年別集計軸）",
        synonyms=["年", "年別", "注文年", "年度"],
        is_time=True,
        time_granularity="year",
    )
    dim_month = DimensionMetadata(
        name="orders_month",
        expression="EXTRACT(month FROM orders.o_orderdate)",
        description="注文月（月別集計軸）",
        synonyms=["月", "月別", "注文月"],
        is_time=True,
        time_granularity="month",
    )
    dim_unqualified = DimensionMetadata(
        name="order_date_year",
        expression="EXTRACT(year FROM o_orderdate)",
        description="未修飾カラムを用いた年別集計軸",
        synonyms=["注文日年"],
        is_time=True,
        time_granularity="year",
    )

    enriched_dimensions = list(schema.dimensions) + [dim_year, dim_month, dim_unqualified]

    return SchemaMetadata(
        title=schema.title,
        description=schema.description,
        tables=schema.tables,
        metrics=schema.metrics,
        hierarchies=schema.hierarchies,
        dimensions=enriched_dimensions,
    )


@pytest.fixture(scope="module")
def schema_graph(ossie_schema_with_dimensions: SchemaMetadata) -> SchemaGraph:
    """Fixture providing initialized SchemaGraph."""
    return SchemaGraph(ossie_schema_with_dimensions)


@pytest.fixture
def sql_builder(schema_graph: SchemaGraph) -> GenericSQLBuilder:
    """Fixture providing GenericSQLBuilder configured with enriched TPC-H schema."""
    return GenericSQLBuilder(schema_graph=schema_graph)


class TestPhysicalMultiDimensionGroupBy:
    """Tests for multi-axis GROUP BY with standard physical columns."""

    def test_two_physical_dimensions_group_by(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify two physical dimensions generate SELECT and GROUP BY with shortest JOIN."""
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["nation.n_name", "orders.o_orderstatus"],
            metrics=[MetricSlot(column="total_revenue")],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        parsed = sqlglot.parse_one(sql, read="postgres")
        assert parsed is not None

        # Verify SELECT contains both dimensions in order
        assert "nation.n_name" in sql
        assert "orders.o_orderstatus" in sql
        assert "AS total_revenue" in sql

        # Verify shortest join path: orders -> customer -> nation
        assert "FROM orders" in sql
        assert "JOIN customer" in sql
        assert "JOIN nation" in sql

        # Verify GROUP BY contains both dimensions
        assert "GROUP BY" in sql
        assert "nation.n_name, orders.o_orderstatus" in sql or (
            "nation.n_name" in sql.split("GROUP BY")[1]
            and "orders.o_orderstatus" in sql.split("GROUP BY")[1]
        )

    def test_three_physical_dimensions_group_by(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify 3-axis GROUP BY across region, nation, and customer tables."""
        intent = QueryIntent(
            primary_table="customer",
            dimensions=["region.r_name", "nation.n_name", "customer.c_mktsegment"],
            metrics=[MetricSlot(column="c_acctbal", agg="AVG", alias="avg_acctbal")],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "FROM customer" in sql
        assert "JOIN nation" in sql
        assert "JOIN region" in sql
        assert "GROUP BY" in sql
        assert "region.r_name" in sql.split("GROUP BY")[1]
        assert "nation.n_name" in sql.split("GROUP BY")[1]
        assert "customer.c_mktsegment" in sql.split("GROUP BY")[1]

    def test_physical_dimensions_with_custom_aliases(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify 'col AS alias' syntax preserves alias in SELECT and column in GROUP BY."""
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["nation.n_name AS country_name", "orders.o_orderstatus AS status"],
            metrics=[MetricSlot(column="total_revenue")],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "nation.n_name AS country_name" in sql
        assert "orders.o_orderstatus AS status" in sql
        assert "GROUP BY" in sql
        assert "nation.n_name" in sql.split("GROUP BY")[1]
        assert "orders.o_orderstatus" in sql.split("GROUP BY")[1]


class TestSemanticTimeDimensionASTExpansion:
    """Tests for expanding calculated semantic dimensions (e.g. EXTRACT(year FROM date))."""

    def test_semantic_time_dimension_alone(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify semantic time dimension alone generates EXTRACT AST in SELECT and GROUP BY."""
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["orders_year"],
            metrics=[MetricSlot(column="total_revenue")],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "EXTRACT(YEAR FROM orders.o_orderdate) AS orders_year" in sql
        assert "GROUP BY" in sql
        assert "EXTRACT(YEAR FROM orders.o_orderdate)" in sql.split("GROUP BY")[1]

    def test_semantic_time_dimension_combined_with_physical_dimension(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        """Verify time dimension and physical dimension multi-axis grouping with multi-hop JOIN."""
        intent = QueryIntent(
            primary_table="lineitem",
            dimensions=["orders_year", "nation.n_name"],
            metrics=[MetricSlot(column="total_revenue")],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        parsed = sqlglot.parse_one(sql, read="postgres")
        assert parsed is not None

        # Verify SELECT expressions
        assert "EXTRACT(YEAR FROM orders.o_orderdate) AS orders_year" in sql
        assert "nation.n_name" in sql
        assert "AS total_revenue" in sql

        # Verify auto-joins to orders and nation from lineitem
        assert "FROM lineitem" in sql
        assert "JOIN orders" in sql
        assert "JOIN nation" in sql

        # Verify GROUP BY contains both dimensions
        assert "GROUP BY" in sql
        group_part = sql.split("GROUP BY")[1]
        assert "EXTRACT(YEAR FROM orders.o_orderdate)" in group_part
        assert "nation.n_name" in group_part

    def test_semantic_time_dimension_synonym_lookup(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify lookup of semantic dimension via natural language synonyms ('年', '注文年')."""
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["年"],
            metrics=[MetricSlot(column="total_revenue")],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        # Default alias should be dimension name 'orders_year'
        assert "EXTRACT(YEAR FROM orders.o_orderdate) AS orders_year" in sql
        assert "GROUP BY" in sql
        assert "EXTRACT(YEAR FROM orders.o_orderdate)" in sql.split("GROUP BY")[1]

    def test_semantic_time_dimension_custom_alias_override(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        """Verify custom alias override 'orders_year AS ord_yr' replaces default alias."""
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["orders_year AS ord_yr"],
            metrics=[MetricSlot(column="total_revenue")],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "EXTRACT(YEAR FROM orders.o_orderdate) AS ord_yr" in sql
        assert "GROUP BY" in sql
        assert "EXTRACT(YEAR FROM orders.o_orderdate)" in sql.split("GROUP BY")[1]

    def test_semantic_dimension_unqualified_column_resolution(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        """Verify unqualified column 'o_orderdate' inside expression is resolved and auto-joined."""
        intent = QueryIntent(
            primary_table="lineitem",
            dimensions=["order_date_year"],
            metrics=[MetricSlot(column="total_revenue")],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        # Column should be qualified with orders.o_orderdate
        assert "orders.o_orderdate" in sql
        assert "JOIN orders" in sql


class TestMultiDimOrderByIntegration:
    """Tests for ORDER BY referencing dimension aliases, names, and synonyms."""

    def test_order_by_semantic_dimension_alias(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify ORDER BY referencing dimension alias produces ORDER BY <alias>."""
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["orders_year"],
            metrics=[MetricSlot(column="total_revenue")],
            order_by=[("orders_year", "DESC")],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "ORDER BY" in sql
        assert "orders_year DESC" in sql

    def test_order_by_semantic_dimension_synonym(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify ORDER BY referencing synonym resolves to dimension alias."""
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["orders_year"],
            metrics=[MetricSlot(column="total_revenue")],
            order_by=[("年", "ASC")],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "ORDER BY" in sql
        assert "orders_year ASC" in sql

    def test_order_by_custom_dimension_alias(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify ORDER BY referencing custom alias 'yr'."""
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["orders_year AS yr"],
            metrics=[MetricSlot(column="total_revenue")],
            order_by=[("yr", "DESC")],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "ORDER BY" in sql
        assert "yr DESC" in sql


class TestMultiDimNonAggregateListing:
    """Tests for dimension-only queries without metrics."""

    def test_dimensions_only_produces_no_group_by(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify dimension-only query produces SELECT without GROUP BY."""
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["orders_year", "nation.n_name"],
            limit=20,
        )

        sql = sql_builder.build(intent, dialect="postgres")
        assert "GROUP BY" not in sql
        assert "EXTRACT(YEAR FROM orders.o_orderdate) AS orders_year" in sql
        assert "nation.n_name" in sql
        assert "LIMIT 20" in sql


class TestMultiDialectTranspilation:
    """Tests for transpiling multi-dimensional and time AST across target dialects."""

    @pytest.mark.parametrize("dialect", ["postgres", "duckdb", "sqlite"])
    def test_multidim_transpiles_across_dialects(
        self, sql_builder: GenericSQLBuilder, dialect: str
    ) -> None:
        """Verify query with multiple dimensions and metrics transpiles cleanly to dialect."""
        intent = QueryIntent(
            primary_table="lineitem",
            dimensions=["orders_year", "nation.n_name"],
            metrics=[MetricSlot(column="total_revenue")],
            limit=10,
        )

        sql = sql_builder.build(intent, dialect=dialect)
        assert isinstance(sql, str)
        assert len(sql) > 0

        # Ensure parsed back by sqlglot with read dialect
        parsed = sqlglot.parse_one(sql, read=dialect)
        assert parsed is not None


class TestMultiDimErrorHandling:
    """Tests for error conditions during dimension AST expansion."""

    def test_invalid_sql_in_dimension_expression_raises_error(self) -> None:
        """Verify syntax error in dimension expression raises SQLBuildError."""
        broken_dim = DimensionMetadata(
            name="broken_dim",
            expression="EXTRACT(year FROM",  # Syntax error
        )
        schema = SchemaMetadata(
            tables=[
                TableMetadata(
                    name="orders",
                    description="orders",
                    columns=[
                        ColumnMetadata(
                            name="o_orderdate", type="DATE", description="date", role="dimension"
                        )
                    ],
                )
            ],
            dimensions=[broken_dim],
        )
        graph = SchemaGraph(schema)
        builder = GenericSQLBuilder(schema_graph=graph)

        intent = QueryIntent(
            primary_table="orders",
            dimensions=["broken_dim"],
            metrics=[MetricSlot(column="o_orderdate", agg="COUNT")],
        )

        with pytest.raises(SQLBuildError, match="Failed to parse semantic dimension expression"):
            builder.build(intent)

    def test_nonexistent_table_in_dimension_expression_raises_error(self) -> None:
        """Verify reference to unknown table in dimension expression raises SQLBuildError."""
        unknown_dim = DimensionMetadata(
            name="unknown_table_dim",
            expression="EXTRACT(year FROM non_existent_table.some_date)",
        )
        schema = SchemaMetadata(
            tables=[
                TableMetadata(
                    name="orders",
                    description="orders",
                    columns=[
                        ColumnMetadata(
                            name="o_orderdate", type="DATE", description="date", role="dimension"
                        )
                    ],
                )
            ],
            dimensions=[unknown_dim],
        )
        graph = SchemaGraph(schema)
        builder = GenericSQLBuilder(schema_graph=graph)

        intent = QueryIntent(
            primary_table="orders",
            dimensions=["unknown_table_dim"],
            metrics=[MetricSlot(column="o_orderdate", agg="COUNT")],
        )

        err_pattern = "Table 'non_existent_table' in dimension expression does not exist in schema"
        with pytest.raises(SQLBuildError, match=err_pattern):
            builder.build(intent)

    def test_nonexistent_column_in_dimension_expression_raises_error(self) -> None:
        """Verify reference to unknown column in dimension expression raises SQLBuildError."""
        unknown_col_dim = DimensionMetadata(
            name="unknown_col_dim",
            expression="EXTRACT(year FROM orders.non_existent_column)",
        )
        schema = SchemaMetadata(
            tables=[
                TableMetadata(
                    name="orders",
                    description="orders",
                    columns=[
                        ColumnMetadata(
                            name="o_orderdate", type="DATE", description="date", role="dimension"
                        )
                    ],
                )
            ],
            dimensions=[unknown_col_dim],
        )
        graph = SchemaGraph(schema)
        builder = GenericSQLBuilder(schema_graph=graph)

        intent = QueryIntent(
            primary_table="orders",
            dimensions=["unknown_col_dim"],
            metrics=[MetricSlot(column="o_orderdate", agg="COUNT")],
        )

        err_pattern = "Column 'non_existent_column' does not exist in table 'orders'"
        with pytest.raises(SQLBuildError, match=err_pattern):
            builder.build(intent)
