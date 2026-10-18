"""Unit tests for multi-dimensional grouping and HAVING slot extraction."""

from __future__ import annotations

import pytest

from deterministic_text_to_sql.compiler.models import HavingSlot
from deterministic_text_to_sql.jev.models import ParsedSlots, RawSlots
from deterministic_text_to_sql.jev.prompts import _build_dynamic_criteria, build_jev_payload
from deterministic_text_to_sql.jev.slots import parse_jev_response
from deterministic_text_to_sql.schema.models import (
    ColumnMetadata,
    DimensionMetadata,
    MetricDefinition,
    SchemaMetadata,
    TableMetadata,
)


@pytest.fixture
def tpch_like_schema() -> SchemaMetadata:
    """Fixture providing a mock schema with semantic dimensions, tables, and metrics."""
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
    metric_revenue = MetricDefinition(
        name="total_revenue",
        expression="SUM(orders.o_totalprice)",
        description="売上合計金額",
        synonyms=["売上", "売上高", "注文金額", "売上合計"],
    )
    metric_qty = MetricDefinition(
        name="total_quantity",
        expression="SUM(lineitem.l_quantity)",
        description="注文数量合計",
        synonyms=["数量", "注文数量", "販売数量"],
    )

    tbl_orders = TableMetadata(
        name="orders",
        description="注文ヘッダーテーブル",
        columns=[
            ColumnMetadata(name="o_orderkey", type="INTEGER", description="注文ID", role="id"),
            ColumnMetadata(name="o_custkey", type="INTEGER", description="顧客ID", role="fk"),
            ColumnMetadata(
                name="o_orderdate", type="DATE", description="注文日", role="dimension"
            ),
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
            ColumnMetadata(name="n_regionkey", type="INTEGER", description="地域ID", role="fk"),
        ],
    )

    return SchemaMetadata(
        title="TPC-H Mock Schema",
        tables=[tbl_orders, tbl_nation],
        metrics=[metric_revenue, metric_qty],
        dimensions=[dim_year, dim_month],
    )


class TestParsedSlotsModelSync:
    """Tests for backward-compatible dimension synchronization in ParsedSlots and RawSlots."""

    def test_single_dimension_syncs_to_dimensions(self) -> None:
        """When dimension is specified, dimensions should automatically contain [dimension]."""
        slots = ParsedSlots(primary_table="orders", dimension="o_orderstatus")
        assert slots.dimension == "o_orderstatus"
        assert slots.dimensions == ["o_orderstatus"]

    def test_dimensions_syncs_to_dimension(self) -> None:
        """When dimensions is non-empty and dimension is 'none', dimension syncs to dimensions[0].
        """
        slots = ParsedSlots(
            primary_table="orders",
            dimensions=["orders_year", "nation.n_name"],
        )
        assert slots.dimension == "orders_year"
        assert slots.dimensions == ["orders_year", "nation.n_name"]

    def test_empty_dimensions_and_none_dimension(self) -> None:
        """When neither is specified, dimension is 'none' and dimensions is empty."""
        slots = ParsedSlots(primary_table="orders")
        assert slots.dimension == "none"
        assert slots.dimensions == []

    def test_raw_slots_dimension_sync(self) -> None:
        """RawSlots should also synchronize dimension and dimensions."""
        raw = RawSlots(primary_table="orders", dimensions=["orders_year", "n_name"])
        assert raw.dimension == "orders_year"
        assert raw.dimensions == ["orders_year", "n_name"]

    def test_having_slots_default_and_assignment(self) -> None:
        """having_slots default is empty and accepts HavingSlot instances."""
        having = HavingSlot(metric="total_revenue", operator=">=", value=10000000)
        slots = ParsedSlots(primary_table="orders", having_slots=[having])
        assert len(slots.having_slots) == 1
        assert slots.having_slots[0].metric == "total_revenue"
        assert slots.having_slots[0].operator == ">="
        assert slots.having_slots[0].value == 10000000


