"""Detroit-school unit tests for Jev window operation slot extraction and prompt extensions."""

from __future__ import annotations

import pytest

from deterministic_text_to_sql.compiler.models import OrderBySlot
from deterministic_text_to_sql.jev.prompts import (
    WINDOW_OPERATION_CRITERIA,
    build_jev_payload,
)
from deterministic_text_to_sql.jev.slots import parse_jev_response
from deterministic_text_to_sql.schema.loader import load_schema
from deterministic_text_to_sql.schema.models import (
    ColumnMetadata,
    DimensionMetadata,
    SchemaMetadata,
    TableMetadata,
)


@pytest.fixture
def tpch_schema() -> SchemaMetadata:
    """Load standard TPC-H schema fixture."""
    return load_schema("tests/fixtures/tpch_schema.json")


@pytest.fixture
def custom_schema() -> SchemaMetadata:
    """Create a minimal custom schema with dimensions and metrics."""
    return SchemaMetadata(
        tables=[
            TableMetadata(
                name="sales",
                description="売上テーブル",
                columns=[
                    ColumnMetadata(name="sale_id", type="INT", role="id", description="売上ID"),
                    ColumnMetadata(
                        name="branch_code",
                        type="VARCHAR(10)",
                        role="dimension",
                        description="支店コード",
                    ),
                    ColumnMetadata(
                        name="amount",
                        type="DECIMAL",
                        role="metric",
                        agg=["SUM"],
                        description="売上金額",
                    ),
                ],
            )
        ],
        dimensions=[
            DimensionMetadata(
                name="branch_code",
                expression="sales.branch_code",
                description="支店コード",
            )
        ],
    )


class TestWindowOperationPrompts:
    """Tests for Jev prompt extension and Criteria definitions."""

    def test_window_operation_criteria_constants_defined(self) -> None:
        """Verify WINDOW_OPERATION_CRITERIA dictionary contains required operation types."""
        assert "partitioned_rank" in WINDOW_OPERATION_CRITERIA
        assert "time_shift" in WINDOW_OPERATION_CRITERIA
        assert "moving_avg" in WINDOW_OPERATION_CRITERIA
        assert "cumulative" in WINDOW_OPERATION_CRITERIA
        assert "none" in WINDOW_OPERATION_CRITERIA

    def test_build_jev_payload_contains_window_questions(self) -> None:
        """Verify build_jev_payload includes window_operation and window_partition_by questions."""
        payload = build_jev_payload("各地域ごとの売上トップ3顧客")
        questions = payload["questions"]

        assert "window_operation" in questions
        assert questions["window_operation"]["type"] == "choice"
        assert questions["window_operation"]["criteria"] == WINDOW_OPERATION_CRITERIA

        assert "window_partition_by" in questions
        assert questions["window_partition_by"]["type"] == "choice"
        assert "r_name" in questions["window_partition_by"]["criteria"]

    def test_build_jev_payload_with_custom_schema(self, custom_schema: SchemaMetadata) -> None:
        """Verify dynamic criteria in window_partition_by when custom schema is provided."""
        payload = build_jev_payload("支店別売上トップ3", schema=custom_schema)
        questions = payload["questions"]

        assert "window_operation" in questions
        assert "window_partition_by" in questions
        criteria = questions["window_partition_by"]["criteria"]
        assert "branch_code" in criteria


