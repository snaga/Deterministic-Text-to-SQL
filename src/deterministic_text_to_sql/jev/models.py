"""Data models for Jev slot extraction and binding."""

from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deterministic_text_to_sql.compiler.models import FilterSlot, HavingSlot, WindowSlot


class RawSlots(BaseModel):
    """Raw slot choices extracted from Jev response before final schema binding.

    Attributes:
        primary_table: Primary focal table identified by Jev.
        secondary_table: Optional secondary table for joins or aggregations ('none' if absent).
        dimension: Grouping or projection dimension column ('none' if absent).
        dimensions: Extracted list of all grouping or projection dimension columns.
        metric: Requested metric identifier (e.g. 'count_records', 'sum_revenue').
        ranking: Top-N or ranking specification ('top_5', 'top_10', 'none').
        needs_clarification: Whether query is ambiguous and requires human clarification.
        filter_slots: Extracted filter slots.
        having_slots: Extracted HAVING aggregation filter condition slots.
        raw_response: Original raw JSON payload returned by Jev API.
    """

    model_config = ConfigDict(frozen=True)

    primary_table: str
    secondary_table: str = "none"
    dimension: str = "none"
    dimensions: list[str] = Field(default_factory=list)
    metric: str = "count_records"
    ranking: str = "none"
    needs_clarification: bool = False
    filter_slots: list[FilterSlot] = Field(default_factory=list)
    having_slots: list[HavingSlot] = Field(default_factory=list)
    raw_response: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _sync_dimensions(self) -> Self:
        """Synchronize dimension and dimensions for full backward compatibility."""
        if self.dimensions and self.dimension == "none":
            first = next((d for d in self.dimensions if d and d != "none"), self.dimensions[0])
            object.__setattr__(self, "dimension", first)
        elif self.dimension != "none" and not self.dimensions:
            object.__setattr__(self, "dimensions", [self.dimension])
        return self


class ParsedSlots(BaseModel):
    """Validated and whitelist-bound slots ready for query intent synthesis.

    Attributes:
        primary_table: Confirmed primary table in schema.
        secondary_table: Confirmed secondary table or 'none'.
        dimension: Confirmed dimension column name or 'none'.
        dimensions: Extracted list of all validated dimension column names or expressions.
        metric: Validated metric aggregation identifier.
        ranking: Validated ranking limit specification.
        needs_clarification: HITL intervention trigger flag.
        filter_slots: Whitelist-verified and deduplicated filter conditions.
        having_slots: Validated HAVING aggregation filter condition slots.
        window_slot: Optional window function operation specification.
        raw_response: Original raw response from Jev API.
    """

    model_config = ConfigDict(frozen=True)

    primary_table: str
    secondary_table: str = "none"
    dimension: str = "none"
    dimensions: list[str] = Field(default_factory=list)
    metric: str = "count_records"
    ranking: str = "none"
    needs_clarification: bool = False
    filter_slots: list[FilterSlot] = Field(default_factory=list)
    having_slots: list[HavingSlot] = Field(default_factory=list)
    window_slot: WindowSlot | None = None
    raw_response: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _sync_dimensions(self) -> Self:
        """Synchronize dimension and dimensions for full backward compatibility."""
        if self.dimensions and self.dimension == "none":
            first = next((d for d in self.dimensions if d and d != "none"), self.dimensions[0])
            object.__setattr__(self, "dimension", first)
        elif self.dimension != "none" and not self.dimensions:
            object.__setattr__(self, "dimensions", [self.dimension])
        return self
