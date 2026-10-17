"""Unit tests for DeterministicTextToSQLEngine with Apache Ossie schema (TASK-19).

Detroit-style tests verifying:
- Initialization with Ossie YAML / dict formats
- Slot-to-intent metric mapping via schema.get_metric (canonical names and synonyms)
- Correct AST expansion and SQL generation with calculated metrics
- Rich metadata retrieval via get_schema_info() including metrics and hierarchies
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import sqlglot
import yaml

from deterministic_text_to_sql.engine import DeterministicTextToSQLEngine
from deterministic_text_to_sql.jev.client import JevClient
from deterministic_text_to_sql.models import EngineStatus
from deterministic_text_to_sql.schema import load_schema

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "tpch_ossie.yaml"


def create_mock_jev_client(mock_responses: dict[str, dict[str, Any]] | None = None) -> JevClient:
    """Create a JevClient backed by an in-memory httpx MockTransport."""
    default_response = {
        "results": {
            "primary_table": "customer",
            "secondary_table": "none",
            "dimension": "c_name",
            "metric": "total_revenue",
            "ranking": "top_5",
            "ambiguity": "clear",
            "filters": ["ASIA"],
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        eval_state = body.get("state", body.get("eval_state", ""))

        if mock_responses:
            for key, resp in mock_responses.items():
                if key in eval_state:
                    return httpx.Response(200, json=resp)

        return httpx.Response(200, json=default_response)

    client = JevClient(api_key="ts_test_key_ossie")
    client._client = httpx.Client(
        base_url="https://api.typesafe.ai",
        transport=httpx.MockTransport(handler),
    )
    return client


class TestEngineOssieInitialization:
    """Tests for DeterministicTextToSQLEngine initialization with Ossie models."""

    def test_init_with_ossie_yaml_path(self) -> None:
        client = create_mock_jev_client()
        engine = DeterministicTextToSQLEngine(schema=FIXTURE_PATH, jev_client=client)
        assert len(engine.schema.tables) == 8
        assert len(engine.schema.metrics) == 6
        assert len(engine.schema.hierarchies) == 1
        assert engine.schema.title == "tpch_semantic_model"

    def test_init_with_ossie_yaml_str(self) -> None:
        client = create_mock_jev_client()
        engine = DeterministicTextToSQLEngine(schema=str(FIXTURE_PATH), jev_client=client)
        assert len(engine.schema.tables) == 8
        assert len(engine.schema.metrics) == 6

    def test_init_with_ossie_dict(self) -> None:
        client = create_mock_jev_client()
        data = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
        engine = DeterministicTextToSQLEngine(schema=data, jev_client=client)
        assert len(engine.schema.tables) == 8
        assert len(engine.schema.metrics) == 6
        assert len(engine.schema.hierarchies) == 1

    def test_init_with_loaded_schema_metadata(self) -> None:
        client = create_mock_jev_client()
        loaded = load_schema(FIXTURE_PATH)
        engine = DeterministicTextToSQLEngine(schema=loaded, jev_client=client)
        assert engine.schema == loaded


class TestEngineOssieProcessQuery:
    """Tests for end-to-end query processing with semantic metrics from Ossie schema."""

    def test_metric_canonical_name_resolution(self) -> None:
        """Verify metric 'total_revenue' expands expression and sorts by alias."""
        mock_resp = {
            "アジアの顧客の売上トップ5": {
                "results": {
                    "primary_table": "customer",
                    "secondary_table": "orders",
                    "dimension": "c_name",
                    "metric": "total_revenue",
                    "ranking": "top_5",
                    "ambiguity": "clear",
                    "filters": ["ASIA"],
                }
            }
        }
        client = create_mock_jev_client(mock_resp)
        with DeterministicTextToSQLEngine(schema=FIXTURE_PATH, jev_client=client) as engine:
            result = engine.process_query("アジアの顧客の売上トップ5", dialect="postgres")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None

        parsed = sqlglot.parse_one(result.sql, read="postgres")
        sql_normalized = parsed.sql(dialect="postgres").lower()

        # Expression SUM(lineitem.l_extendedprice * (1 - lineitem.l_discount)) AS total_revenue
        assert "l_extendedprice * (1 - lineitem.l_discount)" in sql_normalized
        assert "as total_revenue" in sql_normalized
        # ORDER BY total_revenue DESC LIMIT 5
        assert "order by total_revenue desc" in sql_normalized
        assert "limit 5" in sql_normalized
        # Multi-table join resolution
        assert "join orders" in sql_normalized
        assert "join lineitem" in sql_normalized

    def test_metric_synonym_resolution(self) -> None:
        """Verify natural language synonym '売上' maps to total_revenue metric."""
        mock_resp = {
            "顧客別売上": {
                "results": {
                    "primary_table": "customer",
                    "secondary_table": "none",
                    "dimension": "c_name",
                    "metric": "売上",
                    "ranking": "none",
                    "ambiguity": "clear",
                    "filters": [],
                }
            }
        }
        client = create_mock_jev_client(mock_resp)
        with DeterministicTextToSQLEngine(schema=FIXTURE_PATH, jev_client=client) as engine:
            result = engine.process_query("顧客別売上", dialect="postgres")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None

        parsed = sqlglot.parse_one(result.sql, read="postgres")
        sql_normalized = parsed.sql(dialect="postgres").lower()

        assert "l_extendedprice * (1 - lineitem.l_discount)" in sql_normalized
        assert "as total_revenue" in sql_normalized

    def test_metric_synonym_average_price(self) -> None:
        """Verify natural language synonym '平均単価' maps to avg_price metric."""
        mock_resp = {
            "明細の平均単価": {
                "results": {
                    "primary_table": "lineitem",
                    "secondary_table": "none",
                    "dimension": "none",
                    "metric": "平均単価",
                    "ranking": "none",
                    "ambiguity": "clear",
                    "filters": [],
                }
            }
        }
        client = create_mock_jev_client(mock_resp)
        with DeterministicTextToSQLEngine(schema=FIXTURE_PATH, jev_client=client) as engine:
            result = engine.process_query("明細の平均単価", dialect="postgres")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None

        parsed = sqlglot.parse_one(result.sql, read="postgres")
        sql_normalized = parsed.sql(dialect="postgres").lower()

        assert "avg(lineitem.l_extendedprice)" in sql_normalized
        assert "as avg_price" in sql_normalized

    def test_fallback_heuristic_for_legacy_slots(self) -> None:
        """Verify unmapped legacy metric slot 'sum_quantity' falls back gracefully."""
        mock_resp = {
            "数量合計": {
                "results": {
                    "primary_table": "lineitem",
                    "secondary_table": "none",
                    "dimension": "none",
                    "metric": "sum_quantity",
                    "ranking": "none",
                    "ambiguity": "clear",
                    "filters": [],
                }
            }
        }
        client = create_mock_jev_client(mock_resp)
        with DeterministicTextToSQLEngine(schema=FIXTURE_PATH, jev_client=client) as engine:
            result = engine.process_query("数量合計", dialect="postgres")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None
        assert "sum(lineitem.l_quantity)" in result.sql.lower()


class TestEngineOssieSchemaInfo:
    """Tests for get_schema_info() with Ossie metadata."""

    def test_get_schema_info_contains_metrics_and_hierarchies(self) -> None:
        client = create_mock_jev_client()
        with DeterministicTextToSQLEngine(schema=FIXTURE_PATH, jev_client=client) as engine:
            info = engine.get_schema_info()

        assert "metrics" in info
        assert "hierarchies" in info
        assert len(info["metrics"]) == 6
        assert len(info["hierarchies"]) == 1

        # Check total_revenue metric details
        metric_names = [m["name"] for m in info["metrics"]]
        assert "total_revenue" in metric_names
        assert "total_quantity" in metric_names
        assert "avg_price" in metric_names

        tr = next(m for m in info["metrics"] if m["name"] == "total_revenue")
        assert "SUM(lineitem.l_extendedprice" in tr["expression"]
        assert "売上" in tr["synonyms"]

        # Check hierarchy details
        h0 = info["hierarchies"][0]
        assert h0["parent_table"] == "region"
        assert h0["parent_column"] == "r_name"
        assert h0["child_table"] == "nation"
        assert h0["child_column"] == "n_name"
        assert h0["child_to_parent"]["JAPAN"] == "ASIA"
