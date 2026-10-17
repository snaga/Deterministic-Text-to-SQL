"""Schema-related exceptions for deterministic-text-to-sql."""


class SchemaError(Exception):
    """Base exception for all schema-related errors."""


class SchemaValidationError(SchemaError):
    """Raised when schema validation fails or schema file cannot be parsed."""
