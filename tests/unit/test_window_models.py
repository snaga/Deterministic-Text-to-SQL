"""Unit tests for WindowSlot, QueryIntent window integration, and ParsedSlots window integration."""

import pytest
from pydantic import ValidationError

from deterministic_text_to_sql.compiler.models import OrderBySlot, QueryIntent, WindowSlot
from deterministic_text_to_sql.jev.models import ParsedSlots


class TestWindowSlot:
    """Tests for WindowSlot Pydantic model."""

    def test_default_values(self) -> None:
        """Verify default values upon instantiation with required kind."""
        slot = WindowSlot(kind="partitioned_rank")
        assert slot.kind == "partitioned_rank"
        assert slot.function == "ROW_NUMBER"
        assert slot.partition_by == []
        assert slot.order_by == []
        assert slot.offset == 1
        assert slot.limit is None
        assert slot.target_metric is None

    def test_full_instantiation_partitioned_rank(self) -> None:
        """Verify full instantiation for partitioned rank window slot."""
        order_by = [OrderBySlot(column="total_revenue", direction="DESC")]
        slot = WindowSlot(
            kind="partitioned_rank",
            function="DENSE_RANK",
            partition_by=["region.r_name"],
            order_by=order_by,
            offset=1,
            limit=3,
            target_metric="total_revenue",
        )
        assert slot.kind == "partitioned_rank"
        assert slot.function == "DENSE_RANK"
        assert slot.partition_by == ["region.r_name"]
        assert slot.order_by == order_by
        assert slot.offset == 1
        assert slot.limit == 3
        assert slot.target_metric == "total_revenue"

    def test_full_instantiation_time_shift(self) -> None:
        """Verify full instantiation for time shift (YoY / MoM) window slot."""
        order_by = [OrderBySlot(column="order_year", direction="ASC")]
        slot = WindowSlot(
            kind="time_shift",
            function="LAG",
            partition_by=["nation.n_name"],
            order_by=order_by,
            offset=1,
            limit=None,
            target_metric="total_revenue",
        )
        assert slot.kind == "time_shift"
        assert slot.function == "LAG"
        assert slot.partition_by == ["nation.n_name"]
        assert slot.order_by == order_by
        assert slot.offset == 1
        assert slot.limit is None
        assert slot.target_metric == "total_revenue"

    def test_all_supported_kinds(self) -> None:
        """Verify all literal kinds can be instantiated."""
        for kind in ("partitioned_rank", "time_shift", "moving_avg", "cumulative"):
            slot = WindowSlot(kind=kind)
            assert slot.kind == kind

    def test_invalid_kind_raises_validation_error(self) -> None:
        """Verify that unsupported kind raises ValidationError."""
        with pytest.raises(ValidationError):
            WindowSlot(kind="invalid_kind")  # type: ignore[arg-type]

    def test_immutability_frozen(self) -> None:
        """Verify that WindowSlot instances are immutable (frozen=True)."""
        slot = WindowSlot(kind="partitioned_rank", function="ROW_NUMBER")
        with pytest.raises(ValidationError):
            slot.function = "LAG"

        with pytest.raises(ValidationError):
            slot.limit = 5


class TestQueryIntentWindowIntegration:
    """Tests for QueryIntent window slot integration."""

    def test_default_query_intent_window_is_none(self) -> None:
        """Verify that QueryIntent defaults to window=None."""
        intent = QueryIntent(primary_table="orders")
        assert intent.window is None

    def test_query_intent_with_window(self) -> None:
        """Verify QueryIntent initialization with WindowSlot."""
        window = WindowSlot(
            kind="partitioned_rank",
            function="ROW_NUMBER",
            partition_by=["region.r_name"],
            order_by=[OrderBySlot(column="total_revenue", direction="DESC")],
            limit=3,
        )
        intent = QueryIntent(primary_table="orders", window=window)
        assert intent.window == window
        assert intent.window.limit == 3
        assert intent.window.partition_by == ["region.r_name"]

    def test_query_intent_dict_serialization(self) -> None:
        """Verify QueryIntent serialization with window slot."""
        window = WindowSlot(
            kind="partitioned_rank",
            partition_by=["region.r_name"],
            limit=3,
        )
        intent = QueryIntent(primary_table="orders", window=window)
        data = intent.model_dump()
        assert "window" in data
        assert data["window"] is not None
        assert data["window"]["kind"] == "partitioned_rank"
        assert data["window"]["function"] == "ROW_NUMBER"
        assert data["window"]["partition_by"] == ["region.r_name"]
        assert data["window"]["limit"] == 3

    def test_query_intent_window_immutability(self) -> None:
        """Verify that QueryIntent cannot have window reassigned (frozen=True)."""
        intent = QueryIntent(primary_table="orders")
        with pytest.raises(ValidationError):
            intent.window = WindowSlot(kind="partitioned_rank")


class TestParsedSlotsWindowIntegration:
    """Tests for ParsedSlots window slot integration."""

    def test_default_parsed_slots_window_slot_is_none(self) -> None:
        """Verify that ParsedSlots defaults to window_slot=None."""
        parsed = ParsedSlots(primary_table="orders")
        assert parsed.window_slot is None

    def test_parsed_slots_with_window_slot(self) -> None:
        """Verify ParsedSlots initialization with window_slot."""
        window = WindowSlot(
            kind="time_shift",
            function="LAG",
            offset=1,
            target_metric="total_revenue",
        )
        parsed = ParsedSlots(primary_table="orders", window_slot=window)
        assert parsed.window_slot == window
        assert parsed.window_slot.kind == "time_shift"
        assert parsed.window_slot.offset == 1
        assert parsed.window_slot.target_metric == "total_revenue"

    def test_parsed_slots_dict_serialization(self) -> None:
        """Verify ParsedSlots serialization with window_slot."""
        window = WindowSlot(
            kind="moving_avg",
            function="AVG",
            offset=3,
        )
        parsed = ParsedSlots(primary_table="orders", window_slot=window)
        data = parsed.model_dump()
        assert "window_slot" in data
        assert data["window_slot"] is not None
        assert data["window_slot"]["kind"] == "moving_avg"
        assert data["window_slot"]["function"] == "AVG"
        assert data["window_slot"]["offset"] == 3

    def test_parsed_slots_window_immutability(self) -> None:
        """Verify that ParsedSlots cannot have window_slot reassigned (frozen=True)."""
        parsed = ParsedSlots(primary_table="orders")
        with pytest.raises(ValidationError):
            parsed.window_slot = WindowSlot(kind="partitioned_rank")
