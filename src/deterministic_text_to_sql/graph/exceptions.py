"""Exceptions for graph-based schema relationships and path resolving."""


class GraphError(Exception):
    """Base exception for schema graph and join resolution errors."""


class UnconnectedSchemaError(GraphError):
    """Exception raised when no join path exists between requested tables."""
