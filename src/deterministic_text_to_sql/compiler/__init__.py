"""Compiler package for SQL AST generation and ontology deduplication."""

from deterministic_text_to_sql.compiler.deduplicator import (
    DEFAULT_TPCH_HIERARCHY_RULES,
    EQUALITY_OPERATORS,
    TPCH_NATION_TO_REGION,
    HierarchyRule,
    OntologyDeduplicator,
)
from deterministic_text_to_sql.compiler.models import FilterSlot, QueryIntent

__all__ = [
    "DEFAULT_TPCH_HIERARCHY_RULES",
    "EQUALITY_OPERATORS",
    "TPCH_NATION_TO_REGION",
    "FilterSlot",
    "HierarchyRule",
    "OntologyDeduplicator",
    "QueryIntent",
]