class TestMultiDimensionExtraction:
    """Tests for extracting multiple dimensions from Jev responses."""

    def test_multidim_extraction_year_and_nation(self, tpch_like_schema: SchemaMetadata) -> None:
        """Extract dimensions=['orders_year', 'nation.n_name'] from mock response."""
        mock_response = {
            "answers": {
                "primary_table": {"choice": "orders"},
                "secondary_table": {"choice": "nation"},
                "dimensions": {"choice": "orders_year, nation.n_name"},
                "metric": {"choice": "total_revenue"},
            }
        }
        query = "年別かつ国別の注文金額を集計して"
        parsed = parse_jev_response(query, mock_response, schema=tpch_like_schema)

        assert parsed.primary_table == "orders"
        assert parsed.dimensions == ["orders_year", "nation.n_name"]
        assert parsed.dimension == "orders_year"
        assert parsed.metric == "total_revenue"

    def test_multidim_extraction_from_list_in_raw_response(
        self, tpch_like_schema: SchemaMetadata
    ) -> None:
        """Extract dimensions when given as a Python list in raw response."""
        mock_response = {
            "primary_table": "orders",
            "dimensions": ["orders_year", "nation.n_name"],
            "metric": "total_revenue",
        }
        query = "年別および国別の売上"
        parsed = parse_jev_response(query, mock_response, schema=tpch_like_schema)

        assert parsed.dimensions == ["orders_year", "nation.n_name"]
        assert parsed.dimension == "orders_year"

    def test_single_dimension_backward_compatibility(
        self, tpch_like_schema: SchemaMetadata
    ) -> None:
        """Verify that single dimension continues to extract and synchronize properly."""
        mock_response = {
            "answers": {
                "primary_table": {"choice": "orders"},
                "dimension": {"choice": "o_orderdate"},
                "metric": {"choice": "total_revenue"},
            }
        }
        query = "注文日ごとの売上合計"
        parsed = parse_jev_response(query, mock_response, schema=tpch_like_schema)

        assert parsed.dimension == "o_orderdate"
        assert parsed.dimensions == ["o_orderdate"]

    def test_no_dimension_yields_empty_dimensions(
        self, tpch_like_schema: SchemaMetadata
    ) -> None:
        """Verify that 'none' dimension yields empty dimensions list and 'none' dimension."""
        mock_response = {
            "answers": {
                "primary_table": {"choice": "orders"},
                "dimension": {"choice": "none"},
                "metric": {"choice": "total_revenue"},
            }
        }
        query = "全体の売上合計"
        parsed = parse_jev_response(query, mock_response, schema=tpch_like_schema)

        assert parsed.dimension == "none"
        assert parsed.dimensions == []


class TestHavingSlotExtraction:
    """Tests for extracting HAVING aggregation filter conditions from Jev responses."""

    def test_having_slot_from_direct_having_slots(
        self, tpch_like_schema: SchemaMetadata
    ) -> None:
        """Extract HavingSlot from explicit having_slots dictionary list."""
        mock_response = {
            "primary_table": "orders",
            "metric": "total_revenue",
            "having_slots": [
                {"metric": "total_revenue", "operator": ">=", "value": 10000000}
            ],
        }
        query = "売上1000万円以上の注文"
        parsed = parse_jev_response(query, mock_response, schema=tpch_like_schema)

        assert len(parsed.having_slots) == 1
        hs = parsed.having_slots[0]
        assert hs.metric == "total_revenue"
        assert hs.operator == ">="
        assert hs.value == 10000000
        # Ensure it was not duplicated into where filter_slots
        assert len(parsed.filter_slots) == 0

    def test_having_slot_from_structured_jev_questions(
        self, tpch_like_schema: SchemaMetadata
    ) -> None:
        """Extract HavingSlot from Jev questions (has_having_filter, having_metric, operator)."""
        mock_response = {
            "answers": {
                "primary_table": {"choice": "orders"},
                "metric": {"choice": "total_revenue"},
                "has_having_filter": {"noul": 1.0},
                "having_metric": {"choice": "total_revenue"},
                "having_operator": {"choice": "GTE"},
            }
        }
        query = "売上1000万円以上の注文"
        parsed = parse_jev_response(query, mock_response, schema=tpch_like_schema)

        assert len(parsed.having_slots) == 1
        hs = parsed.having_slots[0]
        assert hs.metric == "total_revenue"
        assert hs.operator == ">="
        assert hs.value == 10000000
        assert len(parsed.filter_slots) == 0

    def test_having_slot_fallback_from_natural_language_query(
        self, tpch_like_schema: SchemaMetadata
    ) -> None:
        """Extract HavingSlot from natural language query when aggregate keywords appear."""
        mock_response = {
            "answers": {
                "primary_table": {"choice": "orders"},
                "metric": {"choice": "total_revenue"},
                "dimension": {"choice": "orders_year"},
            }
        }
        query = "年別で売上1000万円以上の集計"
        parsed = parse_jev_response(query, mock_response, schema=tpch_like_schema)

        assert len(parsed.having_slots) == 1
        hs = parsed.having_slots[0]
        assert hs.metric == "total_revenue"
        assert hs.operator == ">="
        assert hs.value == 10000000


class TestPromptCriteriaExtension:
    """Tests for prompt and dynamic criteria extension with dimensions and having."""

    def test_dynamic_criteria_injects_semantic_dimensions(
        self, tpch_like_schema: SchemaMetadata
    ) -> None:
        """_build_dynamic_criteria must include schema.dimensions in dimension_criteria."""
        (
            _,
            _,
            dim_criteria,
            metric_criteria,
            _,
            _,
            _,
            _,
        ) = _build_dynamic_criteria(tpch_like_schema)

        assert "orders_year" in dim_criteria
        assert "年別" in dim_criteria["orders_year"]
        assert "orders_month" in dim_criteria
        assert "total_revenue" in metric_criteria

    def test_build_jev_payload_includes_dimensions_and_having_questions(
        self, tpch_like_schema: SchemaMetadata
    ) -> None:
        """build_jev_payload should construct questions for dimensions and having."""
        payload = build_jev_payload("年別かつ国別の売上1000万以上", schema=tpch_like_schema)
        questions = payload["questions"]

        assert "dimension" in questions
        assert "dimensions" in questions
        assert "has_having_filter" in questions
        assert "having_metric" in questions
        assert "having_operator" in questions

        # Verify criteria bindings
        assert "orders_year" in questions["dimensions"]["criteria"]
        assert "total_revenue" in questions["having_metric"]["criteria"]
