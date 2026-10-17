"""Prompt builder and payload generation for TypeSafe Jev API."""

from __future__ import annotations

from typing import Any

from deterministic_text_to_sql.schema.models import SchemaMetadata

DEFAULT_TABLE_CHOICES = [
    "customer",
    "orders",
    "lineitem",
    "supplier",
    "part",
    "partsupp",
    "nation",
    "region",
]

DEFAULT_METRIC_CHOICES = [
    "count_records",
    "sum_revenue",
    "sum_quantity",
    "avg_price",
    "sum_acctbal",
    "raw_list",
]

DEFAULT_RANKING_CHOICES = [
    "top_5",
    "top_10",
    "none",
]


def build_jev_payload(
    eval_state: str,
    schema: SchemaMetadata | None = None,
) -> dict[str, Any]:
    """Build the structured JSON payload for TypeSafe Jev System 1 classification API.

    Constructs parallel discriminator tasks for primary table, secondary table,
    grouping dimension, metric calculation, top-N ranking limit, ambiguity detection,
    and categorical filter identification.

    Args:
        eval_state: Natural language user query or prompt string.
        schema: Optional semantic schema metadata to dynamically extract valid table
            and dimension choices. If omitted, standard TPC-H choices are used.

    Returns:
        Structured payload dictionary ready for POST /v1/systemone.
    """
    if schema is not None and schema.tables:
        table_choices = [t.name for t in schema.tables]
        dimension_choices: list[str] = []
        for t in schema.tables:
            for col in t.columns:
                if col.role in ("dimension", "id"):
                    dimension_choices.append(f"{t.name}.{col.name}")
                    dimension_choices.append(col.name)
    else:
        table_choices = list(DEFAULT_TABLE_CHOICES)
        dimension_choices = [
            "c_name",
            "c_mktsegment",
            "c_custkey",
            "o_orderdate",
            "o_orderstatus",
            "o_orderpriority",
            "n_name",
            "r_name",
            "s_name",
            "p_name",
            "p_type",
        ]

    # Deduplicate choices while preserving insertion order
    table_choices = list(dict.fromkeys(table_choices))
    dimension_choices = list(dict.fromkeys(dimension_choices))

    tasks: list[dict[str, Any]] = [
        {
            "id": "primary_table",
            "question": "What is the primary focal table targeted by this query?",
            "choices": table_choices,
        },
        {
            "id": "secondary_table",
            "question": "Is there a secondary table needed for joining or filtering?",
            "choices": table_choices + ["none"],
        },
        {
            "id": "dimension",
            "question": "What is the grouping or projection dimension column?",
            "choices": dimension_choices + ["none"],
        },
        {
            "id": "metric",
            "question": "What aggregation metric is requested?",
            "choices": DEFAULT_METRIC_CHOICES,
        },
        {
            "id": "ranking",
            "question": "Is there a ranking or top-N limit specified?",
            "choices": DEFAULT_RANKING_CHOICES,
        },
        {
            "id": "ambiguity",
            "question": "Does the query contain ambiguous definitions requiring clarification?",
            "choices": ["clear", "ambiguous_revenue_definition", "ambiguous_date_range"],
        },
        {
            "id": "filters",
            "question": "What categorical or value filters are present in the query?",
            "extract_mode": "values",
        },
    ]

    return {
        "eval_state": eval_state,
        "tasks": tasks,
        "metadata": {
            "version": "1.0",
            "schema_title": schema.title if (schema and schema.title) else "TPC-H",
        },
    }