class TestWindowSlotPartitionedRank:
    """Tests for intra-group Top-N (partitioned_rank) slot extraction."""

    def test_extract_partitioned_rank_top_3_with_schema(
        self, tpch_schema: SchemaMetadata
    ) -> None:
        """Verify '各地域ごとの売上トップ3顧客' extracts WindowSlot and resets overall ranking."""
        query = "各地域ごとの売上トップ3顧客"
        response_data = {
            "answers": {
                "primary_table": {"choice": "customer"},
                "dimension": {"choice": "c_name"},
                "metric": {"choice": "sum_revenue"},
                "ranking": {"choice": "top_5"},  # LLM erroneously or redundantly selected top_5
                "window_operation": {"choice": "partitioned_rank"},
                "window_partition_by": {"choice": "region.r_name"},
            }
        }

        parsed = parse_jev_response(query, response_data, schema=tpch_schema)

        assert parsed.window_slot is not None
        assert parsed.window_slot.kind == "partitioned_rank"
        assert parsed.window_slot.function == "ROW_NUMBER"
        assert parsed.window_slot.partition_by == ["region.r_name"]
        assert parsed.window_slot.limit == 3
        assert parsed.window_slot.target_metric == "sum_revenue"
        assert parsed.window_slot.order_by == [
            OrderBySlot(column="sum_revenue", direction="DESC")
        ]
        # Crucial requirement: parsed.ranking must be reset to "none" to prevent dual LIMIT
        assert parsed.ranking == "none"

    def test_extract_partitioned_rank_short_column_resolution(
        self, tpch_schema: SchemaMetadata
    ) -> None:
        """Verify partition column 'r_name' is resolved to 'region.r_name'."""
        query = "各地域ごとの売上トップ3顧客"
        response_data = {
            "answers": {
                "primary_table": {"choice": "customer"},
                "dimension": {"choice": "c_name"},
                "metric": {"choice": "sum_revenue"},
                "window_operation": {"choice": "partitioned_rank"},
                "window_partition_by": {"choice": "r_name"},
            }
        }

        parsed = parse_jev_response(query, response_data, schema=tpch_schema)

        assert parsed.window_slot is not None
        assert parsed.window_slot.partition_by == ["region.r_name"]
        assert parsed.window_slot.limit == 3

    def test_extract_partitioned_rank_limit_from_ranking_fallback(
        self, tpch_schema: SchemaMetadata
    ) -> None:
        """Verify limit is deduced from ranking='top_5' when no specific number in query."""
        query = "各地域ごとの売上上位顧客"
        response_data = {
            "primary_table": "customer",
            "dimension": "c_name",
            "metric": "sum_revenue",
            "ranking": "top_5",
            "window_operation": "partitioned_rank",
            "window_partition_by": "region.r_name",
        }

        parsed = parse_jev_response(query, response_data, schema=tpch_schema)

        assert parsed.window_slot is not None
        assert parsed.window_slot.limit == 5
        assert parsed.ranking == "none"

    def test_extract_partitioned_rank_explicit_window_limit(
        self, tpch_schema: SchemaMetadata
    ) -> None:
        """Verify limit from window_limit in slots_map takes precedence."""
        query = "各地域ごとの売上上位顧客"
        response_data = {
            "primary_table": "customer",
            "metric": "sum_revenue",
            "window_limit": 10,
            "window_operation": "partitioned_rank",
            "window_partition_by": "region.r_name",
        }

        parsed = parse_jev_response(query, response_data, schema=tpch_schema)

        assert parsed.window_slot is not None
        assert parsed.window_slot.limit == 10
        assert parsed.ranking == "none"


class TestWindowSlotTimeShift:
    """Tests for time-series shift (YoY / MoM) slot extraction."""

    def test_extract_time_shift_yoy(self, tpch_schema: SchemaMetadata) -> None:
        """Verify '年別の売上および前年比（YoY）' extracts time_shift with LAG function."""
        query = "年別の売上および前年比（YoY）"
        response_data = {
            "answers": {
                "primary_table": {"choice": "orders"},
                "dimension": {"choice": "orders_year"},
                "metric": {"choice": "sum_revenue"},
                "ranking": {"choice": "none"},
                "window_operation": {"choice": "time_shift"},
                "window_partition_by": {"choice": "none"},
            }
        }

        parsed = parse_jev_response(query, response_data, schema=tpch_schema)

        assert parsed.window_slot is not None
        assert parsed.window_slot.kind == "time_shift"
        assert parsed.window_slot.function == "LAG"
        assert parsed.window_slot.offset == 1
        assert parsed.window_slot.target_metric == "sum_revenue"
        assert parsed.window_slot.partition_by == []
        assert parsed.window_slot.limit is None

    def test_extract_time_shift_with_partition(self, tpch_schema: SchemaMetadata) -> None:
        """Verify time_shift with partition key (e.g. nation-wise YoY)."""
        query = "国別・年別の売上および前年比"
        response_data = {
            "answers": {
                "primary_table": {"choice": "orders"},
                "dimension": {"choice": "orders_year"},
                "metric": {"choice": "sum_revenue"},
                "window_operation": {"choice": "time_shift"},
                "window_partition_by": {"choice": "nation.n_name"},
            }
        }

        parsed = parse_jev_response(query, response_data, schema=tpch_schema)

        assert parsed.window_slot is not None
        assert parsed.window_slot.kind == "time_shift"
        assert parsed.window_slot.partition_by == ["nation.n_name"]
        assert parsed.window_slot.target_metric == "sum_revenue"


