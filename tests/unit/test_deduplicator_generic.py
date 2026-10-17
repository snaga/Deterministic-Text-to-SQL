"""Detroit-style comprehensive unit tests for generic OntologyDeduplicator.

Tests cover:
- External custom ontology injection (e.g. city -> prefecture, category -> department)
- Mathematical subsumption logic (C => P implies (C AND P) <=> C)
- Non-subsumptive conditions (mismatched values, cross-parent sets, unknown child entities)
- Guarding of negation and inequality operators (!=, NOT IN, <, >, etc.)
- Factory method OntologyDeduplicator.from_schema(schema) across diverse schema contexts
- Empty rules passthrough behavior
- 100% backward compatibility with DEFAULT_TPCH_HIERARCHY_RULES
"""

from pathlib import Path

import pytest

from deterministic_text_to_sql.compiler import (
    DEFAULT_TPCH_HIERARCHY_RULES,
    FilterSlot,
    HierarchyRule,
    OntologyDeduplicator,
)
from deterministic_text_to_sql.schema import (
    ColumnMetadata,
    OssieSchemaAdapter,
    SchemaMetadata,
    TableMetadata,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
TPCH_OSSIE_YAML = FIXTURES_DIR / "tpch_ossie.yaml"


# ---------------------------------------------------------------------------
# Test Fixtures & Custom Ontologies
# ---------------------------------------------------------------------------


@pytest.fixture
def japan_geo_rule() -> HierarchyRule:
    """Hierarchy rule: city -> prefecture in Japan."""
    return HierarchyRule(
        parent_table="addresses",
        parent_column="prefecture",
        child_table="addresses",
        child_column="city",
        child_to_parent={
            "SHINJUKU": "TOKYO",
            "SHIBUYA": "TOKYO",
            "MINATO": "TOKYO",
            "UMEDA": "OSAKA",
            "NAMBA": "OSAKA",
            "HAKATA": "FUKUOKA",
        },
    )


@pytest.fixture
def ecommerce_category_rule() -> HierarchyRule:
    """Hierarchy rule: category -> department across different tables."""
    return HierarchyRule(
        parent_table="departments",
        parent_column="dept_name",
        child_table="products",
        child_column="category_name",
        child_to_parent={
            "SMARTPHONE": "ELECTRONICS",
            "LAPTOP": "ELECTRONICS",
            "HEADPHONE": "AUDIO",
            "SPEAKER": "AUDIO",
            "T-SHIRT": "APPAREL",
        },
    )


# ---------------------------------------------------------------------------
# Test Suite 1: Generic Custom Ontology Mathematical Subsumption
# ---------------------------------------------------------------------------


class TestGenericCustomOntologySubsumption:
    """Verify mathematical subsumption with arbitrary user-defined ontologies."""

    def test_single_child_strictly_subsumes_single_parent(
        self, japan_geo_rule: HierarchyRule
    ) -> None:
        """city='SHINJUKU' strictly implies prefecture='TOKYO'; parent eliminated."""
        dedup = OntologyDeduplicator(rules=[japan_geo_rule])
        parent = FilterSlot(table="addresses", column="prefecture", operator="=", value="TOKYO")
        child = FilterSlot(table="addresses", column="city", operator="=", value="SHINJUKU")

        result = dedup.deduplicate([parent, child])
        assert result == [child]

        # Order reversed in input
        result_rev = dedup.deduplicate([child, parent])
        assert result_rev == [child]

    def test_multi_value_parent_subsumed_by_single_child(
        self, japan_geo_rule: HierarchyRule
    ) -> None:
        """prefecture IN ('TOKYO', 'OSAKA') subsumed by city='SHINJUKU'."""
        dedup = OntologyDeduplicator(rules=[japan_geo_rule])
        parent = FilterSlot(
            table="addresses", column="prefecture", operator="IN", value=["TOKYO", "OSAKA"]
        )
        child = FilterSlot(table="addresses", column="city", operator="=", value="SHINJUKU")

        result = dedup.deduplicate([parent, child])
        assert result == [child]

    def test_multi_value_parent_subsumed_by_multi_value_child(
        self, japan_geo_rule: HierarchyRule
    ) -> None:
        """prefecture IN ('TOKYO', 'OSAKA') subsumed by city IN ('SHINJUKU', 'UMEDA')."""
        dedup = OntologyDeduplicator(rules=[japan_geo_rule])
        parent = FilterSlot(
            table="addresses", column="prefecture", operator="IN", value=["TOKYO", "OSAKA"]
        )
        child = FilterSlot(
            table="addresses", column="city", operator="IN", value=["SHINJUKU", "UMEDA"]
        )

        result = dedup.deduplicate([parent, child])
        assert result == [child]

    def test_mismatched_parent_and_child_both_retained(
        self, japan_geo_rule: HierarchyRule
    ) -> None:
        """prefecture='OSAKA' and city='SHINJUKU' (TOKYO) do not subsume; both kept."""
        dedup = OntologyDeduplicator(rules=[japan_geo_rule])
        parent = FilterSlot(table="addresses", column="prefecture", operator="=", value="OSAKA")
        child = FilterSlot(table="addresses", column="city", operator="=", value="SHINJUKU")

        result = dedup.deduplicate([parent, child])
        assert result == [parent, child]

    def test_cross_parent_child_set_does_not_subsume_single_parent(
        self, japan_geo_rule: HierarchyRule
    ) -> None:
        """city IN ('SHINJUKU', 'UMEDA') implies {TOKYO, OSAKA};
        it does not imply prefecture='TOKYO'.
        """
        dedup = OntologyDeduplicator(rules=[japan_geo_rule])
        parent = FilterSlot(table="addresses", column="prefecture", operator="=", value="TOKYO")
        child = FilterSlot(
            table="addresses", column="city", operator="IN", value=["SHINJUKU", "UMEDA"]
        )

        result = dedup.deduplicate([parent, child])
        assert result == [parent, child]

    def test_unknown_child_entity_does_not_subsume_parent(
        self, japan_geo_rule: HierarchyRule
    ) -> None:
        """city='UNKNOWN_TOWN' not present in mapping; safety prevents elimination."""
        dedup = OntologyDeduplicator(rules=[japan_geo_rule])
        parent = FilterSlot(table="addresses", column="prefecture", operator="=", value="TOKYO")
        child = FilterSlot(table="addresses", column="city", operator="=", value="UNKNOWN_TOWN")

        result = dedup.deduplicate([parent, child])
        assert result == [parent, child]

    def test_cross_table_hierarchy_subsumption(
        self, ecommerce_category_rule: HierarchyRule
    ) -> None:
        """Subsumption works seamlessly across distinct parent and child tables."""
        dedup = OntologyDeduplicator(rules=[ecommerce_category_rule])
        parent = FilterSlot(
            table="departments", column="dept_name", operator="=", value="ELECTRONICS"
        )
        child = FilterSlot(
            table="products", column="category_name", operator="=", value="SMARTPHONE"
        )
        unrelated = FilterSlot(table="orders", column="status", operator="=", value="COMPLETED")

        result = dedup.deduplicate([unrelated, parent, child])
        assert result == [unrelated, child]

    def test_case_insensitivity_and_whitespace_trimming(
        self, japan_geo_rule: HierarchyRule
    ) -> None:
        """Normalization handles lowercase, uppercase, and trailing whitespace."""
        dedup = OntologyDeduplicator(rules=[japan_geo_rule])
        parent = FilterSlot(table="ADDRESSES", column="Prefecture", operator="=", value="tokyo ")
        child = FilterSlot(table="addresses", column="CITY", operator="==", value="  shinjuku")

        result = dedup.deduplicate([parent, child])
        assert result == [child]


# ---------------------------------------------------------------------------
# Test Suite 2: Operator Guards & Non-Subsumptive Relations
# ---------------------------------------------------------------------------


class TestOperatorGuardsAndEdgeCases:
    """Verify that negation, inequalities, and non-equality operators are strictly preserved."""

    def test_parent_negation_operator_guarded(self, japan_geo_rule: HierarchyRule) -> None:
        """prefecture != 'TOKYO' must never be eliminated by city = 'SHINJUKU'."""
        dedup = OntologyDeduplicator(rules=[japan_geo_rule])
        parent = FilterSlot(table="addresses", column="prefecture", operator="!=", value="TOKYO")
        child = FilterSlot(table="addresses", column="city", operator="=", value="SHINJUKU")

        result = dedup.deduplicate([parent, child])
        assert result == [parent, child]

    def test_parent_not_in_operator_guarded(self, japan_geo_rule: HierarchyRule) -> None:
        """prefecture NOT IN ('TOKYO', 'OSAKA') must never be eliminated."""
        dedup = OntologyDeduplicator(rules=[japan_geo_rule])
        parent = FilterSlot(
            table="addresses", column="prefecture", operator="NOT IN", value=["TOKYO", "OSAKA"]
        )
        child = FilterSlot(table="addresses", column="city", operator="=", value="SHINJUKU")

        result = dedup.deduplicate([parent, child])
        assert result == [parent, child]

    def test_child_negation_operator_does_not_subsume_parent(
        self, japan_geo_rule: HierarchyRule
    ) -> None:
        """city != 'SHINJUKU' does NOT imply prefecture = 'TOKYO'."""
        dedup = OntologyDeduplicator(rules=[japan_geo_rule])
        parent = FilterSlot(table="addresses", column="prefecture", operator="=", value="TOKYO")
        child = FilterSlot(table="addresses", column="city", operator="!=", value="SHINJUKU")

        result = dedup.deduplicate([parent, child])
        assert result == [parent, child]

    def test_inequality_operators_guarded(self, japan_geo_rule: HierarchyRule) -> None:
        """Inequality filters (<, >, <=, >=, LIKE) are never treated as positive equality."""
        dedup = OntologyDeduplicator(rules=[japan_geo_rule])
        parent = FilterSlot(table="addresses", column="prefecture", operator="LIKE", value="TOK%")
        child = FilterSlot(table="addresses", column="city", operator="=", value="SHINJUKU")

        result = dedup.deduplicate([parent, child])
        assert result == [parent, child]

    def test_structural_rule_without_mapping(self) -> None:
        """When child_to_parent is empty, presence of child unconditionally subsumes parent."""
        structural_rule = HierarchyRule(
            parent_table="categories",
            parent_column="cat_id",
            child_table="subcategories",
            child_column="subcat_id",
            child_to_parent={},
        )
        dedup = OntologyDeduplicator(rules=[structural_rule])
        parent = FilterSlot(table="categories", column="cat_id", operator="=", value="C1")
        child = FilterSlot(table="subcategories", column="subcat_id", operator="=", value="SC1")

        result = dedup.deduplicate([parent, child])
        assert result == [child]


# ---------------------------------------------------------------------------
# Test Suite 3: OntologyDeduplicator.from_schema Factory Method
# ---------------------------------------------------------------------------


class TestOntologyDeduplicatorFromSchema:
    """Verify OntologyDeduplicator.from_schema across schema configurations."""

    def test_from_schema_with_ossie_yaml(self) -> None:
        """Load from tpch_ossie.yaml and verify nation->region rules are active."""
        assert TPCH_OSSIE_YAML.exists(), f"Missing fixture: {TPCH_OSSIE_YAML}"
        schema = OssieSchemaAdapter.load_from_yaml(TPCH_OSSIE_YAML)

        dedup = OntologyDeduplicator.from_schema(schema)
        assert len(dedup.rules) == 1
        rule = dedup.rules[0]
        assert rule.parent_table == "region"
        assert rule.child_table == "nation"
        assert rule.child_to_parent["JAPAN"] == "ASIA"

        parent = FilterSlot(table="region", column="r_name", operator="=", value="ASIA")
        child = FilterSlot(table="nation", column="n_name", operator="=", value="JAPAN")
        result = dedup.deduplicate([parent, child])
        assert result == [child]

    def test_from_schema_with_custom_hierarchies(self, japan_geo_rule: HierarchyRule) -> None:
        """Schema with custom hierarchies directly injects those rules."""
        custom_schema = SchemaMetadata(
            title="Custom Geo Schema",
            tables=[
                TableMetadata(
                    name="addresses",
                    description="Addresses table",
                    columns=[
                        ColumnMetadata(
                            name="prefecture", type="VARCHAR", description="Pref", role="dimension"
                        ),
                        ColumnMetadata(
                            name="city", type="VARCHAR", description="City", role="dimension"
                        ),
                    ],
                )
            ],
            hierarchies=[japan_geo_rule],
        )

        dedup = OntologyDeduplicator.from_schema(custom_schema)
        assert len(dedup.rules) == 1
        assert dedup.rules[0].parent_column == "prefecture"
        assert dedup.rules[0].child_column == "city"

        parent = FilterSlot(table="addresses", column="prefecture", operator="=", value="TOKYO")
        child = FilterSlot(table="addresses", column="city", operator="=", value="SHINJUKU")
        assert dedup.deduplicate([parent, child]) == [child]

    def test_from_schema_fallback_to_tpch_when_nation_region_present_but_hierarchies_empty(
        self,
    ) -> None:
        """When hierarchies is empty, presence of 'nation' & 'region' falls back
        to default TPC-H rules.
        """
        legacy_like_tpch = SchemaMetadata(
            title="Legacy TPC-H Schema",
            tables=[
                TableMetadata(
                    name="region",
                    description="Region table",
                    columns=[
                        ColumnMetadata(
                            name="r_name",
                            type="VARCHAR",
                            description="Region name",
                            role="dimension",
                        )
                    ],
                ),
                TableMetadata(
                    name="nation",
                    description="Nation table",
                    columns=[
                        ColumnMetadata(
                            name="n_name",
                            type="VARCHAR",
                            description="Nation name",
                            role="dimension",
                        )
                    ],
                ),
            ],
            hierarchies=[],  # Empty
        )

        dedup = OntologyDeduplicator.from_schema(legacy_like_tpch)
        assert len(dedup.rules) == 1
        assert dedup.rules[0].parent_table == "region"
        assert dedup.rules[0].child_table == "nation"

        parent = FilterSlot(table="region", column="r_name", operator="=", value="EUROPE")
        child = FilterSlot(table="nation", column="n_name", operator="=", value="GERMANY")
        assert dedup.deduplicate([parent, child]) == [child]

    def test_from_schema_empty_rules_for_non_tpch_schema_without_hierarchies(self) -> None:
        """Non-TPC-H schema without hierarchies initializes with empty rule list []."""
        flights_schema = SchemaMetadata(
            title="Flights Schema",
            tables=[
                TableMetadata(
                    name="flights",
                    description="Flights table",
                    columns=[
                        ColumnMetadata(
                            name="flight_no", type="VARCHAR", description="Flight No", role="id"
                        )
                    ],
                ),
                TableMetadata(
                    name="airports",
                    description="Airports table",
                    columns=[
                        ColumnMetadata(
                            name="iata_code", type="VARCHAR", description="IATA", role="dimension"
                        )
                    ],
                ),
            ],
            hierarchies=[],
        )

        dedup = OntologyDeduplicator.from_schema(flights_schema)
        assert dedup.rules == []

        # Filters pass through without alteration even if table names resemble hierarchy
        f1 = FilterSlot(table="airports", column="iata_code", operator="=", value="HND")
        f2 = FilterSlot(table="flights", column="flight_no", operator="=", value="JL001")
        assert dedup.deduplicate([f1, f2]) == [f1, f2]


# ---------------------------------------------------------------------------
# Test Suite 4: Empty Rules Passthrough & Backward Compatibility
# ---------------------------------------------------------------------------


class TestEmptyRulesPassthroughAndCompatibility:
    """Verify transparent passthrough when rules=[] and backward compatibility."""

    def test_empty_rules_passthrough_all_filters(self) -> None:
        """Deduplicator initialized with rules=[] retains all filters intact."""
        dedup = OntologyDeduplicator(rules=[])
        assert dedup.rules == []

        f1 = FilterSlot(table="orders", column="o_orderkey", operator="=", value=100)
        f2 = FilterSlot(table="customer", column="c_nationkey", operator="=", value=1)
        f3 = FilterSlot(table="region", column="r_name", operator="=", value="ASIA")
        f4 = FilterSlot(table="nation", column="n_name", operator="=", value="JAPAN")

        result = dedup.deduplicate([f1, f2, f3, f4])
        assert result == [f1, f2, f3, f4]

    def test_empty_filters_input(self) -> None:
        """Empty filter list returns empty list."""
        dedup = OntologyDeduplicator(rules=[])
        assert dedup.deduplicate([]) == []

    def test_backward_compatibility_default_constructor(self) -> None:
        """OntologyDeduplicator() with no arguments defaults to DEFAULT_TPCH_HIERARCHY_RULES."""
        dedup = OntologyDeduplicator()
        assert len(dedup.rules) == len(DEFAULT_TPCH_HIERARCHY_RULES)
        assert dedup.rules[0].parent_table == "region"
        assert dedup.rules[0].child_table == "nation"

        # Verify deduplication still functions identically
        parent = FilterSlot(table="region", column="r_name", operator="=", value="AMERICA")
        child = FilterSlot(table="nation", column="n_name", operator="=", value="CANADA")
        assert dedup.deduplicate([parent, child]) == [child]

    def test_reexport_of_hierarchy_rule_from_compiler(self) -> None:
        """Ensure HierarchyRule can be imported and instantiated from compiler module."""
        rule = HierarchyRule(
            parent_table="parent",
            parent_column="p_col",
            child_table="child",
            child_column="c_col",
            child_to_parent={"A": "B"},
        )
        assert rule.parent_table == "parent"
        assert rule.child_to_parent["A"] == "B"
