"""Semantic metadata definition and schema loader."""

from deterministic_text_to_sql.schema.exceptions import (
    SchemaError,
    SchemaValidationError,
)
from deterministic_text_to_sql.schema.loader import (
    SchemaLoader,
    load_schema,
    load_schema_from_dict,
    load_schema_from_json,
    load_schema_from_yaml,
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
from deterministic_text_to_sql.schema.ossie import OssieSchemaAdapter

__all__ = [
    "ColumnMetadata",
    "ColumnRole",
    "ForeignKeyMetadata",
    "HierarchyRule",
    "MetricDefinition",
    "OssieSchemaAdapter",
    "SchemaError",
    "SchemaLoader",
    "SchemaMetadata",
    "SchemaValidationError",
    "TableMetadata",
    "load_schema",
    "load_schema_from_dict",
    "load_schema_from_json",
    "load_schema_from_yaml",
]
