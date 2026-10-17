"""Data models for Jev slot extraction and binding."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from deterministic_text_to_sql.compiler.models import FilterSlot


class RawSlots(BaseModel):
    """Raw slot choices extracted from Jev response before final schema binding.

    Attributes:
        primary_table: Primary focal table identified by Jev.
        secondary_table: Optional secondary table for joins or aggregations ('none' if absent).
        dimension: Grouping or projection dimension column ('none' if absent).
        metric: Requested metric identifier (e.g. 'count_records', 'sum_revenue').
        ranking: Top-N or ranking specification ('top_5', 'top_10', 'none').
        needs_clarification: Whether query is ambiguous and requires human clarification.
        filter_slots: Extracted filter slots.
        raw_response: Original raw JSON payload returned by Jev API.
    """

    model_config = ConfigDict(frozen=True)

    primary_table: str
    secondary_table: str = "none"
    dimension: str = "none"
    metric: str = "count_records"
    ranking: str = "none"
    needs_clarification: bool = False
    filter_slots: list[FilterSlot] = Field(default_factory=list)
    raw_response: dict[str, Any] = Field(default_factory=dict)


class ParsedSlots(BaseModel):
    """Validated and whitelist-bound slots ready for query intent synthesis.

    Attributes:
        primary_table: Confirmed primary table in schema.
        secondary_table: Confirmed secondary table or 'none'.
        dimension: Confirmed dimension column name or 'none'.
        metric: Validated metric aggregation identifier.
        ranking: Validated ranking limit specification.
        needs_clarification: HITL intervention trigger flag.
        filter_slots: Whitelist-verified and deduplicated filter conditions.
        raw_response: Original raw response from Jev API.
    """

    model_config = ConfigDict(frozen=True)

    primary_table: str
    secondary_table: str = "none"
    dimension: str = "none"
    metric: str = "count_records"
    ranking: str = "none"
    needs_clarification: bool = False
    filter_slots: list[FilterSlot] = Field(default_factory=list)
    raw_response: dict[str, Any] = Field(default_factory=dict)
