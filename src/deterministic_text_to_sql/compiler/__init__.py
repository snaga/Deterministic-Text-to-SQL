"""Compiler package for SQL AST generation and ontology deduplication."""

from deterministic_text_to_sql.compiler.builder import GenericSQLBuilder
from deterministic_text_to_sql.compiler.deduplicator import (
    DEFAULT_TPCH_HIERARCHY_RULES,
    EQUALITY_OPERATORS,
    TPCH_NATION_TO_REGION,
    HierarchyRule,
    OntologyDeduplicator,
)
from deterministic_text_to_sql.compiler.exceptions import CompilerError, SQLBuildError
from deterministic_text_to_sql.compiler.models import (
    FilterSlot,
    HavingSlot,
    MetricSlot,
    OrderBySlot,
    QueryIntent,
    WindowSlot,
)

__all__ = [
    "DEFAULT_TPCH_HIERARCHY_RULES",
    "EQUALITY_OPERATORS",
    "TPCH_NATION_TO_REGION",
    "CompilerError",
    "FilterSlot",
    "GenericSQLBuilder",
    "HavingSlot",
    "HierarchyRule",
    "MetricSlot",
    "OntologyDeduplicator",
    "OrderBySlot",
    "QueryIntent",
    "SQLBuildError",
    "WindowSlot",
]
