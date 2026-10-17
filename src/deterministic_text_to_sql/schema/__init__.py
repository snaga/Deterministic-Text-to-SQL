"""Semantic metadata definition and schema loader."""

from deterministic_text_to_sql.schema.exceptions import (
    SchemaError,
    SchemaValidationError,
)
from deterministic_text_to_sql.schema.loader import (
    SchemaLoader,
    load_schema_from_dict,
    load_schema_from_json,
)
from deterministic_text_to_sql.schema.models import (
    ColumnMetadata,
    ColumnRole,
    ForeignKeyMetadata,
    HierarchyRule,
    MetricDefinition,
    SchemaMetadata,
    TableMetadata,
)

__all__ = [
    "ColumnMetadata",
    "ColumnRole",
    "ForeignKeyMetadata",
    "HierarchyRule",
    "MetricDefinition",
    "SchemaError",
    "SchemaLoader",
    "SchemaMetadata",
    "SchemaValidationError",
    "TableMetadata",
    "load_schema_from_dict",
    "load_schema_from_json",
]
