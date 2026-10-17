"""Exceptions for SQL compilation and AST generation."""


class CompilerError(Exception):
    """Base exception for all compilation-related errors."""


class SQLBuildError(CompilerError):
    """Raised when SQL AST generation or dialect transpilation fails."""
