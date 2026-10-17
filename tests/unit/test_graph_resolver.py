import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from deterministic_text_to_sql.graph import (
    JoinStep,
    SchemaGraph,
    UnconnectedSchemaError,
)
from deterministic_text_to_sql.schema import SchemaMetadata, load_schema_from_json

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "tpch_schema.json"


@pytest.fixture
def tpch_schema() -> SchemaMetadata:
    """Fixture providing loaded TPC-H schema metadata."""
    return load_schema_from_json(FIXTURE_PATH)


@pytest.fixture
def schema_graph(tpch_schema: SchemaMetadata) -> SchemaGraph:
    """Fixture providing initialized SchemaGraph for TPC-H."""
    return SchemaGraph(tpch_schema)


@pytest.fixture
def disconnected_schema_graph() -> SchemaGraph:
    """Fixture providing SchemaGraph with an isolated table for connectivity tests."""
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    data["tables"].append(
        {
            "name": "isolated_audit_log",
            "description": "孤立した監査ログテーブル（外部キーなし）",
            "aliases": ["監査ログ", "audit"],
            "primary_key": ["log_id"],
            "foreign_keys": [],
            "columns": [
                {"name": "log_id", "type": "BIGINT", "description": "ログID", "role": "id"},
                {
                    "name": "action",
                    "type": "VARCHAR(50)",
                    "description": "操作",
                    "role": "dimension",
                },
            ],
        }
    )
    return SchemaGraph(data)


class TestSchemaGraphInitialization:
    """Tests for SchemaGraph initialization and properties."""

    def test_init_with_schema_metadata(self, tpch_schema: SchemaMetadata) -> None:
        graph = SchemaGraph(tpch_schema)
        assert graph.schema == tpch_schema
        assert graph.graph.number_of_nodes() == len(tpch_schema.tables)

    def test_init_with_dict(self) -> None:
        data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        graph = SchemaGraph(data)
        assert isinstance(graph.schema, SchemaMetadata)
        assert graph.graph.number_of_nodes() == 8

    def test_edge_count_and_connectivity(self, schema_graph: SchemaGraph) -> None:
        g = schema_graph.graph
        # Check node presence
        expected_tables = {
            "region",
            "nation",
            "supplier",
            "customer",
            "part",
            "partsupp",
            "orders",
            "lineitem",
        }
        assert set(g.nodes()) == expected_tables

        # Check key edges
        assert g.has_edge("nation", "region")
        assert g.has_edge("customer", "nation")
        assert g.has_edge("supplier", "nation")
        assert g.has_edge("orders", "customer")
        assert g.has_edge("lineitem", "orders")
        assert g.has_edge("lineitem", "part")
        assert g.has_edge("lineitem", "supplier")
        assert g.has_edge("lineitem", "partsupp")
        assert g.has_edge("partsupp", "part")
        assert g.has_edge("partsupp", "supplier")


class TestFindShortestPath:
    """Tests for find_shortest_path method."""

    def test_same_table_returns_empty_list(self, schema_graph: SchemaGraph) -> None:
        steps = schema_graph.find_shortest_path("lineitem", "lineitem")
        assert steps == []

    def test_adjacent_tables_forward(self, schema_graph: SchemaGraph) -> None:
        steps = schema_graph.find_shortest_path("customer", "nation")
        assert len(steps) == 1
        assert steps[0] == JoinStep(
            from_table="customer",
            to_table="nation",
            join_pairs=[("c_nationkey", "n_nationkey")],
            on_clause="customer.c_nationkey = nation.n_nationkey",
        )

    def test_adjacent_tables_reverse(self, schema_graph: SchemaGraph) -> None:
        steps = schema_graph.find_shortest_path("nation", "customer")
        assert len(steps) == 1
        assert steps[0] == JoinStep(
            from_table="nation",
            to_table="customer",
            join_pairs=[("n_nationkey", "c_nationkey")],
            on_clause="nation.n_nationkey = customer.c_nationkey",
        )

    def test_composite_foreign_key_join(self, schema_graph: SchemaGraph) -> None:
        steps = schema_graph.find_shortest_path("lineitem", "partsupp")
        assert len(steps) == 1
        expected_on = (
            "lineitem.l_partkey = partsupp.ps_partkey AND lineitem.l_suppkey = partsupp.ps_suppkey"
        )
        assert steps[0] == JoinStep(
            from_table="lineitem",
            to_table="partsupp",
            join_pairs=[("l_partkey", "ps_partkey"), ("l_suppkey", "ps_suppkey")],
            on_clause=expected_on,
        )

    def test_multi_hop_join_lineitem_to_region(self, schema_graph: SchemaGraph) -> None:
        # Shortest path in TPC-H graph from lineitem to region is via supplier:
        # lineitem -> supplier -> nation -> region (3 hops)
        steps = schema_graph.find_shortest_path("lineitem", "region")
        assert len(steps) == 3

        expected_sequence = [
            ("lineitem", "supplier", [("l_suppkey", "s_suppkey")]),
            ("supplier", "nation", [("s_nationkey", "n_nationkey")]),
            ("nation", "region", [("n_regionkey", "r_regionkey")]),
        ]

        for step, (expected_from, expected_to, expected_pairs) in zip(
            steps, expected_sequence, strict=True
        ):
            assert step.from_table == expected_from
            assert step.to_table == expected_to
            assert step.join_pairs == expected_pairs
            expected_clause_part = f"{expected_from}.{expected_pairs[0][0]}"
            assert expected_clause_part in step.on_clause

    def test_multi_hop_join_orders_to_region(self, schema_graph: SchemaGraph) -> None:
        # orders -> customer -> nation -> region (3 hops)
        steps = schema_graph.find_shortest_path("orders", "region")
        assert len(steps) == 3

        expected_sequence = [
            ("orders", "customer", [("o_custkey", "c_custkey")]),
            ("customer", "nation", [("c_nationkey", "n_nationkey")]),
            ("nation", "region", [("n_regionkey", "r_regionkey")]),
        ]

        for step, (expected_from, expected_to, expected_pairs) in zip(
            steps, expected_sequence, strict=True
        ):
            assert step.from_table == expected_from
            assert step.to_table == expected_to
            assert step.join_pairs == expected_pairs

    def test_multi_hop_reverse_region_to_lineitem(self, schema_graph: SchemaGraph) -> None:
        steps = schema_graph.find_shortest_path("region", "lineitem")
        assert len(steps) == 3
        assert [s.from_table for s in steps] == ["region", "nation", "supplier"]
        assert [s.to_table for s in steps] == ["nation", "supplier", "lineitem"]

    def test_alias_support_in_path_finding(self, schema_graph: SchemaGraph) -> None:
        # "明細" -> lineitem, "地域" -> region
        steps = schema_graph.find_shortest_path("明細", "地域")
        assert len(steps) == 3
        assert steps[0].from_table == "lineitem"
        assert steps[-1].to_table == "region"

    def test_unconnected_table_raises_error(self, disconnected_schema_graph: SchemaGraph) -> None:
        with pytest.raises(UnconnectedSchemaError) as exc_info:
            disconnected_schema_graph.find_shortest_path("lineitem", "isolated_audit_log")
        assert "No join path exists" in str(exc_info.value)

    def test_unknown_table_raises_error(self, schema_graph: SchemaGraph) -> None:
        with pytest.raises(UnconnectedSchemaError) as exc_info:
            schema_graph.find_shortest_path("lineitem", "non_existent_table")
        assert "does not exist in schema" in str(exc_info.value)


