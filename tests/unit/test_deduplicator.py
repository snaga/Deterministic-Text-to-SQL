"""Detroit-style unit tests for OntologyDeduplicator and compiler models."""

import pytest
from pydantic import ValidationError

from deterministic_text_to_sql.compiler import (
    DEFAULT_TPCH_HIERARCHY_RULES,
    FilterSlot,
    HierarchyRule,
    OntologyDeduplicator,
    QueryIntent,
)


class TestFilterSlotAndQueryIntentModels:
    """Tests for compiler data models."""

    def test_filter_slot_creation_and_immutability(self) -> None:
        slot = FilterSlot(
            table="nation",
            column="n_name",
            operator="=",
            value="JAPAN",
            display_label="国: 日本",
        )
        assert slot.table == "nation"
        assert slot.column == "n_name"
        assert slot.operator == "="
        assert slot.value == "JAPAN"
        assert slot.display_label == "国: 日本"

        # Immutability check (frozen=True)
        with pytest.raises(ValidationError):
            slot.value = "CHINA"

    def test_query_intent_creation_and_defaults(self) -> None:
        slot = FilterSlot(table="region", column="r_name", operator="=", value="ASIA")
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["c_name"],
            metrics=[("o_totalprice", "SUM")],
            filters=[slot],
            limit=10,
            order_by=[("o_totalprice", "DESC")],
        )

        assert intent.primary_table == "orders"
        assert intent.dimensions == ["c_name"]
        assert intent.metrics == [("o_totalprice", "SUM")]
        assert len(intent.filters) == 1
        assert intent.limit == 10
        assert intent.order_by == [("o_totalprice", "DESC")]

        # Immutability check
        with pytest.raises(ValidationError):
            intent.limit = 20

    def test_query_intent_default_factory(self) -> None:
        intent = QueryIntent(primary_table="lineitem")
        assert intent.primary_table == "lineitem"
        assert intent.dimensions == []
        assert intent.metrics == []
        assert intent.filters == []
        assert intent.limit is None
        assert intent.order_by == []


