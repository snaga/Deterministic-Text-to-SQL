"""Ontology deduplicator for eliminating redundant hierarchical filter conditions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from deterministic_text_to_sql.compiler.models import FilterSlot
from deterministic_text_to_sql.schema.models import HierarchyRule, SchemaMetadata

__all__ = [
    "DEFAULT_TPCH_HIERARCHY_RULES",
    "EQUALITY_OPERATORS",
    "TPCH_NATION_TO_REGION",
    "HierarchyRule",
    "OntologyDeduplicator",
]

# Standard TPC-H 25 nations to 5 regions mapping
TPCH_NATION_TO_REGION: dict[str, str] = {
    # AFRICA
    "ALGERIA": "AFRICA",
    "ETHIOPIA": "AFRICA",
    "KENYA": "AFRICA",
    "MOROCCO": "AFRICA",
    "MOZAMBIQUE": "AFRICA",
    # AMERICA
    "ARGENTINA": "AMERICA",
    "BRAZIL": "AMERICA",
    "CANADA": "AMERICA",
    "PERU": "AMERICA",
    "UNITED STATES": "AMERICA",
    # ASIA
    "CHINA": "ASIA",
    "INDIA": "ASIA",
    "INDONESIA": "ASIA",
    "JAPAN": "ASIA",
    "VIETNAM": "ASIA",
    # EUROPE
    "FRANCE": "EUROPE",
    "GERMANY": "EUROPE",
    "ROMANIA": "EUROPE",
    "RUSSIA": "EUROPE",
    "UNITED KINGDOM": "EUROPE",
    # MIDDLE EAST
    "EGYPT": "MIDDLE EAST",
    "IRAN": "MIDDLE EAST",
    "IRAQ": "MIDDLE EAST",
    "JORDAN": "MIDDLE EAST",
    "SAUDI ARABIA": "MIDDLE EAST",
}

EQUALITY_OPERATORS: frozenset[str] = frozenset({"=", "==", "IN"})


def _is_equality_operator(op: str) -> bool:
    """Check whether the given SQL operator is an equality comparison operator.

    Args:
        op: SQL operator string (e.g. '=', '==', 'IN', '!=', 'NOT IN').

    Returns:
        True if the operator represents positive equality membership, False otherwise.
    """
    return op.strip().upper() in EQUALITY_OPERATORS


DEFAULT_TPCH_HIERARCHY_RULES: list[HierarchyRule] = [
    HierarchyRule(
        parent_table="region",
        parent_column="r_name",
        child_table="nation",
        child_column="n_name",
        child_to_parent=TPCH_NATION_TO_REGION,
    ),
]


def _extract_string_values(val: Any) -> list[str]:
    """Helper to extract normalized string values from scalar or collection values.

    Args:
        val: Filter value (scalar string, number, or iterable collection).

    Returns:
        List of trimmed uppercase string representations.
    """
    if isinstance(val, (list, tuple, set)):
        return [str(v).strip().upper() for v in val]
    return [str(val).strip().upper()]


class OntologyDeduplicator:
    """Deduplicator that safely eliminates redundant parent filter conditions

    when child filter conditions strictly subsume them.

    Mathematical Basis:
        In Boolean logic and relational calculus, if predicate C implies predicate P
        (i.e. C => P, meaning every tuple satisfying C also satisfies P), then
        (C AND P) <=> C. Retaining both C and P in the WHERE clause is redundant and
        can degrade query execution plans.

        For equality filters (e.g. '=', 'IN'):
            If implied_for_child ⊆ parent_vals, then C => P holds, and the parent filter P
            is redundant and safely eliminated.
            If implied_for_child ⊄ parent_vals (e.g. child spans multiple parents or
            does not fall entirely within parent_vals), C does not imply P, so P is kept.
            Filters with negation or inequality operators ('!=', 'NOT IN', '<', etc.) are
            never eliminated as positive subsumption does not hold.
    """

    def __init__(self, rules: Sequence[HierarchyRule] | None = None) -> None:
        """Initialize OntologyDeduplicator with hierarchical ontology rules.

        Args:
            rules: List of HierarchyRule definitions. Defaults to standard TPC-H rules.
        """
        raw_rules = rules if rules is not None else DEFAULT_TPCH_HIERARCHY_RULES

        # Pre-normalize rules for case-insensitive lookup
        self._rules: list[HierarchyRule] = []
        for r in raw_rules:
            normalized_mapping = {
                k.strip().upper(): v.strip().upper() for k, v in r.child_to_parent.items()
            }
            self._rules.append(
                HierarchyRule(
                    parent_table=r.parent_table.strip().lower(),
                    parent_column=r.parent_column.strip().lower(),
                    child_table=r.child_table.strip().lower(),
                    child_column=r.child_column.strip().lower(),
                    child_to_parent=normalized_mapping,
                )
            )

    @classmethod
    def from_schema(cls, schema: SchemaMetadata) -> OntologyDeduplicator:
        """Create an OntologyDeduplicator configured from schema metadata.

        If the schema defines explicit hierarchy rules (schema.hierarchies),
        those rules are used. If schema.hierarchies is empty, it falls back
        to DEFAULT_TPCH_HIERARCHY_RULES when the schema contains TPC-H
        'nation' and 'region' tables; otherwise it initializes with an empty rule list.

        Args:
            schema: SchemaMetadata containing table and ontology definitions.

        Returns:
            Configured OntologyDeduplicator instance.
        """
        if schema.hierarchies:
            return cls(rules=schema.hierarchies)

        has_tpch_geo = (
            schema.get_table("nation") is not None
            and schema.get_table("region") is not None
        )
        if has_tpch_geo:
            return cls(rules=DEFAULT_TPCH_HIERARCHY_RULES)

        return cls(rules=[])

    @property
    def rules(self) -> list[HierarchyRule]:
        """Return the list of normalized hierarchy rules configured in this deduplicator."""
        return list(self._rules)

    def deduplicate(self, filters: Sequence[FilterSlot]) -> list[FilterSlot]:
        """Eliminate redundant parent filters if subsumed by present child filters.

        This operation is pure and deterministic:
        - Parent + Child (e.g. ASIA and JAPAN): Parent eliminated, Child kept.
        - Multi-value parent + Child (e.g. [ASIA, EUROPE] and JAPAN): Parent eliminated.
        - Parent + Cross-region Child (e.g. ASIA and [JAPAN, FRANCE]): Parent NOT eliminated.
        - Negation operators (e.g. !=, NOT IN): Skipped from elimination.
        - Parent only (ASIA): Kept as-is.
        - Child only (JAPAN): Kept as-is.
        - Independent children (JAPAN and GERMANY): Both kept.
        - Mismatched parent + child (EUROPE and JAPAN): Both kept (no subsumption).
        - Unrelated filters (e.g. orders.o_orderstatus): Kept without interference.
        - Output preserves the relative ordering of retained filters.

        Args:
            filters: List of FilterSlot instances.

        Returns:
            New list of FilterSlot instances with redundant parent filters removed.
        """
        if not filters:
            return []

        if not self._rules:
            return list(filters)

        redundant_indices: set[int] = set()

        for rule in self._rules:
            # 1. Collect all child filters matching this rule with equality operators
            child_filters = [
                f
                for f in filters
                if f.table.strip().lower() == rule.child_table
                and f.column.strip().lower() == rule.child_column
                and _is_equality_operator(f.operator)
            ]

            if not child_filters:
                # No eligible child filter present; parent filters cannot be subsumed
                continue

            # 2. Check each parent filter matching this rule
            for idx, f in enumerate(filters):
                if idx in redundant_indices:
                    continue

                if not (
                    f.table.strip().lower() == rule.parent_table
                    and f.column.strip().lower() == rule.parent_column
                ):
                    continue

                # Guard: only equality operators on the parent are eligible for subsumption
                if not _is_equality_operator(f.operator):
                    continue

                if not rule.child_to_parent:
                    # Structural rule without discrete mapping: child presence eliminates parent
                    redundant_indices.add(idx)
                    continue

                parent_vals = set(_extract_string_values(f.value))
                if not parent_vals:
                    continue

                # Check if any child filter strictly subsumes this parent filter (C => P)
                # Mathematical subsumption: every parent value implied by child filter
                # must be contained within the parent filter's allowed values.
                for cf in child_filters:
                    child_vals = _extract_string_values(cf.value)
                    if not child_vals:
                        continue
                    # All values in child filter must be mapped in the ontology
                    if not all(cv in rule.child_to_parent for cv in child_vals):
                        continue
                    implied_for_child = {rule.child_to_parent[cv] for cv in child_vals}
                    if implied_for_child and implied_for_child.issubset(parent_vals):
                        redundant_indices.add(idx)
                        break

        # Build output list preserving original sequence order
        return [f for idx, f in enumerate(filters) if idx not in redundant_indices]
