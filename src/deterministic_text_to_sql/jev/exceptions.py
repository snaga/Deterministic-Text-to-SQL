"""Exceptions for TypeSafe Jev API integration and slot parsing."""


class JevError(Exception):
    """Base exception for all Jev-related errors."""


class JevAPIConnectionError(JevError):
    """Raised when communication with Jev API fails (timeout, network drop, 5xx, etc.)."""


class JevResponseParseError(JevError):
    """Raised when Jev API response is malformed or cannot be parsed."""
