"""Unit tests for multi-dimensional and time-dimension engine and CLI integration (TASK-25).

Tests cover:
- _build_intent_from_slots resolution of dimensions (semantic/physical) and having_slots
- engine.get_schema_info() returning semantic dimensions list
- CLI schema command Rich table and JSON output with Semantic Dimensions
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from deterministic_text_to_sql.cli import app
from deterministic_text_to_sql.compiler.models import HavingSlot
from deterministic_text_to_sql.engine import (
    DeterministicTextToSQLEngine,
    _build_intent_from_slots,
)
from deterministic_text_to_sql.jev.models import ParsedSlots
from deterministic_text_to_sql.schema.models import (
    ColumnMetadata,
    DimensionMetadata,
    MetricDefinition,
    SchemaMetadata,
    TableMetadata,
)


@pytest.fixture
def sample_schema_with_dimensions() -> SchemaMetadata:
    """Fixture providing a mock schema with semantic dimensions, tables, and metrics."""
    dim_year = DimensionMetadata(
        name="orders_year",
        expression="EXTRACT(year FROM orders.o_orderdate)",
        description="注文年（年別集計軸）",
        synonyms=["年", "注文年", "年度"],
        is_time=True,
        time_granularity="year",
    )
    dim_month = DimensionMetadata(
        name="orders_month",
        expression="EXTRACT(month FROM orders.o_orderdate)",
        description="注文月（月別集計軸）",
        synonyms=["月", "注文月"],
        is_time=True,
        time_granularity="month",
    )
    metric_revenue = MetricDefinition(
        name="total_revenue",
        expression="SUM(orders.o_totalprice)",
        description="売上合計金額",
        synonyms=["売上", "売上合計"],
    )

    tbl_orders = TableMetadata(
        name="orders",
        description="注文テーブル",
        columns=[
            ColumnMetadata(name="o_orderkey", type="INTEGER", description="注文ID", role="id"),
            ColumnMetadata(name="o_custkey", type="INTEGER", description="顧客ID", role="fk"),
            ColumnMetadata(name="o_orderdate", type="DATE", description="注文日", role="dimension"),
            ColumnMetadata(
                name="o_totalprice", type="DOUBLE", description="合計金額", role="metric"
            ),
            ColumnMetadata(
                name="o_orderstatus",
                type="VARCHAR(1)",
                description="ステータス",
                role="dimension",
            ),
        ],
    )
    tbl_nation = TableMetadata(
        name="nation",
        description="国マスター",
        columns=[
            ColumnMetadata(name="n_nationkey", type="INTEGER", description="国ID", role="id"),
            ColumnMetadata(name="n_name", type="VARCHAR(25)", description="国名", role="dimension"),
        ],
    )

    return SchemaMetadata(
        title="Sample Dimension Schema",
        description="Schema with dimensions and metrics for testing",
        tables=[tbl_orders, tbl_nation],
        metrics=[metric_revenue],
        dimensions=[dim_year, dim_month],
    )


@pytest.fixture
def schema_yaml_file(tmp_path: Path) -> Path:
    """Fixture providing a temporary YAML schema file with time dimensions."""
    content = """
version: 0.2.0.dev0
name: test_sales_model
description: Sales model with time dimensions
datasets:
  - name: orders
    source: orders
    description: Orders table
    primary_key:
      - o_orderkey
    fields:
      - name: o_orderkey
        datatype: INTEGER
        role: id
      - name: o_orderdate
        datatype: DATE
        description: Order date
        is_time: true
        time_granularities:
          - year
          - month
        ai_context:
          synonyms:
            - 注文日
            - オーダー日
      - name: o_totalprice
        datatype: DOUBLE
        role: metric
      - name: o_orderstatus
        datatype: VARCHAR(1)
        role: dimension
