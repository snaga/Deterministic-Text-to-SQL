"""Deterministic Text-to-SQL Engine.

TypeSafe AI Jev と型安全 SQL AST ビルダー (sqlglot) を連携させた、
高信頼・決定論的 Text-to-SQL エンジン。
"""

from deterministic_text_to_sql.engine import DeterministicTextToSQLEngine
from deterministic_text_to_sql.models import (
    EngineResult,
    EngineStatus,
    LatencyMetrics,
)

__version__ = "0.1.0"

__all__ = [
    "DeterministicTextToSQLEngine",
    "EngineResult",
    "EngineStatus",
    "LatencyMetrics",
    "__version__",
]
