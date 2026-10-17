"""Schema graph representation and shortest-path join resolution using NetworkX."""

from typing import Any

import networkx as nx

from deterministic_text_to_sql.graph.exceptions import UnconnectedSchemaError
from deterministic_text_to_sql.graph.models import JoinStep
from deterministic_text_to_sql.schema.loader import load_schema_from_dict
from deterministic_text_to_sql.schema.models import ForeignKeyMetadata, SchemaMetadata


class SchemaGraph:
    """NetworkX-based semantic schema graph for deterministic join resolution.

    Models database tables as nodes and foreign key relationships as undirected edges,
    enabling millisecond-order shortest-path join resolution via Dijkstra's algorithm.
    """

    def __init__(self, schema: SchemaMetadata | dict[str, Any]) -> None:
        """Initialize the schema graph from a SchemaMetadata instance or a schema dict.

        Args:
            schema: Validated SchemaMetadata or raw dictionary conforming to schema definition.
        """
        if isinstance(schema, SchemaMetadata):
            self._schema = schema
        else:
            self._schema = load_schema_from_dict(schema)

        self._graph: nx.Graph = nx.Graph()
        self._build_graph()

    @property
    def schema(self) -> SchemaMetadata:
        """Return the underlying SchemaMetadata."""
        return self._schema

    @property
    def graph(self) -> nx.Graph:
        """Return the internal NetworkX graph."""
        return self._graph

    def _normalize_table_name(self, table_name: str) -> str:
        """Resolve a table name or alias to the canonical physical table name.

        Args:
            table_name: Physical table name or alias.

        Returns:
            The canonical table name defined in the schema.

        Raises:
            UnconnectedSchemaError: If the table cannot be resolved.
        """
        table_meta = self._schema.get_table(table_name)
        if table_meta is None:
            raise UnconnectedSchemaError(f"Table '{table_name}' does not exist in schema")
        return table_meta.name

    def _build_graph(self) -> None:
        """Construct the NetworkX graph from schema tables and foreign keys."""
        # 1. Add all tables as nodes
        for table in self._schema.tables:
            self._graph.add_node(table.name)

        # 2. Add foreign key relationships as undirected edges with join conditions
        for table in self._schema.tables:
            for fk in table.foreign_keys:
                self._add_foreign_key_edge(table.name, fk)

    def _add_foreign_key_edge(self, from_table: str, fk: ForeignKeyMetadata) -> None:
        """Add an undirected edge between from_table and referenced table with join pairs."""
        ref_table = self._normalize_table_name(fk.ref_table)

        # Normalize column and ref_column to lists
        cols = [fk.column] if isinstance(fk.column, str) else list(fk.column)
        ref_cols = [fk.ref_column] if isinstance(fk.ref_column, str) else list(fk.ref_column)

        pairs_forward: list[tuple[str, str]] = list(zip(cols, ref_cols, strict=True))
        pairs_reverse: list[tuple[str, str]] = [(r, c) for c, r in pairs_forward]

        if self._graph.has_edge(from_table, ref_table):
            # Edge already exists; merge relation directions if not already present
            edge_data = self._graph[from_table][ref_table]
            relations: dict[tuple[str, str], list[tuple[str, str]]] = edge_data.setdefault(
                "relations", {}
            )
            if (from_table, ref_table) not in relations:
                relations[(from_table, ref_table)] = pairs_forward
            if (ref_table, from_table) not in relations:
                relations[(ref_table, from_table)] = pairs_reverse
        else:
            relations = {
                (from_table, ref_table): pairs_forward,
                (ref_table, from_table): pairs_reverse,
            }
            self._graph.add_edge(from_table, ref_table, weight=1.0, relations=relations)

    def _build_join_step(self, from_table: str, to_table: str) -> JoinStep:
        """Create a JoinStep between two directly adjacent tables in the graph.

        Args:
            from_table: Canonical source table name.
            to_table: Canonical target table name.

        Returns:
            JoinStep containing join column pairs and the SQL ON clause.

        Raises:
            UnconnectedSchemaError: If no edge or relation exists between the two tables.
        """
        if not self._graph.has_edge(from_table, to_table):
            raise UnconnectedSchemaError(
                f"No direct edge exists between '{from_table}' and '{to_table}'"
            )

        edge_data = self._graph[from_table][to_table]
        relations: dict[tuple[str, str], list[tuple[str, str]]] = edge_data.get("relations", {})

        pairs = relations.get((from_table, to_table))
        if not pairs:
            rev_pairs = relations.get((to_table, from_table))
            if rev_pairs:
                pairs = [(to_c, from_c) for from_c, to_c in rev_pairs]
            else:
                raise UnconnectedSchemaError(
                    f"No join condition found for edge between '{from_table}' and '{to_table}'"
                )

        on_conditions = [f"{from_table}.{fc} = {to_table}.{tc}" for fc, tc in pairs]
        on_clause = " AND ".join(on_conditions)

        return JoinStep(
            from_table=from_table,
            to_table=to_table,
            join_pairs=pairs,
            on_clause=on_clause,
        )

    def find_shortest_path(self, source_table: str, target_table: str) -> list[JoinStep]:
        """Find the shortest sequence of JoinSteps connecting source_table and target_table.

        Uses Dijkstra's algorithm (via nx.shortest_path) over the foreign key graph.

        Args:
            source_table: Start table name or alias.
            target_table: Destination table name or alias.

        Returns:
            List of JoinStep instances representing the shortest path.
            Returns an empty list if source_table and target_table are identical.

        Raises:
            UnconnectedSchemaError: If either table does not exist or no path connects them.
        """
        src = self._normalize_table_name(source_table)
        dst = self._normalize_table_name(target_table)

        if src == dst:
            return []

        if not self._graph.has_node(src) or not self._graph.has_node(dst):
            raise UnconnectedSchemaError(f"Table '{src}' or '{dst}' is not present in graph")

        if not nx.has_path(self._graph, src, dst):
            raise UnconnectedSchemaError(f"No join path exists between '{src}' and '{dst}'")

        node_path: list[str] = nx.shortest_path(
            self._graph, source=src, target=dst, weight="weight"
        )

        steps: list[JoinStep] = []
        for i in range(len(node_path) - 1):
            u = node_path[i]
            v = node_path[i + 1]
            steps.append(self._build_join_step(u, v))

        return steps

    def resolve_joins(self, primary_table: str, target_tables: list[str]) -> list[JoinStep]:
        """Resolve and merge join paths from primary_table to all target_tables.

        Maintains path ordering and eliminates duplicate table joins, forming an optimal
        join spanning tree rooted at primary_table.

        Args:
            primary_table: Starting primary table name or alias (FROM clause root).
            target_tables: List of target table names or aliases to be joined.

        Returns:
            Deduplicated list of JoinStep instances in valid join order.

        Raises:
            UnconnectedSchemaError: If primary_table or any target table cannot be connected.
        """
        primary = self._normalize_table_name(primary_table)
        joined_tables: set[str] = {primary}
        merged_steps: list[JoinStep] = []

        for target in target_tables:
            tgt = self._normalize_table_name(target)
            if tgt in joined_tables:
                continue

            path_steps = self.find_shortest_path(primary, tgt)
            for step in path_steps:
                if step.to_table not in joined_tables:
                    joined_tables.add(step.to_table)
                    merged_steps.append(step)

        return merged_steps
