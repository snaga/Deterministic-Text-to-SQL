"""Unit tests for TPC-H Apache Ossie semantic model fixture (tests/fixtures/tpch_ossie.yaml).

Verifies full compatibility with SchemaMetadata, lossless equivalence with legacy
tpch_schema.json, foreign key relationships, metrics definitions, and ontology hierarchies.
"""

from pathlib import Path

import pytest

from deterministic_text_to_sql.compiler.deduplicator import TPCH_NATION_TO_REGION
from deterministic_text_to_sql.graph import SchemaGraph
from deterministic_text_to_sql.schema import (
    OssieSchemaAdapter,
    SchemaLoader,
    SchemaMetadata,
    load_schema,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
TPCH_OSSIE_YAML = FIXTURES_DIR / "tpch_ossie.yaml"
TPCH_LEGACY_JSON = FIXTURES_DIR / "tpch_schema.json"

EXPECTED_TABLE_NAMES = {
    "region",
    "nation",
    "supplier",
    "customer",
    "part",
    "partsupp",
    "orders",
    "lineitem",
}

EXPECTED_COLUMN_COUNTS = {
    "region": 3,
    "nation": 4,
    "supplier": 7,
    "customer": 8,
    "part": 9,
    "partsupp": 5,
    "orders": 9,
    "lineitem": 16,
}


@pytest.fixture(scope="module")
def ossie_schema() -> SchemaMetadata:
    """Fixture to load TPC-H Ossie schema once per module."""
    assert TPCH_OSSIE_YAML.exists(), f"Missing fixture file: {TPCH_OSSIE_YAML}"
    return OssieSchemaAdapter.load_from_yaml(TPCH_OSSIE_YAML)


@pytest.fixture(scope="module")
def legacy_schema() -> SchemaMetadata:
    """Fixture to load legacy TPC-H JSON schema once per module."""
    assert TPCH_LEGACY_JSON.exists(), f"Missing fixture file: {TPCH_LEGACY_JSON}"
    return SchemaLoader.load_from_json(TPCH_LEGACY_JSON)


class TestTpchOssieLoading:
    """Tests for loading and verifying the structure of tpch_ossie.yaml."""

    def test_load_tpch_ossie_yaml_metadata(self, ossie_schema: SchemaMetadata) -> None:
        """Verify top-level metadata attributes from Ossie YAML."""
        assert ossie_schema.title == "tpch_semantic_model"
        assert ossie_schema.description is not None
        assert "TPC-H Benchmark" in ossie_schema.description
        assert len(ossie_schema.tables) == 8

    def test_load_schema_unified_entrypoint(self) -> None:
        """Verify unified load_schema function automatically parses YAML fixture."""
        schema = load_schema(TPCH_OSSIE_YAML)
        assert isinstance(schema, SchemaMetadata)
        assert schema.title == "tpch_semantic_model"
        assert len(schema.tables) == 8

    def test_all_eight_tables_exist(self, ossie_schema: SchemaMetadata) -> None:
        """Verify all 8 TPC-H tables exist with expected column counts."""
        loaded_table_names = {t.name for t in ossie_schema.tables}
        assert loaded_table_names == EXPECTED_TABLE_NAMES

        for table in ossie_schema.tables:
            assert len(table.columns) == EXPECTED_COLUMN_COUNTS[table.name]

    def test_primary_keys(self, ossie_schema: SchemaMetadata) -> None:
        """Verify primary key definitions for single and composite keys."""
        region = ossie_schema.get_table("region")
        assert region is not None
        assert region.primary_key == ["r_regionkey"]

        partsupp = ossie_schema.get_table("partsupp")
        assert partsupp is not None
        assert partsupp.primary_key == ["ps_partkey", "ps_suppkey"]

        lineitem = ossie_schema.get_table("lineitem")
        assert lineitem is not None
        assert lineitem.primary_key == ["l_orderkey", "l_linenumber"]

    def test_table_synonyms_and_alias_lookup(self, ossie_schema: SchemaMetadata) -> None:
        """Verify get_table lookup using Japanese synonyms and English aliases."""
        assert ossie_schema.get_table("顧客") is not None
        assert ossie_schema.get_table("顧客").name == "customer"  # type: ignore[union-attr]

        assert ossie_schema.get_table("仕入先") is not None
        assert ossie_schema.get_table("仕入先").name == "supplier"  # type: ignore[union-attr]

        assert ossie_schema.get_table("注文") is not None
        assert ossie_schema.get_table("注文").name == "orders"  # type: ignore[union-attr]

        assert ossie_schema.get_table("明細") is not None
        assert ossie_schema.get_table("明細").name == "lineitem"  # type: ignore[union-attr]

        assert ossie_schema.get_table("在庫") is not None
        assert ossie_schema.get_table("在庫").name == "partsupp"  # type: ignore[union-attr]

    def test_column_synonyms_and_lookup(self, ossie_schema: SchemaMetadata) -> None:
        """Verify column lookup and synonyms on tables."""
        customer = ossie_schema.get_table("customer")
        assert customer is not None

        c_name = customer.get_column("顧客名")
        assert c_name is not None
        assert c_name.name == "c_name"
        assert c_name.type == "VARCHAR(25)"
        assert c_name.role == "dimension"

        c_acctbal = customer.get_column("口座残高")
        assert c_acctbal is not None
        assert c_acctbal.name == "c_acctbal"
        assert c_acctbal.role == "metric"


class TestTpchOssieRelationships:
    """Tests for foreign key relationships defined in tpch_ossie.yaml."""

    def test_all_ten_relationships_present(self, ossie_schema: SchemaMetadata) -> None:
        """Verify all 10 expected foreign key relationships are restored across tables."""
        # Nation -> Region
        nation = ossie_schema.get_table("nation")
        assert nation is not None
        fk_n_to_r = [fk for fk in nation.foreign_keys if fk.ref_table == "region"]
        assert len(fk_n_to_r) == 1
        assert fk_n_to_r[0].column == "n_regionkey"
        assert fk_n_to_r[0].ref_column == "r_regionkey"

        # Supplier -> Nation
        supplier = ossie_schema.get_table("supplier")
        assert supplier is not None
        fk_s_to_n = [fk for fk in supplier.foreign_keys if fk.ref_table == "nation"]
        assert len(fk_s_to_n) == 1
        assert fk_s_to_n[0].column == "s_nationkey"
        assert fk_s_to_n[0].ref_column == "n_nationkey"

        # Customer -> Nation
        customer = ossie_schema.get_table("customer")
        assert customer is not None
        fk_c_to_n = [fk for fk in customer.foreign_keys if fk.ref_table == "nation"]
        assert len(fk_c_to_n) == 1
        assert fk_c_to_n[0].column == "c_nationkey"
        assert fk_c_to_n[0].ref_column == "n_nationkey"

        # Orders -> Customer
        orders = ossie_schema.get_table("orders")
        assert orders is not None
        fk_o_to_c = [fk for fk in orders.foreign_keys if fk.ref_table == "customer"]
        assert len(fk_o_to_c) == 1
        assert fk_o_to_c[0].column == "o_custkey"
        assert fk_o_to_c[0].ref_column == "c_custkey"

        # Partsupp -> Part & Supplier
        partsupp = ossie_schema.get_table("partsupp")
        assert partsupp is not None
        fk_ps_to_p = [fk for fk in partsupp.foreign_keys if fk.ref_table == "part"]
        assert len(fk_ps_to_p) == 1
        assert fk_ps_to_p[0].column == "ps_partkey"
        assert fk_ps_to_p[0].ref_column == "p_partkey"

        fk_ps_to_s = [fk for fk in partsupp.foreign_keys if fk.ref_table == "supplier"]
        assert len(fk_ps_to_s) == 1
        assert fk_ps_to_s[0].column == "ps_suppkey"
        assert fk_ps_to_s[0].ref_column == "s_suppkey"

        # Lineitem -> Orders, Part, Supplier, Partsupp (composite)
        lineitem = ossie_schema.get_table("lineitem")
        assert lineitem is not None
        assert len(lineitem.foreign_keys) == 4

        fk_l_to_o = [fk for fk in lineitem.foreign_keys if fk.ref_table == "orders"]
        assert len(fk_l_to_o) == 1
        assert fk_l_to_o[0].column == "l_orderkey"
        assert fk_l_to_o[0].ref_column == "o_orderkey"

        fk_l_to_p = [fk for fk in lineitem.foreign_keys if fk.ref_table == "part"]
        assert len(fk_l_to_p) == 1
        assert fk_l_to_p[0].column == "l_partkey"
        assert fk_l_to_p[0].ref_column == "p_partkey"

        fk_l_to_s = [fk for fk in lineitem.foreign_keys if fk.ref_table == "supplier"]
        assert len(fk_l_to_s) == 1
        assert fk_l_to_s[0].column == "l_suppkey"
        assert fk_l_to_s[0].ref_column == "s_suppkey"

        # Composite FK: lineitem -> partsupp
        fk_l_to_ps = [fk for fk in lineitem.foreign_keys if fk.ref_table == "partsupp"]
        assert len(fk_l_to_ps) == 1
        assert fk_l_to_ps[0].column == ["l_partkey", "l_suppkey"]
        assert fk_l_to_ps[0].ref_column == ["ps_partkey", "ps_suppkey"]


class TestTpchOssieMetrics:
    """Tests for semantic metrics in tpch_ossie.yaml."""

    def test_metrics_count_and_definitions(self, ossie_schema: SchemaMetadata) -> None:
        """Verify all 6 representative metrics are loaded with correct expressions."""
        assert len(ossie_schema.metrics) == 6
        metric_names = {m.name for m in ossie_schema.metrics}
        expected_metrics = {
            "total_revenue",
            "total_quantity",
            "avg_price",
            "total_acctbal",
            "order_count",
            "customer_count",
        }
        assert metric_names == expected_metrics

        rev = ossie_schema.get_metric("total_revenue")
        assert rev is not None
        assert "lineitem.l_extendedprice * (1 - lineitem.l_discount)" in rev.expression
        assert rev.datatype == "DOUBLE"

        qty = ossie_schema.get_metric("total_quantity")
        assert qty is not None
        assert rev.expression != qty.expression
        assert "lineitem.l_quantity" in qty.expression

        cnt = ossie_schema.get_metric("order_count")
        assert cnt is not None
        assert cnt.datatype == "BIGINT"
        assert "COUNT(orders.o_orderkey)" in cnt.expression

    def test_metric_synonym_lookup(self, ossie_schema: SchemaMetadata) -> None:
        """Verify get_metric lookup with Japanese and English synonyms."""
        # Japanese synonyms
        assert ossie_schema.get_metric("売上") is not None
        assert ossie_schema.get_metric("売上").name == "total_revenue"  # type: ignore[union-attr]

        assert ossie_schema.get_metric("数量") is not None
        assert ossie_schema.get_metric("数量").name == "total_quantity"  # type: ignore[union-attr]

        assert ossie_schema.get_metric("平均価格") is not None
        assert ossie_schema.get_metric("平均価格").name == "avg_price"  # type: ignore[union-attr]

        assert ossie_schema.get_metric("口座残高合計") is not None
        assert ossie_schema.get_metric("口座残高合計").name == "total_acctbal"  # type: ignore[union-attr]

        assert ossie_schema.get_metric("注文数") is not None
        assert ossie_schema.get_metric("注文数").name == "order_count"  # type: ignore[union-attr]

        assert ossie_schema.get_metric("顧客数") is not None
        assert ossie_schema.get_metric("顧客数").name == "customer_count"  # type: ignore[union-attr]

        # English synonyms (case-insensitive)
        assert ossie_schema.get_metric("REVENUE") is not None
        assert ossie_schema.get_metric("REVENUE").name == "total_revenue"  # type: ignore[union-attr]


class TestTpchOssieHierarchies:
    """Tests for ontology hierarchies in tpch_ossie.yaml."""

    def test_nation_to_region_hierarchy(self, ossie_schema: SchemaMetadata) -> None:
        """Verify custom_extensions parses nation->region hierarchy matching
        TPCH_NATION_TO_REGION.
        """
        assert len(ossie_schema.hierarchies) == 1
        rule = ossie_schema.hierarchies[0]

        assert rule.parent_table == "region"
        assert rule.parent_column == "r_name"
        assert rule.child_table == "nation"
        assert rule.child_column == "n_name"

        assert len(rule.child_to_parent) == 25
        assert rule.child_to_parent == TPCH_NATION_TO_REGION


class TestTpchOssieEquivalenceWithLegacyJson:
    """Tests comparing Ossie YAML loading with legacy tpch_schema.json."""

    def test_table_and_column_structural_equivalence(
        self,
        ossie_schema: SchemaMetadata,
        legacy_schema: SchemaMetadata,
    ) -> None:
        """Verify tables and columns match between YAML and legacy JSON schemas."""
        ossie_tables = {t.name for t in ossie_schema.tables}
        legacy_tables = {t.name for t in legacy_schema.tables}
        assert ossie_tables == legacy_tables

        for tbl_name in ossie_tables:
            o_tbl = ossie_schema.get_table(tbl_name)
            l_tbl = legacy_schema.get_table(tbl_name)
            assert o_tbl is not None
            assert l_tbl is not None

            # Primary key match
            assert o_tbl.primary_key == l_tbl.primary_key

            # Columns match
            o_cols = {c.name: c for c in o_tbl.columns}
            l_cols = {c.name: c for c in l_tbl.columns}
            assert set(o_cols.keys()) == set(l_cols.keys())

            for col_name, o_col in o_cols.items():
                l_col = l_cols[col_name]
                assert o_col.type == l_col.type
                assert o_col.role == l_col.role

    def test_foreign_keys_equivalence(
        self,
        ossie_schema: SchemaMetadata,
        legacy_schema: SchemaMetadata,
    ) -> None:
        """Verify foreign keys are equivalent between YAML and legacy JSON."""
        for tbl_name in EXPECTED_TABLE_NAMES:
            o_tbl = ossie_schema.get_table(tbl_name)
            l_tbl = legacy_schema.get_table(tbl_name)
            assert o_tbl is not None
            assert l_tbl is not None

            o_fks = sorted(
                [(str(fk.column), fk.ref_table, str(fk.ref_column)) for fk in o_tbl.foreign_keys]
            )
            l_fks = sorted(
                [(str(fk.column), fk.ref_table, str(fk.ref_column)) for fk in l_tbl.foreign_keys]
            )
            assert o_fks == l_fks

    def test_schema_graph_join_paths_identical(
        self,
        ossie_schema: SchemaMetadata,
        legacy_schema: SchemaMetadata,
    ) -> None:
        """Verify SchemaGraph computes identical join paths using both schemas."""
        ossie_graph = SchemaGraph(ossie_schema)
        legacy_graph = SchemaGraph(legacy_schema)

        test_cases = [
            ("lineitem", ["region"]),
            ("customer", ["orders", "part"]),
            ("partsupp", ["region"]),
            ("nation", ["lineitem"]),
        ]

        for primary, targets in test_cases:
            ossie_path = ossie_graph.resolve_joins(primary, targets)
            legacy_path = legacy_graph.resolve_joins(primary, targets)

            assert len(ossie_path) == len(legacy_path)
            for o_step, l_step in zip(ossie_path, legacy_path, strict=True):
                assert o_step.from_table == l_step.from_table
                assert o_step.to_table == l_step.to_table
                assert o_step.join_pairs == l_step.join_pairs
                assert o_step.on_clause == l_step.on_clause
