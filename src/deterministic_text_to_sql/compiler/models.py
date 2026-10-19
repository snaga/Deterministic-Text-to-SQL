"""Data models for SQL compilation and AST building."""

from typing import Any, Literal

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


class MetricSlot(BaseModel):
    """Slot representing an aggregation metric.

    Attributes:
        column: Column name or expression to aggregate (e.g. 'o_totalprice', 'c_custkey', '*').
        agg: Aggregation function (e.g. 'SUM', 'COUNT', 'AVG', 'MIN', 'MAX').
        alias: Optional column alias in SELECT (e.g. 'total_revenue').
        expression: Optional raw SQL aggregation expression
            (e.g. 'SUM(lineitem.l_extendedprice * (1 - lineitem.l_discount))').
    """

    model_config = ConfigDict(frozen=True)

    column: str = ""
    agg: str = "SUM"
    alias: str | None = None
    expression: str | None = None


class OrderBySlot(BaseModel):
    """Slot representing sorting specification.

    Attributes:
        column: Column name or alias to order by.
        direction: Sorting direction ('ASC' or 'DESC'). Default is 'ASC'.
    """

    model_config = ConfigDict(frozen=True)

    column: str
    direction: str = "ASC"


class HavingSlot(BaseModel):
    """Slot representing a HAVING aggregation filter condition.

    Attributes:
        metric: Target aggregate metric name or column expression.
        operator: SQL comparison operator (e.g. '>=', '<=', '=', '>', '<', '!=').
        value: Filter comparison threshold value (e.g. number or literal).
    """

    model_config = ConfigDict(frozen=True)

    metric: str
    operator: str
    value: Any


class WindowSlot(BaseModel):
    """Window function operation specification for partitioned ranking and time shifts.

    Attributes:
        kind: Window operation category
            ('partitioned_rank', 'time_shift', 'moving_avg', 'cumulative').
        function: Window function name
            (e.g. 'ROW_NUMBER', 'DENSE_RANK', 'LAG', 'LEAD', 'AVG', 'SUM').
        partition_by: List of partition grouping column names.
        order_by: Window ordering specifications.
        offset: Shift offset for LAG/LEAD functions.
        limit: Upper rank limit for intra-group Top-N filtering.
        target_metric: Target metric column name for time-shift or aggregation calculations.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["partitioned_rank", "time_shift", "moving_avg", "cumulative"]
    function: str = "ROW_NUMBER"
    partition_by: list[str] = Field(default_factory=list)
    order_by: list[OrderBySlot] = Field(default_factory=list)
    offset: int = 1
    limit: int | None = None
    target_metric: str | None = None


class QueryIntent(BaseModel):
    """Normalized intent representing semantic components for deterministic SQL assembly.

    Attributes:
        primary_table: Base table for FROM clause.
        dimensions: Dimension columns (e.g. ['nation.n_name', 'orders.o_orderstatus']).
        metrics: Aggregation metrics as (col, agg) tuples or MetricSlot objects.
        filters: List of resolved filter slots.
        having: List of resolved having filter slots.
        window: Optional window function operation specification.
        limit: Optional maximum rows limit.
        order_by: Sorting rules as (col, dir) tuples or OrderBySlot objects.
    """

    model_config = ConfigDict(frozen=True)

    primary_table: str
    dimensions: list[str] = Field(default_factory=list)
    metrics: list[tuple[str, str] | tuple[str, str, str] | MetricSlot] = Field(default_factory=list)
    filters: list[FilterSlot] = Field(default_factory=list)
    having: list[HavingSlot] = Field(default_factory=list)
    window: WindowSlot | None = None
    limit: int | None = None
    order_by: list[tuple[str, str] | OrderBySlot] = Field(default_factory=list)
