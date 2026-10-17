"""Unit tests for TypeSafe Jev API client and slot extraction (London School with mocks)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from deterministic_text_to_sql.jev.client import JevClient
from deterministic_text_to_sql.jev.exceptions import (
    JevAPIConnectionError,
    JevResponseParseError,
)
from deterministic_text_to_sql.jev.models import ParsedSlots
from deterministic_text_to_sql.jev.prompts import build_jev_payload
from deterministic_text_to_sql.jev.slots import (
    extract_numeric_literals,
    parse_jev_response,
)
from deterministic_text_to_sql.schema.loader import load_schema_from_json
from deterministic_text_to_sql.schema.models import SchemaMetadata


@pytest.fixture
def tpch_schema() -> SchemaMetadata:
    """Load standard TPC-H schema fixture."""
    fixture_path = Path(__file__).parent.parent / "fixtures" / "tpch_schema.json"
    return load_schema_from_json(fixture_path)


class TestExtractNumericLiterals:
    """Tests for regex-based numeric extraction from natural language queries."""

    def test_positive_integers(self) -> None:
        text = "売上が 1000 以上の顧客"
        nums = extract_numeric_literals(text)
        assert nums == [1000.0]

    def test_negative_numbers(self) -> None:
        text = "口座残高が -500 以下の仕入先"
        nums = extract_numeric_literals(text)
        assert nums == [-500.0]

    def test_floating_point_numbers(self) -> None:
        text = "割引率が 0.05 以上の明細"
        nums = extract_numeric_literals(text)
        assert nums == [0.05]

    def test_comma_formatted_numbers(self) -> None:
        text = "総額が 1,500,000.50 より大きい注文"
        nums = extract_numeric_literals(text)
        assert nums == [1500000.50]

    def test_multiple_numbers_and_symbols(self) -> None:
        text = "顧客 5 件、売上 +250 または 0.01 以上"
        nums = extract_numeric_literals(text)
        assert nums == [5.0, 250.0, 0.01]

    def test_text_without_numbers(self) -> None:
        text = "アジアの顧客一覧を表示して"
        nums = extract_numeric_literals(text)
        assert nums == []


class TestJevPromptPayload:
    """Tests for build_jev_payload helper."""

    def test_payload_structure_without_schema(self) -> None:
        payload = build_jev_payload("顧客の売上トップ5")
        assert payload["eval_state"] == "顧客の売上トップ5"
        assert "tasks" in payload
        task_ids = [t["id"] for t in payload["tasks"]]
        assert "primary_table" in task_ids
        assert "dimension" in task_ids
        assert "metric" in task_ids
        assert "ranking" in task_ids
        assert "ambiguity" in task_ids

    def test_payload_structure_with_schema(self, tpch_schema: SchemaMetadata) -> None:
        payload = build_jev_payload("アジアの売上", schema=tpch_schema)
        primary_task = next(t for t in payload["tasks"] if t["id"] == "primary_table")
        assert "customer" in primary_task["choices"]
        assert "orders" in primary_task["choices"]
        assert "lineitem" in primary_task["choices"]


class TestJevSlotParsingAndWhitelistBinding:
    """Tests for parse_jev_response and F-JEV-02 whitelist choice binding."""

    def test_normal_response_parsing(self, tpch_schema: SchemaMetadata) -> None:
        raw_api_response = {
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
        query = "アジアの顧客の売上トップ5"
        parsed = parse_jev_response(query, raw_api_response, schema=tpch_schema)

        assert isinstance(parsed, ParsedSlots)
        assert parsed.primary_table == "customer"
        assert parsed.secondary_table == "orders"
        assert parsed.dimension == "c_name"
        assert parsed.metric == "sum_revenue"
        assert parsed.ranking == "top_5"
        assert parsed.needs_clarification is False
        assert len(parsed.filter_slots) >= 1
        assert any(
            f.table == "region" and f.column == "r_name" and f.value == "ASIA"
            for f in parsed.filter_slots
        )

    def test_whitelist_binding_sanitizes_injection(self, tpch_schema: SchemaMetadata) -> None:
        """F-JEV-02: Whitelist binding drops unknown values and SQL injection attempts."""
        malicious_response = {
            "results": {
                "primary_table": "customer",
                "secondary_table": "none",
                "dimension": "none",
                "metric": "count_records",
                "ranking": "none",
                "ambiguity": "clear",
                "filters": [
                    "' OR '1'='1",
                    "UNKNOWN_REGION",
                    "DROP TABLE orders;",
                    "ASIA",
                ],
            }
        }
        query = "アジアの顧客"
        parsed = parse_jev_response(query, malicious_response, schema=tpch_schema)

        # Only 'ASIA' is whitelisted in region.r_name
        values_in_filters = [f.value for f in parsed.filter_slots]
        assert "ASIA" in values_in_filters
        assert "' OR '1'='1" not in values_in_filters
        assert "UNKNOWN_REGION" not in values_in_filters
        assert "DROP TABLE orders;" not in values_in_filters

    def test_ambiguity_hitl_flag_detection(self) -> None:
        ambiguous_response = {
            "results": {
                "primary_table": "orders",
                "ambiguity": "ambiguous_revenue_definition",
            }
        }
        parsed = parse_jev_response("売上トップ5", ambiguous_response)
        assert parsed.needs_clarification is True

    def test_numeric_filter_extraction_in_slots(self, tpch_schema: SchemaMetadata) -> None:
        response_data = {
            "results": {
                "primary_table": "orders",
                "metric": "sum_revenue",
                "ranking": "none",
            }
        }
        query = "合計金額が 1000 以上の注文"
        parsed = parse_jev_response(query, response_data, schema=tpch_schema)
        num_filters = [f for f in parsed.filter_slots if f.operator == ">="]
        assert len(num_filters) == 1
        assert num_filters[0].table == "orders"
        assert num_filters[0].column == "o_totalprice"
        assert num_filters[0].value == 1000.0


class TestJevClientNetworkAndRetries:
    """London-school tests for JevClient with MockTransport."""

    def test_extract_slots_e2e_mock(self, tpch_schema: SchemaMetadata) -> None:
        mock_response_data = {
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
            assert request.method == "POST"
            assert request.url.path == "/v1/systemone"
            assert request.headers["Authorization"] == "Bearer ts_test_key_123"
            return httpx.Response(200, json=mock_response_data)

        with JevClient(api_key="ts_test_key_123") as client:
            client._client = httpx.Client(
                base_url="https://api.typesafe.ai",
                transport=httpx.MockTransport(handler),
            )
            slots = client.extract_slots("アジアの顧客の売上トップ5", schema=tpch_schema)

        assert slots.primary_table == "customer"
        assert slots.dimension == "c_name"
        assert slots.metric == "sum_revenue"
        assert slots.ranking == "top_5"
        assert len(slots.filter_slots) >= 1

    def test_missing_api_key_raises_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        client = JevClient(api_key=None)
        with pytest.raises(JevAPIConnectionError, match="TypeSafe API key is required"):
            client.call_api({"eval_state": "test"})

    def test_server_error_500_retries_and_raises(self) -> None:
        call_count = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(500, text="Internal Server Error")

        client = JevClient(api_key="ts_test_key", max_retries=2)
        client._client = httpx.Client(
            base_url="https://api.typesafe.ai",
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(JevAPIConnectionError, match="Server error 500"):
            client.call_api({"eval_state": "test"})

        # Initial attempt + 2 retries = 3 calls
        assert call_count == 3
        client.close()

    def test_server_error_500_recovers_on_retry(self) -> None:
        call_count = 0
        mock_response_data = {
            "results": {
                "primary_table": "orders",
                "metric": "count_records",
                "ranking": "none",
            }
        }

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(500, text="Temporary glitch")
            return httpx.Response(200, json=mock_response_data)

        client = JevClient(api_key="ts_test_key", max_retries=2)
        client._client = httpx.Client(
            base_url="https://api.typesafe.ai",
            transport=httpx.MockTransport(handler),
        )

        result = client.call_api({"eval_state": "test"})
        assert result["results"]["primary_table"] == "orders"
        assert call_count == 2
        client.close()

    def test_timeout_retries_and_raises(self) -> None:
        call_count = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            raise httpx.TimeoutException("Read timed out")

        client = JevClient(api_key="ts_test_key", max_retries=2)
        client._client = httpx.Client(
            base_url="https://api.typesafe.ai",
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(JevAPIConnectionError, match="Network error during Jev API call"):
            client.call_api({"eval_state": "test"})

        assert call_count == 3
        client.close()

    def test_client_error_400_fails_fast_without_retry(self) -> None:
        call_count = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(400, text="Bad Request: invalid question format")

        client = JevClient(api_key="ts_test_key", max_retries=2)
        client._client = httpx.Client(
            base_url="https://api.typesafe.ai",
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(JevAPIConnectionError, match="client error 400"):
            client.call_api({"eval_state": "test"})

        # Client error should not be retried
        assert call_count == 1
        client.close()

    def test_malformed_json_response_raises_parse_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="<!DOCTYPE html><html>Not JSON</html>",
                headers={"Content-Type": "text/html"},
            )

        client = JevClient(api_key="ts_test_key")
        client._client = httpx.Client(
            base_url="https://api.typesafe.ai",
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(JevResponseParseError):
            client.call_api({"eval_state": "test"})
        client.close()

    def test_non_dict_json_response_raises_parse_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="[1, 2, 3]",
                headers={"Content-Type": "application/json"},
            )

        client = JevClient(api_key="ts_test_key")
        client._client = httpx.Client(
            base_url="https://api.typesafe.ai",
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(JevResponseParseError, match="Expected JSON object"):
            client.call_api({"eval_state": "test"})
        client.close()
