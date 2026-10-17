"""Unit tests for DeterministicTextToSQLEngine.

Detroit-style testing with MockTransport and real TPC-H schema.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlglot

from deterministic_text_to_sql import (
    DeterministicTextToSQLEngine,
    EngineResult,
    EngineStatus,
    LatencyMetrics,
)
from deterministic_text_to_sql.jev.client import JevClient
from deterministic_text_to_sql.schema.exceptions import SchemaValidationError
from deterministic_text_to_sql.schema.loader import load_schema_from_json
from deterministic_text_to_sql.schema.models import SchemaMetadata

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "tpch_schema.json"


@pytest.fixture
def tpch_schema() -> SchemaMetadata:
    """Fixture providing loaded TPC-H SchemaMetadata."""
    return load_schema_from_json(FIXTURE_PATH)


def create_mock_jev_client(mock_responses: dict[str, dict[str, Any]] | None = None) -> JevClient:
    """Create a JevClient backed by an in-memory httpx MockTransport."""
    default_response = {
        "results": {
            "primary_table": "customer",
            "secondary_table": "orders",
            "dimension": "c_name",
            "metric": "sum_revenue",
            "ranking": "top_5",
            "ambiguity": "clear",
            "filters": ["ASIA"],
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        eval_state = body.get("eval_state", "")

        if mock_responses:
            for key, resp in mock_responses.items():
                if key in eval_state:
                    return httpx.Response(200, json=resp)

        return httpx.Response(200, json=default_response)

    client = JevClient(api_key="ts_test_key_xyz")
    client._client = httpx.Client(
        base_url="https://api.typesafe.ai",
        transport=httpx.MockTransport(handler),
    )
    return client


class TestEngineInitialization:
    """Tests for DeterministicTextToSQLEngine initialization and lifecycle management."""

    def test_init_with_schema_metadata(self, tpch_schema: SchemaMetadata) -> None:
        client = create_mock_jev_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)
        assert engine.schema.title == tpch_schema.title
        assert len(engine.schema.tables) == len(tpch_schema.tables)
        assert engine.schema_graph is not None

    def test_init_with_path(self) -> None:
        client = create_mock_jev_client()
        engine = DeterministicTextToSQLEngine(schema=FIXTURE_PATH, jev_client=client)
        assert len(engine.schema.tables) == 8

    def test_init_with_str_path(self) -> None:
        client = create_mock_jev_client()
        engine = DeterministicTextToSQLEngine(schema=str(FIXTURE_PATH), jev_client=client)
        assert len(engine.schema.tables) == 8

    def test_init_with_dict(self, tpch_schema: SchemaMetadata) -> None:
        client = create_mock_jev_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_schema.model_dump(), jev_client=client)
        assert len(engine.schema.tables) == 8

    def test_init_with_invalid_path_raises(self) -> None:
        client = create_mock_jev_client()
        with pytest.raises(SchemaValidationError):
            DeterministicTextToSQLEngine(
                schema=Path("non_existent_schema_file.json"), jev_client=client
            )

    def test_context_manager_lifecycle(self, tpch_schema: SchemaMetadata) -> None:
        with DeterministicTextToSQLEngine(
            schema=tpch_schema, api_key="dummy_key"
        ) as engine:
            assert engine is not None
            assert engine._owns_client is True


class TestEngineProcessQuerySuccess:
    """Tests for successful end-to-end query processing and SQL generation."""

    def test_normal_query_success_e2e(self, tpch_schema: SchemaMetadata) -> None:
        mock_resp = {
            "アジアの顧客の売上トップ5": {
                "results": {
                    "primary_table": "customer",
                    "secondary_table": "orders",
                    "dimension": "c_name",
                    "metric": "sum_revenue",
                    "ranking": "top_5",
                    "ambiguity": "clear",
                    "filters": ["ASIA"],
                }
            }
        }
        client = create_mock_jev_client(mock_resp)
        engine = DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)

        result = engine.process_query("アジアの顧客の売上トップ5", dialect="postgres")

        assert result.status == EngineStatus.SUCCESS
        assert result.query == "アジアの顧客の売上トップ5"
        assert result.clarification is None
        assert result.dialect == "postgres"
        assert result.sql is not None
        assert result.error_message is None
        assert result.hitl_issue is None
        assert result.intent is not None
        assert result.intent.primary_table == "customer"
        assert result.intent.limit == 5

        # Latency metrics verification
        assert isinstance(result.latency_ms, LatencyMetrics)
        assert result.latency_ms.jev_inference >= 0.0
        assert result.latency_ms.ast_build >= 0.0
        assert result.latency_ms.total >= result.latency_ms.ast_build

        # SQL syntactic validity and semantic nodes verification
        parsed = sqlglot.parse_one(result.sql, read="postgres")
        assert parsed is not None
        assert "customer" in result.sql
        assert "orders" in result.sql
        assert "nation" in result.sql
        assert "region" in result.sql
        assert "total_revenue" in result.sql
        assert "LIMIT 5" in result.sql
        assert "r_name = 'ASIA'" in result.sql

    def test_multiple_dialects_compilation(self, tpch_schema: SchemaMetadata) -> None:
        client = create_mock_jev_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)

        dialects = ["duckdb", "sqlite", "snowflake", "postgres"]
        for dia in dialects:
            res = engine.process_query("アジアの顧客の売上トップ5", dialect=dia)
            assert res.status == EngineStatus.SUCCESS
            assert res.dialect == dia
            assert res.sql is not None
            parsed = sqlglot.parse_one(res.sql, read=dia)
            assert parsed is not None


class TestEngineHITLIntervention:
    """Tests for Human-in-the-Loop ambiguity detection and resolution."""

    def test_ambiguous_query_requires_hitl(self, tpch_schema: SchemaMetadata) -> None:
        mock_resp = {
            "売上トップ5": {
                "results": {
                    "primary_table": "orders",
                    "secondary_table": "lineitem",
                    "dimension": "none",
                    "metric": "sum_revenue",
                    "ranking": "top_5",
                    "ambiguity": "ambiguous_revenue_definition",
                    "needs_clarification": True,
                }
            }
        }
        client = create_mock_jev_client(mock_resp)
        engine = DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)

        result = engine.process_query("売上トップ5")

        assert result.status == EngineStatus.HITL_REQUIRED
        assert result.sql is None
        assert result.hitl_issue is not None
        assert "売上の集計基準の確認" in result.hitl_issue.title
        assert len(result.hitl_issue.options) == 2
        assert result.hitl_issue.options[0].key == "A"
        assert result.hitl_issue.options[1].key == "B"
        assert result.hitl_summary is not None
        assert result.latency_ms.jev_inference >= 0.0

    def test_slash_syntax_resolves_ambiguity(self, tpch_schema: SchemaMetadata) -> None:
        mock_resp = {
            "lineitem 商品明細売上": {
                "results": {
                    "primary_table": "lineitem",
                    "secondary_table": "none",
                    "dimension": "none",
                    "metric": "sum_revenue",
                    "ranking": "top_5",
                    "ambiguity": "clear",
                }
            }
        }
        client = create_mock_jev_client(mock_resp)
        engine = DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)

        result = engine.process_query("売上トップ5 / Aで")

        assert result.status == EngineStatus.SUCCESS
        assert result.query == "売上トップ5"
        assert result.clarification == "Aで"
        assert result.sql is not None
        assert "lineitem" in result.sql
        assert result.hitl_issue is None

    def test_explicit_clarify_argument_resolves_ambiguity(
        self, tpch_schema: SchemaMetadata
    ) -> None:
        mock_resp = {
            "orders 注文伝票売上": {
                "results": {
                    "primary_table": "orders",
                    "secondary_table": "none",
                    "dimension": "none",
                    "metric": "sum_revenue",
                    "ranking": "top_5",
                    "ambiguity": "clear",
                }
            }
        }
        client = create_mock_jev_client(mock_resp)
        engine = DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)

        result = engine.process_query("売上トップ5", clarify="B")

        assert result.status == EngineStatus.SUCCESS
        assert result.clarification == "B"
        assert result.sql is not None
        assert "orders" in result.sql

    def test_force_bypasses_hitl(self, tpch_schema: SchemaMetadata) -> None:
        mock_resp = {
            "売上トップ5": {
                "results": {
                    "primary_table": "orders",
                    "secondary_table": "lineitem",
                    "dimension": "none",
                    "metric": "sum_revenue",
                    "ranking": "top_5",
                    "ambiguity": "ambiguous_revenue_definition",
                    "needs_clarification": True,
                }
            }
        }
        client = create_mock_jev_client(mock_resp)
        engine = DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)

        result = engine.process_query("売上トップ5", force=True)

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None
        assert result.hitl_issue is None
        assert "orders" in result.sql


class TestEngineGetSchemaInfo:
    """Tests for get_schema_info agent inspection API."""

    def test_get_schema_info_structure(self, tpch_schema: SchemaMetadata) -> None:
        client = create_mock_jev_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)

        info = engine.get_schema_info()

        assert "title" in info
        assert "description" in info
        assert "table_names" in info
        assert "tables" in info

        assert len(info["table_names"]) == 8
        assert "customer" in info["table_names"]
        assert "orders" in info["table_names"]
        assert "lineitem" in info["table_names"]

        customer_info = next(t for t in info["tables"] if t["name"] == "customer")
        assert customer_info["primary_key"] == ["c_custkey"]
        assert any(c["name"] == "c_name" for c in customer_info["columns"])
        assert any(c["role"] == "metric" for c in customer_info["columns"])

        region_info = next(t for t in info["tables"] if t["name"] == "region")
        r_name_col = next(c for c in region_info["columns"] if c["name"] == "r_name")
        assert "ASIA" in r_name_col["values"]


class TestEngineErrorHandling:
    """Tests for graceful error handling returning EngineStatus.ERROR."""

    def test_empty_query_returns_error(self, tpch_schema: SchemaMetadata) -> None:
        client = create_mock_jev_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)

        result = engine.process_query("   ")

        assert result.status == EngineStatus.ERROR
        assert result.sql is None
        assert result.error_message is not None
        assert "empty" in result.error_message.lower()

    def test_jev_connection_error_returns_error_result(
        self, tpch_schema: SchemaMetadata
    ) -> None:
        def error_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="Internal Jev Server Error")

        client = JevClient(api_key="key", max_retries=0)
        client._client = httpx.Client(
            base_url="https://api.typesafe.ai",
            transport=httpx.MockTransport(error_handler),
        )

        engine = DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)
        result = engine.process_query("顧客の売上")

        assert isinstance(result, EngineResult)
        assert result.status == EngineStatus.ERROR
        assert result.sql is None
        assert result.error_message is not None
        assert "500" in result.error_message or "Server error" in result.error_message

    def test_unsupported_dialect_returns_error_result(
        self, tpch_schema: SchemaMetadata
    ) -> None:
        client = create_mock_jev_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)

        result = engine.process_query(
            "アジアの顧客の売上トップ5", dialect="unknown_unsupported_dialect"
        )

        assert isinstance(result, EngineResult)
        assert result.status == EngineStatus.ERROR
        assert result.sql is None
        assert result.error_message is not None
        assert "dialect" in result.error_message.lower()

    def test_malformed_jev_response_returns_error_result(
        self, tpch_schema: SchemaMetadata
    ) -> None:
        def malformed_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="Not a valid JSON response {{{")

        client = JevClient(api_key="key", max_retries=0)
        client._client = httpx.Client(
            base_url="https://api.typesafe.ai",
            transport=httpx.MockTransport(malformed_handler),
        )

        engine = DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)
        result = engine.process_query("顧客一覧")

        assert isinstance(result, EngineResult)
        assert result.status == EngineStatus.ERROR
        assert result.sql is None
        assert result.error_message is not None

    def test_context_manager_lifecycle(self, tpch_schema: SchemaMetadata) -> None:
        """コンテキストマネージャが所有するクライアントを確実にクローズすることを検証。"""
        with DeterministicTextToSQLEngine(
            schema=tpch_schema, api_key="dummy_key"
        ) as engine:
            assert engine is not None
            assert engine._owns_client is True
            assert engine._jev_client._client.is_closed is False
        assert engine._jev_client._client.is_closed is True


