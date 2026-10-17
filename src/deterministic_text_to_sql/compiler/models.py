"""Data models for SQL compilation and AST building."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FilterSlot(BaseModel):
    """Slot representing a WHERE/HAVING filter condition.

    Attributes:
        table: Target table name.
        column: Target column name.
        operator: SQL operator (e.g. '=', '!=', '>', '>=', '<', '<=', 'LIKE', 'IN').
        value: Filter comparison value (e.g. string, number, or list of values).
        display_label: Optional human-readable description for UI/HITL display.
    """

    model_config = ConfigDict(frozen=True)

    table: str
    column: str
    operator: str
    value: Any
    display_label: str | None = None


class QueryIntent(BaseModel):
    """Normalized intent representing semantic components for deterministic SQL assembly.

    Attributes:
        primary_table: Base table for FROM clause.
        dimensions: Dimension columns (e.g. ['nation.n_name', 'orders.o_orderstatus']).
        metrics: Aggregation metrics as (col_or_expr, agg_func) tuples (e.g. [('price', 'SUM')]).
        filters: List of resolved filter slots.
        limit: Optional maximum rows limit.
        order_by: Sorting rules as (column_or_alias, direction) tuples (e.g. [('rev', 'DESC')]).
    """

    model_config = ConfigDict(frozen=True)

    primary_table: str
    dimensions: list[str] = Field(default_factory=list)
    metrics: list[tuple[str, str]] = Field(default_factory=list)
    filters: list[FilterSlot] = Field(default_factory=list)
    limit: int | None = None
    order_by: list[tuple[str, str]] = Field(default_factory=list)
