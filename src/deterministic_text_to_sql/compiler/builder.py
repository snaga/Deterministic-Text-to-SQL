"""Type-safe deterministic SQL query builder based on sqlglot AST nodes."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.dialects.sqlite import SQLite

from deterministic_text_to_sql.compiler.deduplicator import OntologyDeduplicator
from deterministic_text_to_sql.compiler.exceptions import SQLBuildError
from deterministic_text_to_sql.compiler.models import (
    FilterSlot,
    HavingSlot,
    MetricSlot,
    OrderBySlot,
    QueryIntent,
)
from deterministic_text_to_sql.graph.exceptions import UnconnectedSchemaError
from deterministic_text_to_sql.graph.resolver import SchemaGraph


def _sqlite_extract_transform(generator: Any, expression: exp.Extract) -> str:
    """Transform ANSI EXTRACT expression to SQLite STRFTIME function call."""
    part_node = expression.this
    part_str = (
        part_node.name.lower()
        if hasattr(part_node, "name")
        else str(part_node).lower()
    )
    fmt_map = {
        "year": "%Y",
        "month": "%m",
        "day": "%d",
        "hour": "%H",
        "minute": "%M",
        "second": "%S",
    }
    fmt = fmt_map.get(part_str, "%Y")
    arg = generator.sql(expression, "expression")
    return f"STRFTIME('{fmt}', {arg})"


SQLite.Generator.TRANSFORMS[exp.Extract] = _sqlite_extract_transform


@dataclass
class _QueryComponents:
    """Internal container for prepared AST components before query assembly."""

    primary_table: str
    select_nodes: list[exp.Expr]
    group_nodes: list[exp.Expr]
    where_conditions: list[exp.Expr]
    having_conditions: list[exp.Expr]
    join_steps: list[Any]
    alias_to_expr: dict[str, exp.Expr]
    dim_to_alias: dict[str, str]
    dim_to_expr: dict[str, exp.Expr]
    dim_output_names: list[str] = field(default_factory=list)
    dim_output_nodes: list[tuple[exp.Expr, str]] = field(default_factory=list)
    metric_col_to_alias: dict[str, str] = field(default_factory=dict)
    metric_col_to_expr: dict[str, exp.Expr] = field(default_factory=dict)
    metric_output_names: list[str] = field(default_factory=list)
    metric_output_nodes: list[tuple[exp.Expr, str]] = field(default_factory=list)
    metric_agg_nodes: list[exp.Expr] = field(default_factory=list)


class GenericSQLBuilder:
    """Deterministic, type-safe SQL query builder using sqlglot AST expressions.

    Constructs complete SQL ASTs without string concatenation, resolving joins
    via SchemaGraph and applying hierarchical ontology deduplication.
    """

    def __init__(
        self,
        schema_graph: SchemaGraph,
        deduplicator: OntologyDeduplicator | None = None,
    ) -> None:
        """Initialize builder with a schema graph and optional deduplicator.

        Args:
            schema_graph: Semantic schema graph for join path resolution.
            deduplicator: Optional ontology deduplicator for filtering redundancy.
        """
        self._schema_graph = schema_graph
        self._deduplicator = deduplicator
        self._schema = schema_graph.schema

    def _normalize_intent(self, intent: QueryIntent | dict[str, Any]) -> QueryIntent:
        """Normalize intent input to a validated QueryIntent model instance.

        Args:
            intent: QueryIntent instance or dictionary representation.

        Returns:
            Validated QueryIntent model.

        Raises:
            SQLBuildError: If the input cannot be converted to QueryIntent.
        """
        if isinstance(intent, QueryIntent):
            return intent
        if isinstance(intent, dict):
            try:
                return QueryIntent(**intent)
            except Exception as e:
                raise SQLBuildError(f"Failed to validate QueryIntent dict: {e}") from e
        raise SQLBuildError(
            f"Invalid intent type: {type(intent).__name__}, expected QueryIntent or dict"
        )

    def _to_literal(self, val: Any) -> exp.Expr:
        """Convert a Python value into an appropriate sqlglot AST literal expression.

        Args:
            val: Python primitive or sqlglot expression.

        Returns:
            sqlglot AST literal expression.
        """
        if isinstance(val, exp.Expr):
            return val
        if val is None:
            return exp.Null()
        if isinstance(val, bool):
            return exp.Boolean(this=val)
        if isinstance(val, (int, float)):
            return exp.Literal.number(val)
        if isinstance(val, str):
            return exp.Literal.string(val)
        return exp.Literal.string(str(val))

    def _resolve_column(
        self, raw_col: str, default_table: str | None = None
    ) -> tuple[str | None, str]:
        """Resolve a raw column reference to its canonical (table_name, column_name) pair.

        Args:
            raw_col: Raw column string (e.g. 'orders.o_totalprice', 'o_totalprice', '*').
            default_table: Primary table name to search first if table prefix is omitted.

        Returns:
            Tuple of (canonical_table_name or None, canonical_column_name).

        Raises:
            SQLBuildError: If table or column does not exist or column is ambiguous.
        """
        raw_col_clean = raw_col.strip()
        if raw_col_clean == "*":
            return (None, "*")

        if "." in raw_col_clean:
            tbl_part, col_part = raw_col_clean.split(".", 1)
            tbl_part = tbl_part.strip()
            col_part = col_part.strip()

            tbl_meta = self._schema.get_table(tbl_part)
            if tbl_meta is None:
                raise SQLBuildError(
                    f"Table '{tbl_part}' in column reference '{raw_col}' does not exist in schema"
                )

            if col_part == "*":
                return (tbl_meta.name, "*")

            col_meta = tbl_meta.get_column(col_part)
            if col_meta is None:
                raise SQLBuildError(
                    f"Column '{col_part}' does not exist in table '{tbl_meta.name}'"
                )

            return (tbl_meta.name, col_meta.name)

        col_part = raw_col_clean
        # Search default_table first if specified
        if default_table:
            default_meta = self._schema.get_table(default_table)
            if default_meta is not None:
                col_meta = default_meta.get_column(col_part)
                if col_meta is not None:
                    return (default_meta.name, col_meta.name)

        # Search across all tables in schema
        matches: list[tuple[str, str]] = []
        for tbl in self._schema.tables:
            col_meta = tbl.get_column(col_part)
            if col_meta is not None:
                matches.append((tbl.name, col_meta.name))

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise SQLBuildError(
                f"Ambiguous column reference '{col_part}' matches multiple tables: "
                f"{[m[0] for m in matches]}"
            )

        raise SQLBuildError(f"Column '{col_part}' does not exist in any schema table")

    def _build_filter_condition(self, filter_slot: FilterSlot) -> exp.Expr:
        """Construct a sqlglot AST comparison expression for a single FilterSlot.

        Args:
            filter_slot: Filter specification.

        Returns:
            sqlglot AST boolean expression.

        Raises:
            SQLBuildError: If table, column, or operator is invalid.
        """
        tbl_meta = self._schema.get_table(filter_slot.table)
        if tbl_meta is None:
            raise SQLBuildError(f"Filter table '{filter_slot.table}' does not exist in schema")

        col_meta = tbl_meta.get_column(filter_slot.column)
        if col_meta is None:
            raise SQLBuildError(
                f"Filter column '{filter_slot.column}' does not exist in table '{tbl_meta.name}'"
            )

        col_expr = exp.column(col_meta.name, tbl_meta.name)
        op = filter_slot.operator.strip().upper()

        if op in ("=", "=="):
            return exp.EQ(this=col_expr, expression=self._to_literal(filter_slot.value))
        if op in ("!=", "<>"):
            return exp.NEQ(this=col_expr, expression=self._to_literal(filter_slot.value))
        if op == ">":
            return exp.GT(this=col_expr, expression=self._to_literal(filter_slot.value))
        if op == ">=":
            return exp.GTE(this=col_expr, expression=self._to_literal(filter_slot.value))
        if op == "<":
            return exp.LT(this=col_expr, expression=self._to_literal(filter_slot.value))
        if op == "<=":
            return exp.LTE(this=col_expr, expression=self._to_literal(filter_slot.value))
        if op == "LIKE":
            return exp.Like(this=col_expr, expression=self._to_literal(filter_slot.value))
        if op == "ILIKE":
            return exp.ILike(this=col_expr, expression=self._to_literal(filter_slot.value))
        if op == "IN":
            vals = (
                filter_slot.value
                if isinstance(filter_slot.value, (list, tuple, set))
                else [filter_slot.value]
            )
            return exp.In(this=col_expr, expressions=[self._to_literal(v) for v in vals])
        if op == "NOT IN":
            vals = (
                filter_slot.value
                if isinstance(filter_slot.value, (list, tuple, set))
                else [filter_slot.value]
            )
            return exp.Not(
                this=exp.In(this=col_expr, expressions=[self._to_literal(v) for v in vals])
            )
        if op in ("IS", "IS NULL"):
            return exp.Is(this=col_expr, expression=exp.Null())
        if op in ("IS NOT", "IS NOT NULL"):
            return exp.Not(this=exp.Is(this=col_expr, expression=exp.Null()))
        if op == "BETWEEN":
            if not isinstance(filter_slot.value, (list, tuple)) or len(filter_slot.value) != 2:
                raise SQLBuildError(f"BETWEEN operator requires 2 values, got: {filter_slot.value}")
            return exp.Between(
                this=col_expr,
                low=self._to_literal(filter_slot.value[0]),
                high=self._to_literal(filter_slot.value[1]),
            )

        raise SQLBuildError(f"Unsupported filter operator: '{filter_slot.operator}'")

    def _extract_tables_and_resolve_columns(
        self,
        parsed_node: exp.Expr,
        primary_table: str,
        add_ref_table: Callable[[str | None], None],
        context_name: str = "expression",
    ) -> None:
        """Extract all referenced tables from a parsed AST expression and resolve column references.

        Traverses the AST for column references, resolves table qualification
        using schema metadata, and registers referenced tables for JOIN resolution.

        Args:
            parsed_node: Parsed sqlglot expression AST.
            primary_table: Primary table name for default column resolution.
            add_ref_table: Callback to register referenced tables for JOIN resolution.
            context_name: Expression context name for clear error reporting
                (e.g. 'metric', 'dimension').

        Raises:
            SQLBuildError: If a referenced table or column cannot be resolved.
        """
        for col_node in parsed_node.find_all(exp.Column):
            if isinstance(col_node.this, exp.Star) or col_node.name == "*":
                if col_node.table:
                    tbl_meta = self._schema.get_table(col_node.table)
                    if tbl_meta is None:
                        raise SQLBuildError(
                            f"Table '{col_node.table}' in {context_name} expression "
                            "does not exist in schema"
                        )
                    add_ref_table(tbl_meta.name)
                    col_node.set("table", exp.to_identifier(tbl_meta.name))
                continue

            if col_node.table:
                tbl_meta = self._schema.get_table(col_node.table)
                if tbl_meta is None:
                    raise SQLBuildError(
                        f"Table '{col_node.table}' in {context_name} expression "
                        "does not exist in schema"
                    )
                col_meta = tbl_meta.get_column(col_node.name)
                if col_meta is None:
                    raise SQLBuildError(
                        f"Column '{col_node.name}' does not exist in table '{tbl_meta.name}'"
                    )
                add_ref_table(tbl_meta.name)
                col_node.set("table", exp.to_identifier(tbl_meta.name))
                col_node.set("this", exp.to_identifier(col_meta.name))
            else:
                tbl_name, col_name = self._resolve_column(
                    col_node.name, default_table=primary_table
                )
                if tbl_name:
                    add_ref_table(tbl_name)
                    col_node.set("table", exp.to_identifier(tbl_name))
                col_node.set("this", exp.to_identifier(col_name))

    def _extract_metric_tables_and_resolve_columns(
        self,
        parsed_node: exp.Expr,
        primary_table: str,
        add_ref_table: Callable[[str | None], None],
    ) -> None:
        """Extract all referenced tables from a parsed metric AST (backward-compatible wrapper)."""
        self._extract_tables_and_resolve_columns(
            parsed_node, primary_table, add_ref_table, context_name="metric"
        )

    def _build_having_condition(
        self,
        having_slot: HavingSlot,
        primary_table: str,
        add_ref_table: Callable[[str | None], None],
    ) -> exp.Expr:
        """Construct a sqlglot AST comparison expression for a single HavingSlot.

        Resolves semantic metrics to their parsed calculation AST, extracting
        referenced tables for JOIN path resolution and ensuring ANSI SQL portability
        (avoiding SELECT alias references in HAVING for dialects like Postgres).
        Falls back to physical column resolution or column alias reference.

        Args:
            having_slot: Having specification slot.
            primary_table: Primary table name for default column resolution.
            add_ref_table: Callback to register referenced tables for JOIN resolution.

        Returns:
            sqlglot AST comparison expression node.

        Raises:
            SQLBuildError: If metric expression is invalid or operator is unsupported.
        """
        metric_name = having_slot.metric.strip()
        left_node: exp.Expr

        # 1. Resolve left expression (semantic metric, physical column, or alias)
        metric_def = (
            self._schema.get_metric(metric_name)
            if hasattr(self._schema, "get_metric")
            else None
        )

        if metric_def is not None:
            expr_str = metric_def.expression.strip()
            try:
                parsed_node = sqlglot.parse_one(expr_str)
            except Exception as e:
                raise SQLBuildError(
                    f"Failed to parse semantic metric expression '{expr_str}' "
                    f"for having metric '{metric_def.name}': {e}"
                ) from e
            if parsed_node is None:
                raise SQLBuildError(
                    f"Failed to parse semantic metric expression '{expr_str}' "
                    f"for having metric '{metric_def.name}': expression is empty or invalid"
                )

            self._extract_metric_tables_and_resolve_columns(
                parsed_node, primary_table, add_ref_table
            )
            left_node = parsed_node
        else:
            try:
                tbl_name, col_name = self._resolve_column(
                    metric_name, default_table=primary_table
                )
                add_ref_table(tbl_name)
                left_node = (
                    exp.column(col_name, tbl_name)
                    if tbl_name
                    else exp.column(col_name)
                )
            except SQLBuildError:
                left_node = exp.column(metric_name)

        # 2. Derive right literal
        right_node = self._to_literal(having_slot.value)

        # 3. Construct comparison node
        op = having_slot.operator.strip().upper()
        if op == ">=":
            return exp.GTE(this=left_node, expression=right_node)
        if op == ">":
            return exp.GT(this=left_node, expression=right_node)
        if op == "<=":
            return exp.LTE(this=left_node, expression=right_node)
        if op == "<":
            return exp.LT(this=left_node, expression=right_node)
        if op in ("=", "=="):
            return exp.EQ(this=left_node, expression=right_node)
        if op in ("!=", "<>"):
            return exp.NEQ(this=left_node, expression=right_node)

        raise SQLBuildError(f"Unsupported HAVING operator: '{having_slot.operator}'")

    def _prepare_components(self, norm_intent: QueryIntent) -> _QueryComponents:
        """Prepare query AST components, resolving tables, columns, and expressions.

        Args:
            norm_intent: Validated QueryIntent.

        Returns:
            _QueryComponents instance containing parsed expressions and metadata.

        Raises:
            SQLBuildError: If schema resolution or AST parsing fails.
        """
        # 1. Resolve and validate primary table
        primary_meta = self._schema.get_table(norm_intent.primary_table)
        if primary_meta is None:
            raise SQLBuildError(
                f"Primary table '{norm_intent.primary_table}' does not exist in schema"
            )
        primary_table = primary_meta.name

        # 2. Ontology deduplication
        active_filters = norm_intent.filters
        if self._deduplicator is not None and active_filters:
            active_filters = self._deduplicator.deduplicate(active_filters)

        # 3. Identify all referenced tables and validate columns
        referenced_tables: list[str] = []

        def add_ref_table(tbl: str | None) -> None:
            if tbl is not None and tbl != primary_table and tbl not in referenced_tables:
                referenced_tables.append(tbl)

        # Dimensions
        select_nodes: list[exp.Expr] = []
        group_nodes: list[exp.Expr] = []
        alias_to_expr: dict[str, exp.Expr] = {}
        dim_to_alias: dict[str, str] = {}
        dim_to_expr: dict[str, exp.Expr] = {}
        dim_output_names: list[str] = []
        dim_output_nodes: list[tuple[exp.Expr, str]] = []
        metric_col_to_expr: dict[str, exp.Expr] = {}
        metric_col_to_alias: dict[str, str] = {}
        metric_output_names: list[str] = []
        metric_output_nodes: list[tuple[exp.Expr, str]] = []
        metric_agg_nodes: list[exp.Expr] = []

        for dim in norm_intent.dimensions:
            # Check for alias syntax e.g. "col AS alias"
            dim_str = dim.strip()
            dim_alias: str | None = None
            if " as " in dim_str.lower():
                parts = dim_str.lower().split(" as ", 1)
                dim_str = dim_str[: len(parts[0])].strip()
                dim_alias = dim[len(parts[0]) + 4 :].strip()

            dim_def = (
                self._schema.get_dimension(dim_str)
                if hasattr(self._schema, "get_dimension")
                else None
            )

            # Case 1: Semantic dimension with calculation expression
            if dim_def is not None and dim_def.expression:
                expr_str = dim_def.expression.strip()
                try:
                    parsed_node = sqlglot.parse_one(expr_str)
                except Exception as e:
                    raise SQLBuildError(
                        f"Failed to parse semantic dimension expression '{expr_str}' "
                        f"for dimension '{dim_def.name}': {e}"
                    ) from e
                if parsed_node is None:
                    raise SQLBuildError(
                        f"Failed to parse semantic dimension expression '{expr_str}' "
                        f"for dimension '{dim_def.name}': expression is empty or invalid"
                    )

                self._extract_tables_and_resolve_columns(
                    parsed_node, primary_table, add_ref_table, context_name="dimension"
                )

                final_alias = dim_alias if dim_alias else dim_def.name
                aliased_node = exp.alias_(parsed_node, final_alias)
                select_nodes.append(aliased_node)
                group_nodes.append(parsed_node.copy())

                # Register alias mapping and expressions for ORDER BY lookup
                alias_to_expr[final_alias] = aliased_node
                alias_to_expr[dim_def.name] = aliased_node
                alias_to_expr[dim_str] = aliased_node
                for syn in dim_def.synonyms:
                    alias_to_expr[syn] = aliased_node
                dim_to_alias[final_alias] = final_alias
                dim_to_alias[dim_def.name] = final_alias
                dim_to_alias[dim_str] = final_alias
                dim_to_expr[final_alias] = parsed_node
                dim_to_expr[dim_def.name] = parsed_node
                dim_to_expr[dim_str] = parsed_node

                dim_output_names.append(final_alias)
                dim_output_nodes.append((parsed_node.copy(), final_alias))

            # Case 2: Physical column or dimension without expression
            else:
                target_col = dim_def.name if dim_def is not None else dim_str
                tbl_name, col_name = self._resolve_column(target_col, default_table=primary_table)
                add_ref_table(tbl_name)

                col_node = exp.column(col_name, tbl_name) if tbl_name else exp.column(col_name)
                group_nodes.append(col_node)

                out_alias = dim_alias if dim_alias else col_name
                if dim_alias:
                    aliased_node = exp.alias_(col_node, dim_alias)
                    select_nodes.append(aliased_node)
                    alias_to_expr[dim_alias] = aliased_node
                    dim_to_alias[dim_alias] = dim_alias
                    dim_to_alias[target_col] = dim_alias
                else:
                    select_nodes.append(col_node)
                    if dim_def is not None:
                        dim_to_alias[dim_def.name] = col_name

                dim_to_expr[target_col] = col_node
                if tbl_name:
                    dim_to_expr[f"{tbl_name}.{col_name}"] = col_node
                    dim_to_alias[f"{tbl_name}.{col_name}"] = dim_alias or col_name

                dim_output_names.append(out_alias)
                dim_output_nodes.append((col_node.copy(), out_alias))

        # Metrics
        for idx, metric in enumerate(norm_intent.metrics):
            if isinstance(metric, MetricSlot):
                m_col = metric.column
                m_agg = metric.agg
                m_alias = metric.alias
                m_expr = metric.expression
            else:
                m_col = metric[0]
                m_agg = metric[1]
                m_alias = metric[2] if len(metric) > 2 else None
                m_expr = None

            m_col_clean = m_col.strip() if m_col else ""
            m_expr_clean = m_expr.strip() if m_expr else None

            agg_node: exp.Expr

            # Priority 1: Explicit expression in MetricSlot
            if m_expr_clean:
                try:
                    parsed_node = sqlglot.parse_one(m_expr_clean)
                except Exception as e:
                    raise SQLBuildError(
                        f"Failed to parse metric expression '{m_expr_clean}': {e}"
                    ) from e
                if parsed_node is None:
                    raise SQLBuildError(
                        f"Failed to parse metric expression '{m_expr_clean}': "
                        "expression is empty or invalid"
                    )

                self._extract_metric_tables_and_resolve_columns(
                    parsed_node, primary_table, add_ref_table
                )
                agg_node = parsed_node

                final_alias = m_alias or f"metric_{idx + 1}"
                if m_alias:
                    metric_col_to_alias[m_expr_clean] = m_alias
                    if m_col_clean:
                        metric_col_to_alias[m_col_clean] = m_alias
                metric_col_to_expr[m_expr_clean] = agg_node
                if m_col_clean:
                    metric_col_to_expr[m_col_clean] = agg_node

                metric_agg_nodes.append(agg_node.copy())
                metric_output_names.append(final_alias)
                metric_output_nodes.append((agg_node.copy(), final_alias))

            # Priority 2: Semantic metric defined in schema
            elif (
                m_col_clean
                and hasattr(self._schema, "get_metric")
                and (metric_def := self._schema.get_metric(m_col_clean)) is not None
            ):
                try:
                    parsed_node = sqlglot.parse_one(metric_def.expression)
                except Exception as e:
                    raise SQLBuildError(
                        f"Failed to parse semantic metric expression '{metric_def.expression}' "
                        f"for metric '{metric_def.name}': {e}"
                    ) from e
                if parsed_node is None:
                    raise SQLBuildError(
                        f"Failed to parse semantic metric expression '{metric_def.expression}' "
                        f"for metric '{metric_def.name}': expression is empty or invalid"
                    )

                self._extract_metric_tables_and_resolve_columns(
                    parsed_node, primary_table, add_ref_table
                )
                agg_node = parsed_node

                if m_alias is None:
                    m_alias = metric_def.name

                metric_col_to_alias[metric_def.name] = m_alias
                metric_col_to_alias[m_col_clean] = m_alias
                metric_col_to_expr[metric_def.name] = agg_node
                metric_col_to_expr[m_col_clean] = agg_node
                for syn in metric_def.synonyms:
                    metric_col_to_alias[syn] = m_alias
                    metric_col_to_expr[syn] = agg_node

                metric_agg_nodes.append(agg_node.copy())
                metric_output_names.append(m_alias)
                metric_output_nodes.append((agg_node.copy(), m_alias))

            # Priority 3: Fallback to single-column aggregate function
            else:
                if not m_col_clean:
                    raise SQLBuildError(
                        "Metric must specify either 'expression' or a valid 'column'"
                    )

                inner_expr: exp.Expression
                if m_col_clean == "*":
                    inner_expr = exp.Star()
                else:
                    tbl_name, col_name = self._resolve_column(
                        m_col_clean, default_table=primary_table
                    )
                    add_ref_table(tbl_name)
                    inner_expr = (
                        exp.column(col_name, tbl_name) if tbl_name else exp.column(col_name)
                    )

                agg_node = exp.func(m_agg.strip().upper(), inner_expr)
                metric_col_to_expr[m_col_clean] = agg_node
                if m_alias:
                    metric_col_to_alias[m_col_clean] = m_alias

                fallback_suffix = col_name if m_col_clean != "*" else "count"
                fallback_alias = m_alias or f"{m_agg.lower()}_{fallback_suffix}"
                metric_agg_nodes.append(agg_node.copy())
                metric_output_names.append(fallback_alias)
                metric_output_nodes.append((agg_node.copy(), fallback_alias))

            if m_alias:
                aliased_node = exp.alias_(agg_node, m_alias)
                select_nodes.append(aliased_node)
                alias_to_expr[m_alias] = aliased_node
                metric_col_to_alias[m_alias] = m_alias
            else:
                select_nodes.append(agg_node)

        # Filters referenced tables
        where_conditions: list[exp.Expr] = []
        for flt in active_filters:
            cond = self._build_filter_condition(flt)
            where_conditions.append(cond)
            # Find filter canonical table
            flt_meta = self._schema.get_table(flt.table)
            if flt_meta:
                add_ref_table(flt_meta.name)

        # Having referenced tables and conditions
        having_conditions: list[exp.Expr] = []
        for h_slot in norm_intent.having:
            h_cond = self._build_having_condition(h_slot, primary_table, add_ref_table)
            having_conditions.append(h_cond)

        # Default select if empty
        if not select_nodes:
            select_nodes = [exp.Star()]

        # 4. Resolve JOIN path via SchemaGraph
        try:
            join_steps = self._schema_graph.resolve_joins(primary_table, referenced_tables)
        except UnconnectedSchemaError as e:
            raise SQLBuildError(f"Failed to resolve JOIN path: {e}") from e

        return _QueryComponents(
            primary_table=primary_table,
            select_nodes=select_nodes,
            group_nodes=group_nodes,
            where_conditions=where_conditions,
            having_conditions=having_conditions,
            join_steps=join_steps,
            alias_to_expr=alias_to_expr,
            dim_to_alias=dim_to_alias,
            dim_to_expr=dim_to_expr,
            dim_output_names=dim_output_names,
            dim_output_nodes=dim_output_nodes,
            metric_col_to_alias=metric_col_to_alias,
            metric_col_to_expr=metric_col_to_expr,
            metric_output_names=metric_output_names,
            metric_output_nodes=metric_output_nodes,
            metric_agg_nodes=metric_agg_nodes,
        )

    def _assemble_base_query(
        self,
        components: _QueryComponents,
        select_nodes: list[exp.Expr],
    ) -> exp.Select:
        """Assemble base SELECT query AST with joins, filters, group by, and having.

        Args:
            components: Query components.
            select_nodes: SELECT expressions for the base query.

        Returns:
            sqlglot Select AST node.

        Raises:
            SQLBuildError: If an ON clause cannot be parsed.
        """
        query = sqlglot.select(*select_nodes).from_(exp.to_table(components.primary_table))

        # Join clauses
        for step in components.join_steps:
            target_table_node = exp.to_table(step.to_table)
            on_clause: exp.Expr
            if step.join_pairs:
                step_conds: list[exp.Expr] = [
                    exp.EQ(
                        this=exp.column(fc, step.from_table),
                        expression=exp.column(tc, step.to_table),
                    )
                    for fc, tc in step.join_pairs
                ]
                on_clause = exp.and_(*step_conds) if len(step_conds) > 1 else step_conds[0]
            else:
                try:
                    on_clause = sqlglot.parse_one(step.on_clause)
                except Exception as e:
                    raise SQLBuildError(f"Failed to parse ON clause '{step.on_clause}': {e}") from e

            query = query.join(target_table_node, on=on_clause)

        # Where clause
        if components.where_conditions:
            where_node = (
                exp.and_(*components.where_conditions)
                if len(components.where_conditions) > 1
                else components.where_conditions[0]
            )
            query = query.where(where_node)

        # Group By clause (if metrics are present or having conditions exist, and dimensions exist)
        has_metrics_or_having = bool(components.metric_output_nodes or components.having_conditions)
        if has_metrics_or_having and components.group_nodes:
            query = query.group_by(*components.group_nodes)

        # Having clause
        if components.having_conditions:
            having_node = (
                exp.and_(*components.having_conditions)
                if len(components.having_conditions) > 1
                else components.having_conditions[0]
            )
            query = query.having(having_node)

        return query

    def _build_single_query(
        self,
        norm_intent: QueryIntent,
        components: _QueryComponents,
        dialect: str = "postgres",
    ) -> str:
        """Assemble single-pass SQL query string.

        Args:
            norm_intent: QueryIntent instance.
            components: Prepared AST query components.
            dialect: Target SQL dialect.

        Returns:
            Formatted SQL query string.

        Raises:
            SQLBuildError: If AST building or transpilation fails.
        """
        query = self._assemble_base_query(components, components.select_nodes)

        # Order By clause
        if norm_intent.order_by:
            order_nodes: list[exp.Expr] = []
            for order_item in norm_intent.order_by:
                if isinstance(order_item, OrderBySlot):
                    ord_col = order_item.column
                    ord_dir = order_item.direction
                else:
                    ord_col = order_item[0]
                    ord_dir = order_item[1]

                ord_col_clean = ord_col.strip()
                desc = ord_dir.strip().upper() == "DESC"

                order_target: exp.Expr
                # 1. Alias match
                if ord_col_clean in components.alias_to_expr:
                    aliased = components.alias_to_expr[ord_col_clean]
                    target_name = (
                        aliased.alias if isinstance(aliased, exp.Alias) else ord_col_clean
                    )
                    order_target = exp.column(target_name)
                # 2. Metric alias lookup match
                elif ord_col_clean in components.metric_col_to_alias:
                    order_target = exp.column(components.metric_col_to_alias[ord_col_clean])
                # 3. Metric column match without alias (e.g. order by aggregate expr)
                elif ord_col_clean in components.metric_col_to_expr:
                    order_target = components.metric_col_to_expr[ord_col_clean]
                # 4. Dimension alias lookup match
                elif ord_col_clean in components.dim_to_alias:
                    order_target = exp.column(components.dim_to_alias[ord_col_clean])
                # 5. Dimension expression match
                elif ord_col_clean in components.dim_to_expr:
                    order_target = components.dim_to_expr[ord_col_clean]
                else:
                    # 6. Resolve column
                    tbl, col = self._resolve_column(
                        ord_col_clean, default_table=components.primary_table
                    )
                    order_target = exp.column(col, tbl) if tbl else exp.column(col)

                order_nodes.append(exp.Ordered(this=order_target, desc=desc))

            query = query.order_by(*order_nodes)

        # Limit clause
        if norm_intent.limit is not None:
            if norm_intent.limit < 0:
                raise SQLBuildError(f"LIMIT cannot be negative: {norm_intent.limit}")
            query = query.limit(norm_intent.limit)

        # Dialect transpilation
        try:
            return query.sql(dialect=dialect, pretty=True)
        except Exception as e:
            raise SQLBuildError(f"Failed to transpile SQL query to dialect '{dialect}': {e}") from e

    def _build_partitioned_rank_query(
        self,
        intent: QueryIntent,
        components: _QueryComponents,
        base_select_nodes: list[exp.Expr],
        dialect: str = "postgres",
    ) -> str:
        """Construct two-pass CTE AST for partitioned ranking (e.g. intra-group Top-N).

        Args:
            intent: QueryIntent with window slot.
            components: Query components.
            base_select_nodes: Base SELECT nodes before rank_num window expression.
            dialect: Target SQL dialect.

        Returns:
            Transpiled SQL string.

        Raises:
            SQLBuildError: If window parameters or transpilation fails.
        """
        window = intent.window
        assert window is not None

        # 1. Resolve PARTITION BY expressions
        partition_nodes: list[exp.Expr] = []
        partition_out_cols: list[str] = []
        for p in window.partition_by:
            p_clean = p.strip()
            if p_clean in components.dim_to_expr:
                partition_nodes.append(components.dim_to_expr[p_clean].copy())
            else:
                tbl, col = self._resolve_column(p_clean, default_table=components.primary_table)
                partition_nodes.append(exp.column(col, tbl) if tbl else exp.column(col))

            if p_clean in components.dim_to_alias:
                partition_out_cols.append(components.dim_to_alias[p_clean])
            else:
                try:
                    _, col = self._resolve_column(p_clean, default_table=components.primary_table)
                    partition_out_cols.append(col)
                except SQLBuildError:
                    partition_out_cols.append(p_clean)

        # 2. Resolve ORDER BY expressions inside Window
        order_nodes: list[exp.Expr] = []
        if window.order_by:
            for ord_item in window.order_by:
                col_clean = ord_item.column.strip()
                desc = ord_item.direction.strip().upper() == "DESC"
                target_expr: exp.Expr
                if col_clean in components.metric_col_to_expr:
                    target_expr = components.metric_col_to_expr[col_clean].copy()
                elif col_clean in components.dim_to_expr:
                    target_expr = components.dim_to_expr[col_clean].copy()
                else:
                    tbl, col = self._resolve_column(
                        col_clean, default_table=components.primary_table
                    )
                    target_expr = exp.column(col, tbl) if tbl else exp.column(col)
                order_nodes.append(exp.Ordered(this=target_expr, desc=desc))
        else:
            # Default order by: target metric DESC
            target_metric_expr: exp.Expr | None = None
            tm_key = window.target_metric.strip() if window.target_metric else None
            if tm_key and tm_key in components.metric_col_to_expr:
                target_metric_expr = components.metric_col_to_expr[tm_key]
            elif components.metric_agg_nodes:
                target_metric_expr = components.metric_agg_nodes[0]

            if target_metric_expr is None:
                raise SQLBuildError(
                    "Partitioned ranking requires at least one metric or explicit window order_by"
                )
            order_nodes.append(exp.Ordered(this=target_metric_expr.copy(), desc=True))

        # 3. Assemble Window function expression and add to base SELECT
        func_name = window.function.strip().upper() if window.function else "ROW_NUMBER"
        window_expr = exp.Window(
            this=exp.Anonymous(this=func_name),
            partition_by=partition_nodes if partition_nodes else None,
            order=exp.Order(expressions=order_nodes) if order_nodes else None,
        )
        base_select_nodes.append(exp.alias_(window_expr, "rank_num"))

        # 4. Construct Pass 1 Base CTE Query
        base_query = self._assemble_base_query(components, base_select_nodes)

        # 5. Construct Pass 2 Outer Query
        cte_table_name = "ranked_base"
        outer_selects: list[exp.Expr] = []
        for dim_name in components.dim_output_names:
            outer_selects.append(exp.column(dim_name))
        for m_name in components.metric_output_names:
            outer_selects.append(exp.column(m_name))

        outer_query = (
            exp.select(*outer_selects)
            .from_(exp.to_table(cte_table_name))
        )

        if window.limit is not None:
            outer_query = outer_query.where(
                exp.LTE(this=exp.column("rank_num"), expression=exp.Literal.number(window.limit))
            )

        # Outer ORDER BY: partition columns + rank_num
        outer_order: list[exp.Expr] = []
        for p_col in partition_out_cols:
            outer_order.append(exp.Ordered(this=exp.column(p_col), desc=False))
        outer_order.append(exp.Ordered(this=exp.column("rank_num"), desc=False))
        outer_query = outer_query.order_by(*outer_order)

        # Attach CTE
        outer_query = outer_query.with_(cte_table_name, as_=base_query)

        try:
            return outer_query.sql(dialect=dialect, pretty=True)
        except Exception as e:
            raise SQLBuildError(f"Failed to transpile SQL query to dialect '{dialect}': {e}") from e

    def _build_time_shift_query(
        self,
        intent: QueryIntent,
        components: _QueryComponents,
        base_select_nodes: list[exp.Expr],
        dialect: str = "postgres",
    ) -> str:
        """Construct two-pass CTE AST for time-series shift (e.g. YoY / MoM growth).

        Args:
            intent: QueryIntent with window slot.
            components: Query components.
            base_select_nodes: Base SELECT nodes.
            dialect: Target SQL dialect.

        Returns:
            Transpiled SQL string.

        Raises:
            SQLBuildError: If parameters or transpilation fails.
        """
        window = intent.window
        assert window is not None

        if not components.dim_output_names:
            raise SQLBuildError("Time shift window operation requires at least one dimension")
        if not components.metric_output_names:
            raise SQLBuildError("Time shift window operation requires at least one metric")

        # 1. Identify time dimension and metric aliases
        time_dim_alias = components.dim_output_names[0]
        for name in components.dim_output_names:
            lower = name.lower()
            if any(k in lower for k in ("year", "month", "date", "day", "time")):
                time_dim_alias = name
                break

        metric_alias = components.metric_output_names[0]
        if window.target_metric:
            tm_clean = window.target_metric.strip()
            if tm_clean in components.metric_col_to_alias:
                metric_alias = components.metric_col_to_alias[tm_clean]
            elif tm_clean in components.metric_output_names:
                metric_alias = tm_clean

        # 2. Construct Pass 1 Base CTE Query
        base_query = self._assemble_base_query(components, base_select_nodes)

        # 3. Construct Window LAG and YoY expressions on outer query
        offset = window.offset
        lag_expr = exp.Window(
            this=exp.Anonymous(
                this="LAG",
                expressions=[exp.column(metric_alias), exp.Literal.number(offset)],
            ),
            order=exp.Order(expressions=[exp.column(time_dim_alias)]),
        )

        diff_expr = exp.Paren(
            this=exp.Sub(this=exp.column(metric_alias), expression=lag_expr.copy())
        )
        pct_expr = exp.Mul(this=diff_expr, expression=exp.Literal.number(100.0))
        growth_expr = exp.Div(this=pct_expr, expression=lag_expr.copy())
        rounded_growth = exp.Anonymous(
            this="ROUND", expressions=[growth_expr, exp.Literal.number(2)]
        )

        cte_table_name = "time_series_base"
        outer_selects: list[exp.Expr] = []
        for d in components.dim_output_names:
            outer_selects.append(exp.column(d))
        for m in components.metric_output_names:
            outer_selects.append(exp.column(m))
        outer_selects.append(exp.alias_(lag_expr, f"prev_{metric_alias}"))
        outer_selects.append(exp.alias_(rounded_growth, f"{metric_alias}_yoy_growth_pct"))

        outer_query = (
            exp.select(*outer_selects)
            .from_(exp.to_table(cte_table_name))
            .order_by(exp.column(time_dim_alias))
            .with_(cte_table_name, as_=base_query)
        )

        try:
            return outer_query.sql(dialect=dialect, pretty=True)
        except Exception as e:
            raise SQLBuildError(f"Failed to transpile SQL query to dialect '{dialect}': {e}") from e

    def _build_window_cte_query(
        self,
        intent: QueryIntent,
        components: _QueryComponents,
        dialect: str = "postgres",
    ) -> str:
        """Build a two-pass CTE query with window functions.

        Args:
            intent: Validated QueryIntent containing window specifications.
            components: Prepared AST query components.
            dialect: Target SQL dialect.

        Returns:
            Formatted SQL query string.

        Raises:
            SQLBuildError: If window configuration is invalid or compilation fails.
        """
        assert intent.window is not None
        window = intent.window

        # Validate window operation kind
        if window.kind not in ("partitioned_rank", "time_shift"):
            raise SQLBuildError(f"Unsupported window kind: '{window.kind}'")
        if window.offset <= 0:
            raise SQLBuildError(f"Window offset must be positive, got: {window.offset}")
        if window.limit is not None and window.limit < 0:
            raise SQLBuildError(f"Window limit cannot be negative, got: {window.limit}")

        # Assemble base query SELECT nodes with explicit aliases
        base_select_nodes: list[exp.Expr] = [
            exp.alias_(node.copy(), alias) for node, alias in components.dim_output_nodes
        ] + [
            exp.alias_(node.copy(), alias) for node, alias in components.metric_output_nodes
        ]

        if window.kind == "partitioned_rank":
            return self._build_partitioned_rank_query(
                intent, components, base_select_nodes, dialect=dialect
            )
        elif window.kind == "time_shift":
            return self._build_time_shift_query(
                intent, components, base_select_nodes, dialect=dialect
            )
        else:
            raise SQLBuildError(f"Unsupported window kind: '{window.kind}'")

    def build(
        self,
        intent: QueryIntent | dict[str, Any],
        dialect: str = "postgres",
    ) -> str:
        """Build a deterministic SQL query string from intent using sqlglot AST expressions.

        Args:
            intent: QueryIntent instance or dictionary conforming to QueryIntent.
            dialect: Target SQL dialect (e.g. 'postgres', 'duckdb', 'sqlite', 'snowflake').

        Returns:
            Formatted SQL query string.

        Raises:
            SQLBuildError: If schema resolution, AST building, or dialect transpilation fails.
        """
        norm_intent = self._normalize_intent(intent)
        components = self._prepare_components(norm_intent)

        if norm_intent.window is not None:
            return self._build_window_cte_query(norm_intent, components, dialect=dialect)

        return self._build_single_query(norm_intent, components, dialect=dialect)