class TestWindowSlotOtherOperations:
    """Tests for moving average and cumulative calculations."""

    def test_extract_moving_avg(self, tpch_schema: SchemaMetadata) -> None:
        """Verify moving_avg maps to AVG window function."""
        query = "3ヶ月移動平均売上"
        response_data = {
            "primary_table": "orders",
            "metric": "sum_revenue",
            "window_operation": "moving_avg",
        }

        parsed = parse_jev_response(query, response_data, schema=tpch_schema)

        assert parsed.window_slot is not None
        assert parsed.window_slot.kind == "moving_avg"
        assert parsed.window_slot.function == "AVG"
        assert parsed.window_slot.target_metric == "sum_revenue"

    def test_extract_cumulative(self, tpch_schema: SchemaMetadata) -> None:
        """Verify cumulative maps to SUM window function."""
        query = "累積売上推移"
        response_data = {
            "primary_table": "orders",
            "metric": "sum_revenue",
            "window_operation": "cumulative",
        }

        parsed = parse_jev_response(query, response_data, schema=tpch_schema)

        assert parsed.window_slot is not None
        assert parsed.window_slot.kind == "cumulative"
        assert parsed.window_slot.function == "SUM"
        assert parsed.window_slot.target_metric == "sum_revenue"


class TestWindowSlotBackwardCompatibility:
    """Tests ensuring backward compatibility and non-window query handling."""

    def test_window_operation_none_retains_ranking(self, tpch_schema: SchemaMetadata) -> None:
        """Verify window_operation='none' sets window_slot=None and retains ranking='top_5'."""
        query = "売上上位5顧客"
        response_data = {
            "primary_table": "customer",
            "dimension": "c_name",
            "metric": "sum_revenue",
            "ranking": "top_5",
            "window_operation": "none",
        }

        parsed = parse_jev_response(query, response_data, schema=tpch_schema)

        assert parsed.window_slot is None
        assert parsed.ranking == "top_5"

    def test_omitted_window_operation_retains_ranking(self, tpch_schema: SchemaMetadata) -> None:
        """Verify legacy response without window_operation field produces window_slot=None."""
        query = "売上上位5顧客"
        response_data = {
            "primary_table": "customer",
            "dimension": "c_name",
            "metric": "sum_revenue",
            "ranking": "top_5",
        }

        parsed = parse_jev_response(query, response_data, schema=tpch_schema)

        assert parsed.window_slot is None
        assert parsed.ranking == "top_5"


class TestWindowSlotFallbackAndSafety:
    """Tests verifying safe fallback on invalid keys or missing parameters."""

    def test_invalid_partition_by_safely_ignored_with_schema(
        self, tpch_schema: SchemaMetadata
    ) -> None:
        """Verify nonexistent partition column is safely filtered without raising exception."""
        query = "グループごとの上位3件"
        response_data = {
            "primary_table": "customer",
            "metric": "sum_revenue",
            "window_operation": "partitioned_rank",
            "window_partition_by": "completely_fake_column_xyz",
        }

        parsed = parse_jev_response(query, response_data, schema=tpch_schema)

        assert parsed.window_slot is not None
        assert parsed.window_slot.partition_by == []
        assert parsed.window_slot.limit == 3

    def test_none_partition_by_produces_empty_list(self, tpch_schema: SchemaMetadata) -> None:
        """Verify 'none' partition by results in empty partition_by list."""
        query = "売上上位3件"
        response_data = {
            "primary_table": "customer",
            "metric": "sum_revenue",
            "window_operation": "partitioned_rank",
            "window_partition_by": "none",
        }

        parsed = parse_jev_response(query, response_data, schema=tpch_schema)

        assert parsed.window_slot is not None
        assert parsed.window_slot.partition_by == []
        assert parsed.window_slot.limit == 3

    def test_unknown_window_operation_ignored(self, tpch_schema: SchemaMetadata) -> None:
        """Verify unsupported window operation string produces window_slot=None."""
        query = "売上集計"
        response_data = {
            "primary_table": "customer",
            "metric": "sum_revenue",
            "window_operation": "unsupported_magic_op",
        }

        parsed = parse_jev_response(query, response_data, schema=tpch_schema)

        assert parsed.window_slot is None

    def test_legacy_tpch_prefix_resolution_without_schema(self) -> None:
        """Verify schema=None legacy prefix resolution resolves r_name to region.r_name."""
        query = "地域別売上トップ3顧客"
        response_data = {
            "primary_table": "customer",
            "metric": "sum_revenue",
            "window_operation": "partitioned_rank",
            "window_partition_by": "r_name",
        }

        parsed = parse_jev_response(query, response_data, schema=None)

        assert parsed.window_slot is not None
        assert parsed.window_slot.partition_by == ["region.r_name"]
        assert parsed.window_slot.limit == 3