class TestResolveJoins:
    """Tests for resolve_joins method."""

    def test_resolve_empty_target_tables(self, schema_graph: SchemaGraph) -> None:
        steps = schema_graph.resolve_joins("lineitem", [])
        assert steps == []

    def test_resolve_primary_in_targets_is_ignored(self, schema_graph: SchemaGraph) -> None:
        steps = schema_graph.resolve_joins("lineitem", ["lineitem"])
        assert steps == []

    def test_resolve_multiple_direct_targets(self, schema_graph: SchemaGraph) -> None:
        # primary = lineitem, targets = [orders, part, supplier] (from requirement)
        steps = schema_graph.resolve_joins("lineitem", ["orders", "part", "supplier"])
        assert len(steps) == 3
        assert [s.to_table for s in steps] == ["orders", "part", "supplier"]
        assert all(s.from_table == "lineitem" for s in steps)

    def test_resolve_merging_overlapping_paths(self, schema_graph: SchemaGraph) -> None:
        # primary = orders, targets = [region, customer, part]
        # orders -> customer -> nation -> region
        # customer is an intermediate hop for region, so customer should NOT be joined again!
        # part is resolved via orders -> lineitem -> part
        steps = schema_graph.resolve_joins("orders", ["region", "customer", "part"])

        # Expected joined order: customer, nation, region, lineitem, part
        joined_to_tables = [s.to_table for s in steps]
        assert joined_to_tables == ["customer", "nation", "region", "lineitem", "part"]

        # Ensure customer only appears once in the join tree
        assert joined_to_tables.count("customer") == 1

    def test_resolve_with_aliases(self, schema_graph: SchemaGraph) -> None:
        steps = schema_graph.resolve_joins("明細", ["注文", "顧客"])
        # lineitem -> orders, lineitem -> orders -> customer
        joined_to_tables = [s.to_table for s in steps]
        assert joined_to_tables == ["orders", "customer"]

    def test_resolve_unconnected_raises_error(self, disconnected_schema_graph: SchemaGraph) -> None:
        with pytest.raises(UnconnectedSchemaError):
            disconnected_schema_graph.resolve_joins("lineitem", ["orders", "isolated_audit_log"])


class TestJoinStepModel:
    """Tests for JoinStep immutability and behavior."""

    def test_join_step_immutable(self) -> None:
        step = JoinStep(
            from_table="orders",
            to_table="customer",
            join_pairs=[("o_custkey", "c_custkey")],
            on_clause="orders.o_custkey = customer.c_custkey",
        )
        with pytest.raises(ValidationError):
            step.from_table = "lineitem"

    def test_join_step_equality(self) -> None:
        step1 = JoinStep(
            from_table="orders",
            to_table="customer",
            join_pairs=[("o_custkey", "c_custkey")],
            on_clause="orders.o_custkey = customer.c_custkey",
        )
        step2 = JoinStep(
            from_table="orders",
            to_table="customer",
            join_pairs=[("o_custkey", "c_custkey")],
            on_clause="orders.o_custkey = customer.c_custkey",
        )
        assert step1 == step2
        assert hash(step1) == hash(step2)
