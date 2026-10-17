"""Comprehensive unit tests for the Typer/Rich CLI interface (TASK-09).

London-style unit tests verifying:
- One-shot query execution (human Rich format and pure JSON for AI agents)
- HITL ambiguity intervention output and exit code 2
- Slash syntax (/ ...) and --clarify parameter resolution
- Subcommands: schema inspection and interactive REPL mode
- Default interactive REPL invocation when query argument is omitted
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from deterministic_text_to_sql.cli import app
from deterministic_text_to_sql.engine import DeterministicTextToSQLEngine
from deterministic_text_to_sql.jev.client import JevClient
from deterministic_text_to_sql.schema.loader import load_schema_from_json
from deterministic_text_to_sql.schema.models import SchemaMetadata

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "tpch_schema.json"


@pytest.fixture
def tpch_schema() -> SchemaMetadata:
    """Load TPC-H schema metadata fixture."""
    return load_schema_from_json(FIXTURE_PATH)


def build_mock_jev_client() -> JevClient:
    """Build a mock JevClient backed by httpx.MockTransport with deterministic responses."""
    mock_responses: dict[str, dict[str, Any]] = {
        "lineitem 商品明細売上": {
            "results": {
                "primary_table": "lineitem",
                "secondary_table": "none",
                "dimension": "none",
                "metric": "sum_revenue",
                "ranking": "top_5",
                "ambiguity": "clear",
                "filters": ["ASIA"],
            }
        },
        "orders 注文伝票売上": {
            "results": {
                "primary_table": "orders",
                "secondary_table": "none",
                "dimension": "none",
                "metric": "sum_revenue",
                "ranking": "top_5",
                "ambiguity": "clear",
                "filters": ["ASIA"],
            }
        },
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
        },
        "アジアの顧客の売上トップ5": {
            "results": {
                "primary_table": "orders",
                "secondary_table": "lineitem",
                "dimension": "c_name",
                "metric": "sum_revenue",
                "ranking": "top_5",
                "ambiguity": "ambiguous_revenue_definition",
                "needs_clarification": True,
                "filters": ["ASIA"],
            }
        },
    }

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
        eval_state = body.get("state", body.get("eval_state", ""))

        for key, resp in mock_responses.items():
            if key in eval_state:
                return httpx.Response(200, json=resp)

        return httpx.Response(200, json=default_response)

    client = JevClient(api_key="ts_test_key_cli")
    client._client = httpx.Client(
        base_url="https://api.typesafe.ai",
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.fixture
def mock_engine_factory(monkeypatch: pytest.MonkeyPatch, tpch_schema: SchemaMetadata) -> None:
    """Patch create_engine in cli module to inject MockTransport client."""
    def _mock_create_engine(
        schema_path: Path | None = None,
        api_key: str | None = None,
        jev_client: JevClient | None = None,
    ) -> DeterministicTextToSQLEngine:
        target_schema = tpch_schema if schema_path is None else schema_path
        client = jev_client or build_mock_jev_client()
        return DeterministicTextToSQLEngine(
            schema=target_schema,
            jev_client=client,
            api_key=api_key,
        )

    monkeypatch.setattr("deterministic_text_to_sql.cli.create_engine", _mock_create_engine)


@pytest.fixture
def runner() -> CliRunner:
    """Fixture providing Typer CliRunner."""
    return CliRunner()


@pytest.mark.usefixtures("mock_engine_factory")
class TestOneShotQuery:
    """Tests for one-shot deterministic query compilation."""

    def test_oneshot_success_rich(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["アジアの顧客の売上トップ5 / Aで"])
        assert result.exit_code == 0
        assert "Generated Deterministic SQL" in result.output
        assert "SELECT" in result.output
        assert "Dialect: postgres" in result.output
        assert "Total Latency:" in result.output

    def test_oneshot_success_json(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["アジアの顧客の売上トップ5 / Aで", "--json"])
        assert result.exit_code == 0

        # Must parse as clean JSON without ANSI color escape sequences
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["query"] == "アジアの顧客の売上トップ5"
        assert data["clarification"] == "Aで"
        assert data["dialect"] == "postgres"
        assert data["sql"] is not None
        assert "SELECT" in data["sql"]
        assert data["latency_ms"]["total"] >= 0.0

    def test_oneshot_hitl_rich_exit_code_2(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(app, ["アジアの顧客の売上トップ5"])
        assert result.exit_code == 2
        assert "Human-in-the-Loop Disambiguation Required" in result.output
        assert "Question:" in result.output
        assert "Key" in result.output
        assert "A" in result.output
        assert "B" in result.output
        assert "--clarify" in result.output

    def test_oneshot_hitl_json_exit_code_2(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(app, ["アジアの顧客の売上トップ5", "--json"])
        assert result.exit_code == 2

        data = json.loads(result.output)
        assert data["status"] == "hitl_required"
        assert data["query"] == "アジアの顧客の売上トップ5"
        assert data["sql"] is None
        assert data["hitl_issue"] is not None
        assert len(data["hitl_issue"]["options"]) >= 2
        assert data["hitl_summary"] is not None

    def test_oneshot_clarify_option_resolves(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            app, ["アジアの顧客の売上トップ5", "--clarify", "B", "--json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["clarification"] == "B"
        assert "orders" in data["sql"]

    def test_oneshot_force_bypasses_hitl(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(app, ["アジアの顧客の売上トップ5", "--force", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["sql"] is not None

    def test_oneshot_dialect_option(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            app, ["アジアの顧客の売上トップ5 / Aで", "--dialect", "duckdb", "--json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["dialect"] == "duckdb"

    def test_oneshot_empty_query_error(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(app, ["   ", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert "cannot be empty" in data["error_message"]

    def test_oneshot_nonexistent_schema_error(
        self, runner: CliRunner
    ) -> None:
        # Directly test with a bad schema path (without mock_engine_factory patch)
        result = runner.invoke(
            app, ["SELECT 1", "--schema", "non_existent_schema_xyz.json", "--json"]
        )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert "not exist" in data["error_message"] or "not found" in data["error_message"]


@pytest.mark.usefixtures("mock_engine_factory")
class TestSchemaCommand:
    """Tests for database schema inspection command."""

    def test_schema_rich_table(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["schema"])
        assert result.exit_code == 0
        assert "Database Schema Inspector" in result.output
        assert "Table: customer" in result.output
        assert "c_custkey" in result.output
        assert "orders" in result.output

    def test_schema_json(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["schema", "--json"])
        assert result.exit_code == 0

        data = json.loads(result.output)
        assert "table_names" in data
        assert "customer" in data["table_names"]
        assert "orders" in data["table_names"]
        assert "tables" in data
        assert len(data["tables"]) == 8

    def test_schema_invalid_path_error(self, runner: CliRunner) -> None:
        result = runner.invoke(
            app, ["schema", "--schema", "invalid_path_xyz.json", "--json"]
        )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data


@pytest.mark.usefixtures("mock_engine_factory")
class TestInteractiveCommand:
    """Tests for human-friendly interactive REPL mode."""

    def test_interactive_quit_immediately(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(app, ["interactive"], input="exit\n")
        assert result.exit_code == 0
        assert "Interactive REPL" in result.output
        assert "Goodbye!" in result.output

    def test_interactive_query_then_quit(
        self, runner: CliRunner
    ) -> None:
        user_inputs = "アジアの顧客の売上トップ5 / Aで\nexit\n"
        result = runner.invoke(app, ["interactive"], input=user_inputs)
        assert result.exit_code == 0
        assert "Generated Deterministic SQL" in result.output
        assert "SELECT" in result.output
        assert "Goodbye!" in result.output

    def test_interactive_hitl_immediate_resolution(
        self, runner: CliRunner
    ) -> None:
        # 1. Ambiguous query triggers HITL prompt
        # 2. Immediate choice 'A' resolves ambiguity
        # 3. Exit REPL
        user_inputs = "アジアの顧客の売上トップ5\nA\nexit\n"
        result = runner.invoke(app, ["interactive"], input=user_inputs)
        assert result.exit_code == 0
        assert "Human-in-the-Loop Disambiguation Required" in result.output
        assert "Generated Deterministic SQL" in result.output
        assert "Goodbye!" in result.output

    def test_interactive_hitl_followup_turn_resolution(
        self, runner: CliRunner
    ) -> None:
        # 1. Ambiguous query triggers HITL prompt
        # 2. Skip immediate prompt (Enter)
        # 3. Enter 'Aで' in the next REPL turn
        # 4. Exit REPL
        user_inputs = "アジアの顧客の売上トップ5\n\nAで\nexit\n"
        result = runner.invoke(app, ["interactive"], input=user_inputs)
        assert result.exit_code == 0
        assert "Human-in-the-Loop Disambiguation Required" in result.output
        assert "Generated Deterministic SQL" in result.output

    def test_default_invocation_launches_interactive(
        self, runner: CliRunner
    ) -> None:
        # Invoking root CLI with no arguments must start interactive REPL
        result = runner.invoke(app, [], input="q\n")
        assert result.exit_code == 0
        assert "Interactive REPL" in result.output
        assert "Goodbye!" in result.output
