"""Deterministic Text-to-SQL core engine orchestrator (MCP-ready decoupled architecture)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from deterministic_text_to_sql.compiler.builder import GenericSQLBuilder
from deterministic_text_to_sql.compiler.deduplicator import (
    OntologyDeduplicator,
)
from deterministic_text_to_sql.compiler.models import (
    FilterSlot,
    MetricSlot,
    OrderBySlot,
    QueryIntent,
)
from deterministic_text_to_sql.graph.resolver import SchemaGraph
from deterministic_text_to_sql.hitl.feedback import HITLManager
from deterministic_text_to_sql.jev.client import JevClient
from deterministic_text_to_sql.jev.models import ParsedSlots
from deterministic_text_to_sql.models import EngineResult, EngineStatus, LatencyMetrics
from deterministic_text_to_sql.schema.loader import (
    load_schema,
    load_schema_from_dict,
)
from deterministic_text_to_sql.schema.models import SchemaMetadata
from deterministic_text_to_sql.schema.ossie import OssieSchemaAdapter


def _build_intent_from_slots(slots: ParsedSlots, schema: SchemaMetadata) -> QueryIntent:
    """Transform validated ParsedSlots into a normalized QueryIntent model.

    Resolves table references, aligns aggregation metrics with schema roles,
    and maps ranking directives to ORDER BY and LIMIT clauses.

    Args:
        slots: Validated ParsedSlots instance from Jev.
        schema: Semantic SchemaMetadata for column and table resolution.

    Returns:
        Constructed QueryIntent ready for AST compilation.
    """
    # 1. Primary Table Resolution
    primary_tbl_meta = schema.get_table(slots.primary_table)
    primary_table = primary_tbl_meta.name if primary_tbl_meta else slots.primary_table

    # 2. Dimensions Resolution
    candidate_dims: list[str] = []
    if slots.dimensions:
        candidate_dims = [d for d in slots.dimensions if d and d.strip().lower() != "none"]
    elif slots.dimension and slots.dimension.strip().lower() != "none":
        candidate_dims = [slots.dimension]

    resolved_dimensions: list[str] = []
    for d in candidate_dims:
        dim_clean = d.strip()
        if not dim_clean or dim_clean.lower() == "none":
            continue

        # Check semantic dimensions defined in schema
        dim_def = schema.get_dimension(dim_clean)
        if dim_def is not None:
            if dim_def.name not in resolved_dimensions:
                resolved_dimensions.append(dim_def.name)
            continue

        # Fallback to physical column resolution
        resolved_col: str | None = None
        if "." in dim_clean:
            resolved_col = dim_clean
        else:
            if primary_tbl_meta and primary_tbl_meta.get_column(dim_clean):
                resolved_col = f"{primary_tbl_meta.name}.{dim_clean}"
            elif slots.secondary_table and slots.secondary_table.lower() != "none":
                sec_meta = schema.get_table(slots.secondary_table)
                if sec_meta and sec_meta.get_column(dim_clean):
                    resolved_col = f"{sec_meta.name}.{dim_clean}"
            if not resolved_col:
                for tbl in schema.tables:
                    if tbl.get_column(dim_clean):
                        resolved_col = f"{tbl.name}.{dim_clean}"
                        break
            resolved_col = resolved_col or dim_clean

        if resolved_col not in resolved_dimensions:
            resolved_dimensions.append(resolved_col)

    dimensions = resolved_dimensions

    # 3. Metrics Resolution
    metrics: list[tuple[str, str] | tuple[str, str, str] | MetricSlot] = []
    raw_metric_str = slots.metric.strip()
    metric_str = raw_metric_str.lower()

    # Step 3.1: Check semantic metrics defined in schema
    metric_def = schema.get_metric(raw_metric_str) or schema.get_metric(metric_str)
    if metric_def is not None:
        metrics.append(
            MetricSlot(
                column=metric_def.name,
                alias=metric_def.name,
                expression=metric_def.expression,
            )
        )
    # Step 3.2: Fallback heuristics for backwards compatibility
    elif metric_str in ("sum_revenue", "revenue"):
        # Determine whether orders.o_totalprice or lineitem.l_extendedprice applies
        sec_str = slots.secondary_table.lower() if slots.secondary_table else "none"
        if primary_table == "orders" or sec_str == "orders":
            metrics.append(
                MetricSlot(column="orders.o_totalprice", agg="SUM", alias="total_revenue")
            )
        elif primary_table == "lineitem" or sec_str == "lineitem":
            metrics.append(
                MetricSlot(column="lineitem.l_extendedprice", agg="SUM", alias="total_revenue")
            )
        elif primary_table == "customer":
            if sec_str == "lineitem":
                metrics.append(
                    MetricSlot(column="lineitem.l_extendedprice", agg="SUM", alias="total_revenue")
                )
            else:
                metrics.append(
                    MetricSlot(column="orders.o_totalprice", agg="SUM", alias="total_revenue")
                )
        else:
            # Fallback to schema search
            orders_tbl = schema.get_table("orders")
            lineitem_tbl = schema.get_table("lineitem")
            if orders_tbl and orders_tbl.get_column("o_totalprice"):
                metrics.append(
                    MetricSlot(column="orders.o_totalprice", agg="SUM", alias="total_revenue")
                )
            elif lineitem_tbl and lineitem_tbl.get_column("l_extendedprice"):
                metrics.append(
                    MetricSlot(column="lineitem.l_extendedprice", agg="SUM", alias="total_revenue")
                )
            else:
                metrics.append(MetricSlot(column="*", agg="COUNT", alias="count"))

    elif metric_str in ("count_records", "count"):
        metrics.append(MetricSlot(column="*", agg="COUNT", alias="count"))

    elif metric_str in ("sum_quantity", "quantity"):
        metrics.append(MetricSlot(column="lineitem.l_quantity", agg="SUM", alias="total_quantity"))

    elif metric_str in ("avg_revenue", "avg_price"):
        sec_str = slots.secondary_table.lower() if slots.secondary_table else "none"
        target_col = "orders.o_totalprice"
        if primary_table == "lineitem" or sec_str == "lineitem":
            target_col = "lineitem.l_extendedprice"
        metrics.append(MetricSlot(column=target_col, agg="AVG", alias="avg_revenue"))

    elif metric_str in ("sum_acctbal", "acctbal"):
        col = "s_acctbal" if primary_table == "supplier" else "c_acctbal"
        metrics.append(
            MetricSlot(column=f"{primary_table}.{col}", agg="SUM", alias="total_acctbal")
        )

    else:
        # Check standard agg prefix (e.g. sum_foo, avg_foo, count_foo)
        handled = False
        for prefix, agg_op in [
            ("sum_", "SUM"),
            ("avg_", "AVG"),
            ("count_", "COUNT"),
            ("min_", "MIN"),
            ("max_", "MAX"),
        ]:
            if metric_str.startswith(prefix):
                candidate_col = metric_str[len(prefix) :]
                metrics.append(
                    MetricSlot(column=candidate_col, agg=agg_op, alias=candidate_col)
                )
                handled = True
                break

        if not handled:
            # Check if metric_str is a physical column in primary table or schema
            pt = schema.get_table(primary_table)
            col_meta = pt.get_column(metric_str) if pt else None
            if col_meta:
                metrics.append(
                    MetricSlot(column=col_meta.name, agg="MAX", alias=col_meta.name)
                )
                handled = True

        if not handled:
            metrics.append(MetricSlot(column="*", agg="COUNT", alias="count"))

    # 4. Ranking (LIMIT and ORDER BY) Resolution
    limit: int | None = None
    order_by: list[tuple[str, str] | OrderBySlot] = []
    ranking_str = slots.ranking.strip().lower()

    if ranking_str != "none":
        if metrics:
            m0 = metrics[0]
            if isinstance(m0, MetricSlot):
                sort_target = m0.alias or m0.column
            elif len(m0) == 3:
                sort_target = m0[2] or m0[0]
            else:
                sort_target = m0[0]
        elif dimensions:
            sort_target = dimensions[0]
        else:
            sort_target = "*"

        if ranking_str.startswith("top_"):
            try:
                limit = int(ranking_str.replace("top_", ""))
            except ValueError:
                limit = 5
            order_by = [OrderBySlot(column=sort_target, direction="DESC")]
        elif ranking_str.startswith("bottom_"):
            try:
                limit = int(ranking_str.replace("bottom_", ""))
            except ValueError:
                limit = 5
            order_by = [OrderBySlot(column=sort_target, direction="ASC")]

    # 5. Filters
    filters: list[FilterSlot] = list(slots.filter_slots)

    return QueryIntent(
        primary_table=primary_table,
        dimensions=dimensions,
        metrics=metrics,
        filters=filters,
        having=list(slots.having_slots),
        window=slots.window_slot,
        limit=limit,
        order_by=order_by,
    )


class DeterministicTextToSQLEngine:
    """Core orchestrator service for deterministic Text-to-SQL synthesis.

    Completely decoupled from presentation frameworks (Typer, Rich) and future
    transport protocols (MCP, FastAPI). Can be integrated directly into any Python
    service or MCP tool handler in a single call.
    """

    def __init__(
        self,
        schema: SchemaMetadata | Path | str | dict[str, Any],
        jev_client: JevClient | None = None,
        api_key: str | None = None,
    ) -> None:
        """Initialize the deterministic engine with schema metadata and components.

        Args:
            schema: Schema metadata instance, file path, JSON string, or dict.
            jev_client: Optional pre-configured JevClient instance.
            api_key: Optional TypeSafe API key used if jev_client is instantiated internally.

        Raises:
            SchemaValidationError: If schema fails validation or loading.
        """
        # 1. Load SchemaMetadata
        if isinstance(schema, SchemaMetadata):
            self._schema = schema
        elif isinstance(schema, dict):
            if "datasets" in schema:
                self._schema = OssieSchemaAdapter.load_from_dict(schema)
            else:
                self._schema = load_schema_from_dict(schema)
        elif isinstance(schema, (Path, str)):
            p = Path(schema) if isinstance(schema, str) else schema
            if p.exists() and p.is_file():
                self._schema = load_schema(p)
            elif isinstance(schema, str) and schema.strip().startswith("{"):
                try:
                    data = json.loads(schema)
                    if isinstance(data, dict) and "datasets" in data:
                        self._schema = OssieSchemaAdapter.load_from_dict(data)
                    else:
                        self._schema = load_schema_from_dict(data)
                except json.JSONDecodeError:
                    self._schema = load_schema(schema)
            else:
                self._schema = load_schema(schema)
        else:
            raise TypeError(f"Unsupported schema type: {type(schema).__name__}")

        # 2. Configure Jev Client
        if jev_client is not None:
            self._jev_client = jev_client
            self._owns_client = False
        else:
            self._jev_client = JevClient(api_key=api_key)
            self._owns_client = True

        # 3. Configure Core Subsystems
        self._schema_graph = SchemaGraph(self._schema)
        self._deduplicator = OntologyDeduplicator.from_schema(self._schema)
        self._sql_builder = GenericSQLBuilder(
            schema_graph=self._schema_graph,
            deduplicator=self._deduplicator,
        )
        self._hitl_manager = HITLManager()

    @property
    def schema(self) -> SchemaMetadata:
        """Return the loaded SchemaMetadata."""
        return self._schema

    @property
    def schema_graph(self) -> SchemaGraph:
        """Return the underlying SchemaGraph."""
        return self._schema_graph

    def __enter__(self) -> DeterministicTextToSQLEngine:
        """Enter context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Exit context manager and release client resources."""
        self.close()

    def close(self) -> None:
        """Release underlying HTTP client resources if internally owned."""
        if self._owns_client and self._jev_client is not None:
            self._jev_client.close()

    def process_query(
        self,
        raw_query: str,
        dialect: str = "postgres",
        clarify: str | None = None,
        force: bool = False,
        clarification: str | None = None,
    ) -> EngineResult:
        """Process a natural language query end-to-end to generate deterministic SQL.

        Pipeline Stages:
            1. Parse query input for slash clarification syntax or CLI argument.
            2. Synthesize disambiguated prompt context.
            3. Call Jev System 1 API for low-latency slot extraction.
            4. Inspect ambiguity: return HITL intervention request unless force=True.
            5. Transform validated slots to QueryIntent.
            6. Resolve joins via SchemaGraph and compile AST via GenericSQLBuilder.

        Args:
            raw_query: User query string (e.g. 'アジアの顧客の売上トップ5 / Aで').
            dialect: Target SQL dialect (e.g. 'postgres', 'duckdb', 'sqlite', 'snowflake').
            clarify: Disambiguation key (e.g. 'A', 'B', 'lineitem') overriding slash syntax.
            force: If True, bypass HITL intervention and synthesize best-effort SQL.
            clarification: Alias for clarify parameter.

        Returns:
            EngineResult containing status, compiled SQL, HITL issues, or error messages.
        """
        t0 = time.perf_counter()
        jev_latency_ms = 0.0
        ast_latency_ms = 0.0

        effective_clarify_arg = clarification if clarification is not None else clarify

        # Validate input query
        if not raw_query or not raw_query.strip():
            total_ms = (time.perf_counter() - t0) * 1000.0
            return EngineResult(
                status=EngineStatus.ERROR,
                query=raw_query,
                clarification=effective_clarify_arg,
                dialect=dialect,
                error_message="Query string cannot be empty or whitespace.",
                latency_ms=LatencyMetrics(
                    jev_inference=0.0,
                    ast_build=0.0,
                    total=total_ms,
                ),
            )

        try:
            # 1. Parse Input & Clarification
            parsed_input = self._hitl_manager.parse_input(
                raw_query, clarify_arg=effective_clarify_arg
            )
            base_query = parsed_input.base_query
            effective_clarify = parsed_input.clarification

            if not base_query:
                base_query = raw_query.strip()

            # 2. Synthesize Prompt Context for Jev
            eval_query = self._hitl_manager.synthesize_eval_state(
                base_query, effective_clarify
            )

            # 3. Jev Inference
            t_jev_start = time.perf_counter()
            slots = self._jev_client.extract_slots(eval_query, schema=self._schema)
            t_jev_end = time.perf_counter()
            jev_latency_ms = (t_jev_end - t_jev_start) * 1000.0

            hitl_summary = self._hitl_manager.format_hitl_summary(slots)

            # 4. HITL Ambiguity Check (unless bypassed by force=True)
            if not force:
                hitl_issue = self._hitl_manager.check_ambiguity(
                    slots, clarification=effective_clarify, schema=self._schema
                )
                if hitl_issue is not None:
                    # Ensure base_query is accurately populated
                    hitl_issue = hitl_issue.model_copy(update={"base_query": base_query})
                    total_ms = (time.perf_counter() - t0) * 1000.0
                    return EngineResult(
                        status=EngineStatus.HITL_REQUIRED,
                        query=base_query,
                        clarification=effective_clarify,
                        dialect=dialect,
                        hitl_issue=hitl_issue,
                        hitl_summary=hitl_summary,
                        latency_ms=LatencyMetrics(
                            jev_inference=jev_latency_ms,
                            ast_build=0.0,
                            total=total_ms,
                        ),
                    )

            # 5. Transform Slots to QueryIntent
            intent = _build_intent_from_slots(slots, self._schema)

            # 6. AST Compilation & Transpilation
            t_ast_start = time.perf_counter()
            sql = self._sql_builder.build(intent, dialect=dialect)
            t_ast_end = time.perf_counter()
            ast_latency_ms = (t_ast_end - t_ast_start) * 1000.0

            total_ms = (time.perf_counter() - t0) * 1000.0
            return EngineResult(
                status=EngineStatus.SUCCESS,
                query=base_query,
                clarification=effective_clarify,
                dialect=dialect,
                sql=sql,
                intent=intent,
                hitl_summary=hitl_summary,
                latency_ms=LatencyMetrics(
                    jev_inference=jev_latency_ms,
                    ast_build=ast_latency_ms,
                    total=total_ms,
                ),
            )

        except Exception as e:
            total_ms = (time.perf_counter() - t0) * 1000.0
            return EngineResult(
                status=EngineStatus.ERROR,
                query=raw_query,
                clarification=clarify,
                dialect=dialect,
                error_message=str(e),
                latency_ms=LatencyMetrics(
                    jev_inference=jev_latency_ms,
                    ast_build=ast_latency_ms,
                    total=total_ms,
                ),
            )

    def get_schema_info(self) -> dict[str, Any]:
        """Retrieve schema metadata summary for agent inspection and MCP resource provisioning.

        Returns:
            Dictionary containing table definitions, columns, roles, and allowed values.
        """
        tables_info: list[dict[str, Any]] = []
        for tbl in self._schema.tables:
            tables_info.append(
                {
                    "name": tbl.name,
                    "description": tbl.description,
                    "aliases": tbl.aliases,
                    "primary_key": tbl.primary_key,
                    "foreign_keys": [
                        {
                            "column": fk.column,
                            "ref_table": fk.ref_table,
                            "ref_column": fk.ref_column,
                        }
                        for fk in tbl.foreign_keys
                    ],
                    "columns": [
                        {
                            "name": col.name,
                            "type": col.type,
                            "description": col.description,
                            "role": col.role,
                            "agg": col.agg,
                            "values": col.values,
                        }
                        for col in tbl.columns
                    ],
                }
            )

        dimensions_info: list[dict[str, Any]] = [
            {
                "name": dim.name,
                "expression": dim.expression,
                "description": dim.description,
                "synonyms": list(dim.synonyms),
                "is_time": dim.is_time,
                "time_granularity": dim.time_granularity,
            }
            for dim in self._schema.dimensions
        ]

        metrics_info: list[dict[str, Any]] = [
            {
                "name": m.name,
                "expression": m.expression,
                "description": m.description,
                "synonyms": m.synonyms,
            }
            for m in self._schema.metrics
        ]

        hierarchies_info: list[dict[str, Any]] = [
            {
                "parent_table": h.parent_table,
                "parent_column": h.parent_column,
                "child_table": h.child_table,
                "child_column": h.child_column,
                "child_to_parent": h.child_to_parent,
            }
            for h in self._schema.hierarchies
        ]

        return {
            "title": self._schema.title,
            "description": self._schema.description,
            "table_names": [t.name for t in self._schema.tables],
            "tables": tables_info,
            "dimensions": dimensions_info,
            "metrics": metrics_info,
            "hierarchies": hierarchies_info,
        }
