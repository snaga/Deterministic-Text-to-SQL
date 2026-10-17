"""Top-level data models and data contracts for the Deterministic Text-to-SQL Engine."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict

from deterministic_text_to_sql.compiler.models import QueryIntent
from deterministic_text_to_sql.hitl.models import HITLIssue


class EngineStatus(str, Enum):
    """Execution status returned by DeterministicTextToSQLEngine."""

    SUCCESS = "success"
    HITL_REQUIRED = "hitl_required"
    ERROR = "error"


class LatencyMetrics(BaseModel):
    """Fine-grained latency metrics in milliseconds.

    Attributes:
        jev_inference: Jev System 1 slot extraction latency in ms.
        ast_build: Semantic graph resolution and AST compilation latency in ms.
        total: End-to-end processing latency in ms.
    """

    model_config = ConfigDict(frozen=True)

    jev_inference: float = 0.0
    ast_build: float = 0.0
    total: float = 0.0


class EngineResult(BaseModel):
    """Comprehensive structured result returned by DeterministicTextToSQLEngine.

    Serves as the strict data contract between the core engine and higher-level
    presentation layers (Typer CLI, interactive REPL, and future MCP tool handlers).

    Attributes:
        status: Execution status (success, hitl_required, or error).
        query: Base natural language query processed by the engine.
        clarification: Optional disambiguation token extracted from query or arguments.
        dialect: Target SQL dialect used for transpilation.
        sql: Generated deterministic SQL query string if status is SUCCESS.
        hitl_issue: Ambiguity issue details if status is HITL_REQUIRED.
        hitl_summary: Normalized slots and classification summary for agent inspection.
        intent: Compiled QueryIntent representing semantic components.
        latency_ms: Fine-grained latency breakdown in milliseconds.
        error_message: Error description string if status is ERROR.
    """

    model_config = ConfigDict(frozen=True)

    status: EngineStatus
    query: str
    clarification: str | None = None
    dialect: str
    sql: str | None = None
    hitl_issue: HITLIssue | None = None
    hitl_summary: dict[str, Any] | None = None
    intent: QueryIntent | None = None
    latency_ms: LatencyMetrics
    error_message: str | None = None