class TestOntologyDeduplicator:
    """Comprehensive unit tests for hierarchical ontology deduplication."""

    @pytest.fixture
    def deduplicator(self) -> OntologyDeduplicator:
        """Fixture providing default TPC-H deduplicator."""
        return OntologyDeduplicator()

    def test_default_tpch_rules_configuration(self) -> None:
        """Verify default TPC-H hierarchy rule definition."""
        assert len(DEFAULT_TPCH_HIERARCHY_RULES) == 1
        rule = DEFAULT_TPCH_HIERARCHY_RULES[0]
        assert rule.parent_table == "region"
        assert rule.parent_column == "r_name"
        assert rule.child_table == "nation"
        assert rule.child_column == "n_name"
        assert rule.child_to_parent["JAPAN"] == "ASIA"
        assert rule.child_to_parent["GERMANY"] == "EUROPE"

    def test_deduplicate_empty_filters(self, deduplicator: OntologyDeduplicator) -> None:
        """Requirement: Empty filter list safety."""
        result = deduplicator.deduplicate([])
        assert result == []

    def test_deduplicate_parent_and_child_removes_parent(
        self, deduplicator: OntologyDeduplicator
    ) -> None:
        """Requirement: When parent (ASIA) and child (JAPAN) are present, parent is removed."""
        parent_filter = FilterSlot(table="region", column="r_name", operator="=", value="ASIA")
        child_filter = FilterSlot(table="nation", column="n_name", operator="=", value="JAPAN")

        # Case 1: Parent before child
        result1 = deduplicator.deduplicate([parent_filter, child_filter])
        assert len(result1) == 1
        assert result1[0] == child_filter

        # Case 2: Child before parent
        result2 = deduplicator.deduplicate([child_filter, parent_filter])
        assert len(result2) == 1
        assert result2[0] == child_filter

    def test_deduplicate_parent_only(self, deduplicator: OntologyDeduplicator) -> None:
        """Requirement: When only parent (ASIA) is present, parent is preserved."""
        parent_filter = FilterSlot(table="region", column="r_name", operator="=", value="ASIA")
        result = deduplicator.deduplicate([parent_filter])
        assert len(result) == 1
        assert result[0] == parent_filter

    def test_deduplicate_child_only(self, deduplicator: OntologyDeduplicator) -> None:
        """Requirement: When only child (JAPAN) is present, child is preserved."""
        child_filter = FilterSlot(table="nation", column="n_name", operator="=", value="JAPAN")
        result = deduplicator.deduplicate([child_filter])
        assert len(result) == 1
        assert result[0] == child_filter

    def test_deduplicate_independent_children_both_kept(
        self, deduplicator: OntologyDeduplicator
    ) -> None:
        """Requirement: Independent children (JAPAN + GERMANY) are both retained."""
        japan_filter = FilterSlot(table="nation", column="n_name", operator="=", value="JAPAN")
        germany_filter = FilterSlot(table="nation", column="n_name", operator="=", value="GERMANY")

        result = deduplicator.deduplicate([japan_filter, germany_filter])
        assert len(result) == 2
        assert result == [japan_filter, germany_filter]

    def test_deduplicate_unrelated_table_filters_preserved(
        self, deduplicator: OntologyDeduplicator
    ) -> None:
        """Requirement: Filters on other tables (e.g. orders.o_orderstatus) do not interfere."""
        order_filter = FilterSlot(table="orders", column="o_orderstatus", operator="=", value="F")
        parent_filter = FilterSlot(table="region", column="r_name", operator="=", value="ASIA")
        child_filter = FilterSlot(table="nation", column="n_name", operator="=", value="JAPAN")
        customer_filter = FilterSlot(
            table="customer", column="c_mktsegment", operator="=", value="BUILDING"
        )

        result = deduplicator.deduplicate(
            [order_filter, parent_filter, child_filter, customer_filter]
        )
        assert len(result) == 3
        assert result == [order_filter, child_filter, customer_filter]

    def test_deduplicate_case_insensitivity(self, deduplicator: OntologyDeduplicator) -> None:
        """Test robustness against case differences in table, column, and values."""
        parent_filter = FilterSlot(table="REGION", column="R_NAME", operator="=", value="asia ")
        child_filter = FilterSlot(table="Nation", column="n_name", operator="=", value="japan")

        result = deduplicator.deduplicate([parent_filter, child_filter])
        assert len(result) == 1
        assert result[0] == child_filter

    def test_deduplicate_multiple_children_single_parent(
        self, deduplicator: OntologyDeduplicator
    ) -> None:
        """Multiple children under same parent (JAPAN, CHINA under ASIA)."""
        parent_filter = FilterSlot(table="region", column="r_name", operator="=", value="ASIA")
        japan_filter = FilterSlot(table="nation", column="n_name", operator="=", value="JAPAN")
        china_filter = FilterSlot(table="nation", column="n_name", operator="=", value="CHINA")

        result = deduplicator.deduplicate([parent_filter, japan_filter, china_filter])
        assert len(result) == 2
        assert result == [japan_filter, china_filter]

    def test_deduplicate_mismatched_parent_not_removed(
        self, deduplicator: OntologyDeduplicator
    ) -> None:
        """When child (JAPAN -> ASIA) and parent (EUROPE) do not match, both are retained."""
        parent_filter = FilterSlot(table="region", column="r_name", operator="=", value="EUROPE")
        child_filter = FilterSlot(table="nation", column="n_name", operator="=", value="JAPAN")

        result = deduplicator.deduplicate([parent_filter, child_filter])
        assert len(result) == 2
        assert result == [parent_filter, child_filter]

    def test_deduplicate_child_with_collection_value(
        self, deduplicator: OntologyDeduplicator
    ) -> None:
        """Child filter with IN operator and list value."""
        parent_filter = FilterSlot(table="region", column="r_name", operator="=", value="EUROPE")
        child_filter = FilterSlot(
            table="nation", column="n_name", operator="IN", value=["FRANCE", "GERMANY"]
        )

        result = deduplicator.deduplicate([parent_filter, child_filter])
        assert len(result) == 1
        assert result[0] == child_filter

    def test_deduplicate_custom_hierarchy_rules(self) -> None:
        """Test with custom domain rules (e.g. prefecture -> region in Japan)."""
        custom_rule = HierarchyRule(
            parent_table="address",
            parent_column="region",
            child_table="address",
            child_column="prefecture",
            child_to_parent={
                "TOKYO": "KANTO",
                "KANAGAWA": "KANTO",
                "OSAKA": "KANSAI",
                "KYOTO": "KANSAI",
            },
        )
        custom_deduplicator = OntologyDeduplicator(rules=[custom_rule])

        parent = FilterSlot(table="address", column="region", operator="=", value="KANTO")
        child = FilterSlot(table="address", column="prefecture", operator="=", value="TOKYO")
        unrelated = FilterSlot(table="address", column="city", operator="=", value="SHINJUKU")

        result = custom_deduplicator.deduplicate([parent, child, unrelated])
        assert result == [child, unrelated]

    def test_deduplicate_structural_rule_without_mapping(self) -> None:
        """Test with structural rule where child presence eliminates parent unconditionally."""
        structural_rule = HierarchyRule(
            parent_table="category",
            parent_column="cat_name",
            child_table="item",
            child_column="item_name",
            child_to_parent={},  # No value mapping
        )
        dedup = OntologyDeduplicator(rules=[structural_rule])

        parent = FilterSlot(table="category", column="cat_name", operator="=", value="ELECTRONICS")
        child = FilterSlot(table="item", column="item_name", operator="=", value="IPHONE")

        result = dedup.deduplicate([parent, child])
        assert result == [child]

    def test_deduplicate_parent_multi_value_and_child_single_removes_parent(
        self, deduplicator: OntologyDeduplicator
    ) -> None:
        """When parent is multi-value (IN ASIA, EUROPE) and child is single, parent is removed."""
        parent_filter = FilterSlot(
            table="region", column="r_name", operator="IN", value=["ASIA", "EUROPE"]
        )
        child_filter = FilterSlot(table="nation", column="n_name", operator="=", value="JAPAN")

        result = deduplicator.deduplicate([parent_filter, child_filter])
        assert len(result) == 1
        assert result[0] == child_filter

    def test_deduplicate_parent_single_and_child_cross_region_keeps_parent(
        self, deduplicator: OntologyDeduplicator
    ) -> None:
        """When parent is single and child spans multiple regions, parent is NOT removed."""
        parent_filter = FilterSlot(table="region", column="r_name", operator="=", value="ASIA")
        child_filter = FilterSlot(
            table="nation", column="n_name", operator="IN", value=["JAPAN", "FRANCE"]
        )

        result = deduplicator.deduplicate([parent_filter, child_filter])
        assert len(result) == 2
        assert result == [parent_filter, child_filter]

    def test_deduplicate_parent_negation_operator_preserved(
        self, deduplicator: OntologyDeduplicator
    ) -> None:
        """When parent has negation operator (region != 'ASIA'), it is guarded from elimination."""
        parent_filter = FilterSlot(table="region", column="r_name", operator="!=", value="ASIA")
        child_filter = FilterSlot(table="nation", column="n_name", operator="=", value="JAPAN")

        result = deduplicator.deduplicate([parent_filter, child_filter])
        assert len(result) == 2
        assert result == [parent_filter, child_filter]

    def test_deduplicate_child_negation_operator_does_not_subsume_parent(
        self, deduplicator: OntologyDeduplicator
    ) -> None:
        """When child has negation operator (nation != 'JAPAN'), it cannot subsume parent."""
        parent_filter = FilterSlot(table="region", column="r_name", operator="=", value="ASIA")
        child_filter = FilterSlot(table="nation", column="n_name", operator="!=", value="JAPAN")

        result = deduplicator.deduplicate([parent_filter, child_filter])
        assert len(result) == 2
        assert result == [parent_filter, child_filter]

    def test_deduplicate_parent_multi_value_and_child_multi_within_parents_removes_parent(
        self, deduplicator: OntologyDeduplicator
    ) -> None:
        """When all child values are contained in parent values, parent is removed."""
        parent_filter = FilterSlot(
            table="region", column="r_name", operator="IN", value=["ASIA", "EUROPE"]
        )
        child_filter = FilterSlot(
            table="nation", column="n_name", operator="IN", value=["JAPAN", "GERMANY"]
        )

        result = deduplicator.deduplicate([parent_filter, child_filter])
        assert len(result) == 1
        assert result[0] == child_filter

    def test_deduplicate_parent_multi_value_and_child_cross_outside_keeps_parent(
        self, deduplicator: OntologyDeduplicator
    ) -> None:
        """When child values contain an entity outside the parent filter, parent is kept."""
        parent_filter = FilterSlot(
            table="region", column="r_name", operator="IN", value=["ASIA", "AMERICA"]
        )
        child_filter = FilterSlot(
            table="nation", column="n_name", operator="IN", value=["JAPAN", "FRANCE"]
        )

        result = deduplicator.deduplicate([parent_filter, child_filter])
        assert len(result) == 2
        assert result == [parent_filter, child_filter]

    def test_deduplicate_preserves_order_and_immutability(
        self, deduplicator: OntologyDeduplicator
    ) -> None:
        """Test that original list is not modified and order of remaining elements is preserved."""
        f1 = FilterSlot(table="orders", column="o_orderstatus", operator="=", value="O")
        f2 = FilterSlot(table="region", column="r_name", operator="=", value="ASIA")
        f3 = FilterSlot(table="customer", column="c_acctbal", operator=">", value=1000)
        f4 = FilterSlot(table="nation", column="n_name", operator="=", value="JAPAN")
        f5 = FilterSlot(table="part", column="p_size", operator="=", value=15)

        original_list = [f1, f2, f3, f4, f5]
        original_copy = list(original_list)

        result = deduplicator.deduplicate(original_list)

        # Original list was not modified
        assert original_list == original_copy
        # f2 (region=ASIA) was removed, rest retained in exact same relative order
        assert result == [f1, f3, f4, f5]