"""
    file_path = tmp_path / "test_schema.yaml"
    file_path.write_text(content.strip(), encoding="utf-8")
    return file_path


class TestBuildIntentMultiDimAndHaving:
    """Tests for _build_intent_from_slots handling multidim and HAVING slots."""

    def test_build_intent_with_multidim_physical_and_semantic(
        self, sample_schema_with_dimensions: SchemaMetadata
    ) -> None:
        """Verify _build_intent_from_slots resolves semantic dimension and physical column."""
        having_slot = HavingSlot(metric="total_revenue", operator=">", value="1000000")
        slots = ParsedSlots(
            primary_table="orders",
            dimensions=["orders_year", "nation.n_name"],
            metric="total_revenue",
            having_slots=[having_slot],
        )

        intent = _build_intent_from_slots(slots, sample_schema_with_dimensions)

        assert intent.dimensions == ["orders_year", "nation.n_name"]
        assert len(intent.having) == 1
        assert intent.having[0].metric == "total_revenue"
        assert intent.having[0].operator == ">"
        assert intent.having[0].value == "1000000"

    def test_build_intent_resolves_semantic_dimension_by_synonym(
        self, sample_schema_with_dimensions: SchemaMetadata
    ) -> None:
        """Verify synonym '年' resolves to semantic dimension name 'orders_year'."""
        slots = ParsedSlots(
            primary_table="orders",
            dimensions=["年", "o_orderstatus"],
            metric="total_revenue",
        )

        intent = _build_intent_from_slots(slots, sample_schema_with_dimensions)

        assert intent.dimensions == ["orders_year", "orders.o_orderstatus"]

    def test_build_intent_single_dimension_fallback(
        self, sample_schema_with_dimensions: SchemaMetadata
    ) -> None:
        """Verify slots.dimension is resolved when slots.dimensions is empty."""
        # Create ParsedSlots with dimension specified
        slots = ParsedSlots(
            primary_table="orders",
            dimension="orders_year",
            metric="total_revenue",
        )

        intent = _build_intent_from_slots(slots, sample_schema_with_dimensions)

        assert intent.dimensions == ["orders_year"]

    def test_build_intent_deduplicates_dimensions(
        self, sample_schema_with_dimensions: SchemaMetadata
    ) -> None:
        """Verify duplicate dimensions are deduplicated in resolved intent."""
        slots = ParsedSlots(
            primary_table="orders",
            dimensions=["orders_year", "orders_year", "o_orderstatus", "orders.o_orderstatus"],
            metric="total_revenue",
        )

        intent = _build_intent_from_slots(slots, sample_schema_with_dimensions)

        assert intent.dimensions == ["orders_year", "orders.o_orderstatus"]

    def test_build_intent_having_slots_empty_by_default(
        self, sample_schema_with_dimensions: SchemaMetadata
    ) -> None:
        """Verify empty having_slots results in empty having list in QueryIntent."""
        slots = ParsedSlots(
            primary_table="orders",
            dimensions=["orders_year"],
            metric="total_revenue",
        )

        intent = _build_intent_from_slots(slots, sample_schema_with_dimensions)

        assert intent.having == []


class TestEngineGetSchemaInfoDimensions:
    """Tests for DeterministicTextToSQLEngine.get_schema_info semantic dimensions."""

    def test_get_schema_info_includes_semantic_dimensions(
        self, sample_schema_with_dimensions: SchemaMetadata
    ) -> None:
        """Verify engine.get_schema_info() contains all semantic dimensions metadata."""
        engine = DeterministicTextToSQLEngine(schema=sample_schema_with_dimensions)
        try:
            info = engine.get_schema_info()

            assert "dimensions" in info
            assert len(info["dimensions"]) == 2

            dim_map = {d["name"]: d for d in info["dimensions"]}
            assert "orders_year" in dim_map
            year_info = dim_map["orders_year"]
            assert year_info["name"] == "orders_year"
            assert year_info["expression"] == "EXTRACT(year FROM orders.o_orderdate)"
            assert year_info["description"] == "注文年（年別集計軸）"
            assert year_info["is_time"] is True
            assert year_info["time_granularity"] == "year"
            assert "年" in year_info["synonyms"]
            assert "注文年" in year_info["synonyms"]

            assert "orders_month" in dim_map
            month_info = dim_map["orders_month"]
            assert month_info["is_time"] is True
            assert month_info["time_granularity"] == "month"
        finally:
            engine.close()


class TestCLISchemaSemanticDimensions:
    """Tests for CLI schema command output of Semantic Dimensions table and JSON."""

    def test_cli_schema_human_table_displays_semantic_dimensions(
        self, schema_yaml_file: Path
    ) -> None:
        """Verify Rich table with title 'Semantic Dimensions' is rendered."""
        runner = CliRunner()
        result = runner.invoke(app, ["schema", "--schema", str(schema_yaml_file)])

        assert result.exit_code == 0
        assert "Semantic Dimensions" in result.output
        assert "orders_year" in result.output
        assert "EXTRACT(year" in result.output
        assert "year" in result.output

    def test_cli_schema_json_output_includes_dimensions(
        self, schema_yaml_file: Path
    ) -> None:
        """Verify CLI schema command with --json returns dimensions list."""
        runner = CliRunner()
        result = runner.invoke(app, ["schema", "--schema", str(schema_yaml_file), "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)

        assert "dimensions" in data
        assert len(data["dimensions"]) == 2
        dim_names = [d["name"] for d in data["dimensions"]]
        assert "orders_year" in dim_names
        assert "orders_month" in dim_names

        year_dim = next(d for d in data["dimensions"] if d["name"] == "orders_year")
        assert year_dim["is_time"] is True
        assert year_dim["time_granularity"] == "year"
        assert year_dim["expression"] == "EXTRACT(year FROM orders.o_orderdate)"
