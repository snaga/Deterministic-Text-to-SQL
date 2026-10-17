"""Live integration tests against the actual TypeSafe Jev System 1 API.

These tests execute actual HTTP requests to https://api.typesafe.ai/v1/systemone
using the TYPESAFE_API_KEY environment variable.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlglot

from deterministic_text_to_sql import (
    DeterministicTextToSQLEngine,
    EngineStatus,
)
from deterministic_text_to_sql.jev.client import JevClient
from deterministic_text_to_sql.schema.loader import load_schema_from_json
from deterministic_text_to_sql.schema.models import SchemaMetadata

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "tpch_schema.json"


@pytest.fixture
def tpch_schema() -> SchemaMetadata:
    """Fixture providing loaded TPC-H SchemaMetadata."""
    return load_schema_from_json(FIXTURE_PATH)


@pytest.fixture
def live_engine(tpch_schema: SchemaMetadata) -> DeterministicTextToSQLEngine:
    """Fixture providing an engine configured with live JevClient."""
    api_key = os.environ.get("TYPESAFE_API_KEY")
    client = JevClient(api_key=api_key)
    return DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)


@pytest.mark.skipif(
    not os.environ.get("TYPESAFE_API_KEY"),
    reason="TYPESAFE_API_KEY environment variable is not set",
)
class TestLiveJevAPIIntegration:
    """Integration tests connecting to the real TypeSafe Jev System 1 API."""

    def test_live_japan_customer_sales_top_5(
        self, live_engine: DeterministicTextToSQLEngine
    ) -> None:
        """Test query: '日本の顧客の売上を顧客別に集計して、トップ5を教えて'."""
        query = "日本の顧客の売上を顧客別に集計して、トップ5を教えて"
        result = live_engine.process_query(query)

        # Must not fail with 400 client error
        assert result.status != EngineStatus.ERROR, f"API Error: {result.error_message}"

        # Either resolved directly or triggered HITL for revenue disambiguation
        if result.status == EngineStatus.HITL_REQUIRED:
            assert result.hitl_issue is not None
            # Clarify with Option A (lineitem)
            result = live_engine.process_query(query, clarification="A")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None

        # Verify SQL syntax validity
        parsed = sqlglot.parse_one(result.sql)
        assert parsed is not None

        # Verify key query components
        sql_upper = result.sql.upper()
        assert "CUSTOMER" in sql_upper
        assert "NATION" in sql_upper
        assert "JAPAN" in sql_upper
        assert "SUM(" in sql_upper
        assert "LIMIT 5" in sql_upper

    def test_live_sales_top_5_hitl_and_resolution(
        self, live_engine: DeterministicTextToSQLEngine
    ) -> None:
        """Test query: '売上トップ5を集計して' triggers HITL and resolves cleanly."""
        query = "売上トップ5を集計して"
        result = live_engine.process_query(query)

        assert result.status != EngineStatus.ERROR, f"API Error: {result.error_message}"

        if result.status == EngineStatus.HITL_REQUIRED:
            assert result.hitl_issue is not None
            assert len(result.hitl_issue.options) >= 2

            # Clarify with Option A
            result_clarified = live_engine.process_query(query, clarification="A")
            assert result_clarified.status == EngineStatus.SUCCESS
            assert result_clarified.sql is not None

            parsed = sqlglot.parse_one(result_clarified.sql)
            assert parsed is not None
            assert "LIMIT 5" in result_clarified.sql.upper()
        else:
            # If directly resolved without clarification
            assert result.status == EngineStatus.SUCCESS
            assert result.sql is not None
            parsed = sqlglot.parse_one(result.sql)
            assert parsed is not None

    def test_live_numeric_filter(
        self, live_engine: DeterministicTextToSQLEngine
    ) -> None:
        """Test query with numeric inequality: '口座残高が1000以上の顧客'."""
        query = "口座残高が1000以上の顧客"
        result = live_engine.process_query(query)

        assert result.status != EngineStatus.ERROR, f"API Error: {result.error_message}"

        if result.status == EngineStatus.HITL_REQUIRED:
            result = live_engine.process_query(query, clarification="A")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None

        parsed = sqlglot.parse_one(result.sql)
        assert parsed is not None

        sql_upper = result.sql.upper()
        assert "CUSTOMER" in sql_upper
        assert "C_ACCTBAL" in sql_upper
        assert "1000" in result.sql
