"""Unit tests for Typer/Rich CLI interface with Apache Ossie schema (TASK-19).

Tests cover:
- One-shot query execution with --schema <tpch_ossie.yaml>
- Pure structured JSON query output with --json
- Schema inspection command with Rich tables (Metrics, Hierarchies)
- Schema inspection command with pure JSON output containing metrics and hierarchies
- Automatic default schema resolution prioritizing tpch_ossie.yaml
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from deterministic_text_to_sql.cli import app
from deterministic_text_to_sql.engine import DeterministicTextToSQLEngine
from deterministic_text_to_sql.jev.client import JevClient

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "tpch_ossie.yaml"


def build_mock_jev_client() -> JevClient:
    """Build a mock JevClient backed by httpx.MockTransport."""
    default_response = {
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

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=default_response)

    client = JevClient(api_key="ts_test_key_cli_ossie")
    client._client = httpx.Client(
        base_url="https://api.typesafe.ai",
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.fixture
def mock_engine_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch create_engine in cli module to inject MockTransport client."""
    from deterministic_text_to_sql.cli import resolve_schema_path

    def _mock_create_engine(
        schema_path: Path | None = None,
        api_key: str | None = None,
        jev_client: JevClient | None = None,
    ) -> DeterministicTextToSQLEngine:
        resolved = resolve_schema_path(schema_path)
        client = jev_client or build_mock_jev_client()
        return DeterministicTextToSQLEngine(
            schema=resolved,
            jev_client=client,
            api_key=api_key,
        )

    monkeypatch.setattr("deterministic_text_to_sql.cli.create_engine", _mock_create_engine)


@pytest.fixture
def runner() -> CliRunner:
    """Fixture providing Typer CliRunner."""
    return CliRunner()


@pytest.mark.usefixtures("mock_engine_client")
class TestCliOssieOneShotQuery:
    """Tests for one-shot query execution using Ossie schema."""

    def test_query_rich_output(self, runner: CliRunner) -> None:
        result = runner.invoke(
            app,
            ["アジアの顧客の売上トップ5 / Aで", "--schema", str(FIXTURE_PATH)],
        )
        assert result.exit_code == 0
        assert "Generated Deterministic SQL" in result.output
        assert "SELECT" in result.output
        assert "total_revenue" in result.output

    def test_query_json_output(self, runner: CliRunner) -> None:
        result = runner.invoke(
            app,
            ["アジアの顧客の売上トップ5 / Aで", "--schema", str(FIXTURE_PATH), "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert "total_revenue" in data["sql"]
        assert data["intent"]["metrics"][0]["alias"] == "total_revenue"


@pytest.mark.usefixtures("mock_engine_client")
class TestCliOssieSchemaCommand:
    """Tests for schema inspection command with Ossie schema."""

    def test_schema_rich_output_includes_metrics_and_hierarchies(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(app, ["schema", "--schema", str(FIXTURE_PATH)])
        assert result.exit_code == 0
        assert "Database Schema Inspector" in result.output
        assert "Table: customer" in result.output

        # Verify Semantic Metrics table
        assert "Semantic Metrics" in result.output
        assert "total_revenue" in result.output
        assert "total_quantity" in result.output
        assert "avg_price" in result.output
        assert "SUM(lineitem" in result.output

        # Verify Ontology Hierarchies table
        assert "Ontology Hierarchies" in result.output
        assert "region.r_name" in result.output
        assert "nation.n_name" in result.output

    def test_schema_json_output_includes_metrics_and_hierarchies(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(app, ["schema", "--schema", str(FIXTURE_PATH), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)

        assert "tables" in data
        assert len(data["tables"]) == 8

        assert "metrics" in data
        assert len(data["metrics"]) == 6
        metric_names = [m["name"] for m in data["metrics"]]
        assert "total_revenue" in metric_names
        assert "total_quantity" in metric_names
        assert "avg_price" in metric_names
        assert "order_count" in metric_names
        assert "customer_count" in metric_names

        assert "hierarchies" in data
        assert len(data["hierarchies"]) == 1
        assert data["hierarchies"][0]["parent_table"] == "region"
        assert data["hierarchies"][0]["child_table"] == "nation"

    def test_schema_default_path_resolves_ossie_yaml(self, runner: CliRunner) -> None:
        """Verify omitting --schema resolves tpch_ossie.yaml and includes metrics."""
        result = runner.invoke(app, ["schema", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "metrics" in data
        assert len(data["metrics"]) == 6
