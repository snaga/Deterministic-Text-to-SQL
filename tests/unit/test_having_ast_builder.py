"""Unit tests for sqlglot-based type-safe HAVING clause AST assembly.

Tests verify TASK-24 (F-COMP-05, C-6) requirements:
- Single HAVING clause generation using semantic metrics (e.g. total_revenue >= 1000000)
- Multiple HAVING clauses combined with boolean AND
- Automatic extraction and JOIN resolution of tables referenced in HAVING expressions
- Physical column and column alias fallback in HAVING clauses
- Semantic metric synonym resolution in HAVING clauses
- Comprehensive comparison operator support (>=, >, <=, <, =, ==, !=, <>)
- Multi-dialect transpilation (Postgres, DuckDB, SQLite, Snowflake)
- Robust error handling for unsupported operators and invalid expressions
"""

from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from deterministic_text_to_sql.compiler import (
    GenericSQLBuilder,
    HavingSlot,
    MetricSlot,
    QueryIntent,
    SQLBuildError,
)
from deterministic_text_to_sql.graph import SchemaGraph
from deterministic_text_to_sql.schema import (
    ColumnMetadata,
    MetricDefinition,
    SchemaMetadata,
    TableMetadata,
    load_schema,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
TPCH_OSSIE_YAML = FIXTURES_DIR / "tpch_ossie.yaml"


@pytest.fixture(scope="module")
def ossie_schema() -> SchemaMetadata:
    """Fixture providing loaded TPC-H SchemaMetadata."""
    return load_schema(TPCH_OSSIE_YAML)


@pytest.fixture(scope="module")
def schema_graph(ossie_schema: SchemaMetadata) -> SchemaGraph:
    """Fixture providing initialized SchemaGraph."""
    return SchemaGraph(ossie_schema)


@pytest.fixture
def sql_builder(schema_graph: SchemaGraph) -> GenericSQLBuilder:
    """Fixture providing GenericSQLBuilder configured with TPC-H schema."""
    return GenericSQLBuilder(schema_graph=schema_graph)


class TestSingleHavingClause:
    """Tests for single HAVING condition assembly."""

    def test_single_having_semantic_metric_gte(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify HAVING SUM(...) >= 1000000 generated from semantic metric total_revenue."""
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["orders.o_orderstatus"],
            metrics=[MetricSlot(column="total_revenue")],
            having=[HavingSlot(metric="total_revenue", operator=">=", value=1000000)],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        parsed = sqlglot.parse_one(sql, read="postgres")

        having_node = parsed.find(exp.Having)
        assert having_node is not None, "Generated SQL must contain HAVING clause"

        # Left side should be the expanded calculation expression, not the alias
        gte = having_node.find(exp.GTE)
        assert gte is not None
        assert isinstance(gte.this, exp.Sum) or gte.this.find(exp.Sum) is not None
        assert isinstance(gte.expression, exp.Literal)
        assert gte.expression.this == "1000000"

    def test_single_having_semantic_metric_synonym(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify semantic metric synonym (e.g. '売上') is resolved in HAVING."""
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["orders.o_orderstatus"],
            metrics=[MetricSlot(column="total_revenue")],
            having=[HavingSlot(metric="売上", operator=">", value=500000)],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        parsed = sqlglot.parse_one(sql, read="postgres")

        having_node = parsed.find(exp.Having)
        assert having_node is not None
        gt = having_node.find(exp.GT)
        assert gt is not None
        assert gt.find(exp.Sum) is not None
        assert gt.expression.this == "500000"

    def test_having_operators(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify all supported comparison operators in HAVING."""
        operator_cases = [
            (">=", exp.GTE),
            (">", exp.GT),
            ("<=", exp.LTE),
            ("<", exp.LT),
            ("=", exp.EQ),
            ("==", exp.EQ),
            ("!=", exp.NEQ),
            ("<>", exp.NEQ),
        ]

        for op_str, expected_exp_cls in operator_cases:
            intent = QueryIntent(
                primary_table="orders",
                dimensions=["orders.o_orderstatus"],
                metrics=[MetricSlot(column="order_count")],
                having=[HavingSlot(metric="order_count", operator=op_str, value=10)],
            )
            sql = sql_builder.build(intent, dialect="postgres")
            parsed = sqlglot.parse_one(sql, read="postgres")
            having_node = parsed.find(exp.Having)
            assert having_node is not None, f"HAVING clause missing for operator '{op_str}'"
            assert having_node.find(expected_exp_cls) is not None, (
                f"Operator node {expected_exp_cls.__name__} missing for '{op_str}' in SQL: {sql}"
            )


class TestMultipleHavingClauses:
    """Tests for multiple HAVING conditions combined with AND."""

    def test_multiple_having_and_combination(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify multiple HAVING clauses are combined with AND."""
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["orders.o_orderstatus"],
            metrics=[
                MetricSlot(column="total_revenue"),
                MetricSlot(column="order_count"),
            ],
            having=[
                HavingSlot(metric="total_revenue", operator=">=", value=1000000),
                HavingSlot(metric="order_count", operator=">", value=5),
            ],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        parsed = sqlglot.parse_one(sql, read="postgres")

        having_node = parsed.find(exp.Having)
        assert having_node is not None

        # Must have an AND node connecting conditions
        and_node = having_node.find(exp.And)
        assert and_node is not None
        assert having_node.find(exp.GTE) is not None
        assert having_node.find(exp.GT) is not None


class TestHavingJoinPathResolution:
    """Tests verifying referenced tables in HAVING expressions trigger auto-JOINs."""

    def test_having_referenced_table_auto_join(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify table referenced only in HAVING is joined automatically.

        Query is on 'customer' table with 'nation.n_name' dimension and customer_count metric,
        but HAVING references 'total_revenue' which references 'lineitem'.
        SchemaGraph must resolve customer -> orders -> lineitem JOIN path.
        """
        intent = QueryIntent(
            primary_table="customer",
            dimensions=["nation.n_name"],
            metrics=[MetricSlot(column="customer_count")],
            having=[HavingSlot(metric="total_revenue", operator=">=", value=500000)],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        parsed = sqlglot.parse_one(sql, read="postgres")

        # Table customer is FROM
        from_table = parsed.find(exp.Table)
        assert from_table is not None
        assert from_table.name == "customer"

        # lineitem, orders, and nation must all be joined
        joined_tables = {j.this.name for j in parsed.find_all(exp.Join)}
        assert "nation" in joined_tables
        assert "orders" in joined_tables
        assert "lineitem" in joined_tables

        # HAVING condition must be present
        having_node = parsed.find(exp.Having)
        assert having_node is not None
        assert having_node.find(exp.GTE) is not None


class TestHavingPhysicalColumnAndAliasFallback:
    """Tests for physical column and select alias resolution in HAVING."""

    def test_having_physical_column(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify physical column resolution in HAVING."""
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["orders.o_orderstatus"],
            metrics=[MetricSlot(column="o_totalprice", agg="sum")],
            having=[HavingSlot(metric="orders.o_totalprice", operator=">", value=1000)],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        parsed = sqlglot.parse_one(sql, read="postgres")

        having_node = parsed.find(exp.Having)
        assert having_node is not None
        col = having_node.find(exp.Column)
        assert col is not None
        assert col.name == "o_totalprice"
        assert col.table == "orders"

    def test_having_alias_fallback(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify fallback to column node for unresolved alias in HAVING."""
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["orders.o_orderstatus"],
            metrics=[MetricSlot(column="o_totalprice", agg="sum", alias="custom_sum")],
            having=[HavingSlot(metric="custom_sum", operator=">", value=1000)],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        parsed = sqlglot.parse_one(sql, read="postgres")

        having_node = parsed.find(exp.Having)
        assert having_node is not None
        col = having_node.find(exp.Column)
        assert col is not None
        assert col.name == "custom_sum"


class TestHavingMultiDialectTranspilation:
    """Tests for multi-dialect transpilation of queries with HAVING clauses."""

    @pytest.mark.parametrize("dialect", ["postgres", "duckdb", "sqlite", "snowflake"])
    def test_having_dialect_transpilation(
        self, sql_builder: GenericSQLBuilder, dialect: str
    ) -> None:
        """Verify query with HAVING transpiles cleanly across dialects."""
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["orders.o_orderstatus"],
            metrics=[MetricSlot(column="total_revenue")],
            having=[HavingSlot(metric="total_revenue", operator=">=", value=1000000)],
        )

        sql = sql_builder.build(intent, dialect=dialect)
        assert "HAVING" in sql.upper()
        # Verify dialect parses without error
        sqlglot.parse_one(sql, read=dialect)


class TestHavingErrorHandling:
    """Tests for error scenarios in HAVING clause construction."""

    def test_unsupported_operator_raises_error(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify unsupported operator (e.g. 'LIKE', 'IN') raises SQLBuildError."""
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["orders.o_orderstatus"],
            metrics=[MetricSlot(column="total_revenue")],
            having=[HavingSlot(metric="total_revenue", operator="LIKE", value="%100%")],
        )

        with pytest.raises(SQLBuildError, match="Unsupported HAVING operator: 'LIKE'"):
            sql_builder.build(intent)

    def test_invalid_semantic_metric_expression_raises_error(self) -> None:
        """Verify unparseable semantic metric expression raises SQLBuildError."""
        broken_metric = MetricDefinition(
            name="broken_metric",
            expression="SUM(+++invalid SQL syntax)",
            datatype="DOUBLE",
        )
        broken_schema = SchemaMetadata(
            title="BrokenSchema",
            tables=[
                TableMetadata(
                    name="orders",
                    description="Orders table",
                    columns=[
                        ColumnMetadata(
                            name="o_orderkey",
                            type="BIGINT",
                            description="Order key",
                            role="id",
                        )
                    ],
                )
            ],
            metrics=[broken_metric],
        )
        builder = GenericSQLBuilder(schema_graph=SchemaGraph(broken_schema))

        intent = QueryIntent(
            primary_table="orders",
            having=[HavingSlot(metric="broken_metric", operator=">", value=100)],
        )

        with pytest.raises(SQLBuildError, match="Failed to parse semantic metric expression"):
            builder.build(intent)


class TestHavingEdgeCases:
    """Tests for edge cases and boundary conditions in HAVING clause assembly."""

    def test_having_with_dict_intent(self, sql_builder: GenericSQLBuilder) -> None:
        """Verify HAVING assembly when intent is passed as raw dictionary."""
        intent_dict = {
            "primary_table": "orders",
            "dimensions": ["orders.o_orderstatus"],
            "metrics": [{"column": "total_revenue"}],
            "having": [{"metric": "total_revenue", "operator": ">=", "value": 1000000}],
        }

        sql = sql_builder.build(intent_dict, dialect="postgres")
        assert "HAVING" in sql.upper()
        parsed = sqlglot.parse_one(sql, read="postgres")
        having_node = parsed.find(exp.Having)
        assert having_node is not None
        assert having_node.find(exp.GTE) is not None

    def test_having_with_dimension_and_no_explicit_metrics(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        """Verify GROUP BY is properly generated when HAVING exists.

        Ensures GROUP BY is created even if no explicit metrics are in SELECT.
        """
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["orders.o_orderstatus"],
            having=[HavingSlot(metric="total_revenue", operator=">=", value=1000000)],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        parsed = sqlglot.parse_one(sql, read="postgres")

        group_by = parsed.find(exp.Group)
        assert group_by is not None, "GROUP BY must be generated when dimensions and HAVING exist"
        having_node = parsed.find(exp.Having)
        assert having_node is not None

    def test_having_without_dimensions_or_metrics(
        self, sql_builder: GenericSQLBuilder
    ) -> None:
        """Verify HAVING generates on overall table aggregate without GROUP BY."""
        intent = QueryIntent(
            primary_table="orders",
            having=[HavingSlot(metric="total_revenue", operator=">=", value=1000000)],
        )

        sql = sql_builder.build(intent, dialect="postgres")
        parsed = sqlglot.parse_one(sql, read="postgres")

        assert parsed.find(exp.Group) is None, "No GROUP BY should exist without dimensions"
        having_node = parsed.find(exp.Having)
        assert having_node is not None

