"""Human-in-the-Loop (HITL) ambiguity management and slash syntax parsing."""

from deterministic_text_to_sql.hitl.feedback import HITLManager
from deterministic_text_to_sql.hitl.models import HITLIssue, HITLOption, ParsedQueryInput

__all__ = [
    "HITLManager",
    "HITLIssue",
    "HITLOption",
    "ParsedQueryInput",
]
