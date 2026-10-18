"""End-to-End integration tests for deterministic-text-to-sql using TPC-H benchmark (TASK-10).

Detroit-style comprehensive E2E tests validating:
1. Five representative TPC-H query patterns:
   - Case 1: Customer Aggregation (multi-hop join: customer <-> nation <-> region)
   - Case 2: Revenue Ranking (customer <-> orders with ORDER BY + LIMIT)
   - Case 3: Inventory Shortage (part <-> partsupp with inequality numeric filter)
   - Case 4: Ambiguous Query HITL (ambiguity detection -> slash syntax/clarify resolution)
   - Case 5: Hierarchical Deduplication (ASIA + JAPAN pruning redundant region joins)
2. Real database execution verification:
   - Full TPC-H 8-table DDL and sample seed data loaded into in-memory SQLite (and optional DuckDB)
   - All generated SQL statements are executed against the database to prove semantic validity.
3. Performance benchmark:
   - AST build latency under 10ms (<10.0 ms target)
   - 100 consecutive compilations average under 5ms.
4. Installed package CLI executable verification:
   - Subprocess invocation of `deterministic-text-to-sql query "日本の顧客" --json`
   - Zero exit code and valid structured JSON output.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
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

try:
    import duckdb  # type: ignore

    HAS_DUCKDB = True
except ImportError:
    HAS_DUCKDB = False


# ==============================================================================
# TPC-H 8 Tables DDL & Seed Data for Real Database Execution
# ==============================================================================

TPCH_DDL_STATEMENTS = [
    """
    CREATE TABLE region (
        r_regionkey INTEGER PRIMARY KEY,
        r_name CHAR(25) NOT NULL,
        r_comment VARCHAR(152)
    );
    """,
    """
    CREATE TABLE nation (
        n_nationkey INTEGER PRIMARY KEY,
        n_name CHAR(25) NOT NULL,
        n_regionkey INTEGER NOT NULL REFERENCES region(r_regionkey),
        n_comment VARCHAR(152)
    );
    """,
    """
    CREATE TABLE supplier (
        s_suppkey INTEGER PRIMARY KEY,
        s_name CHAR(25) NOT NULL,
        s_address VARCHAR(40) NOT NULL,
        s_nationkey INTEGER NOT NULL REFERENCES nation(n_nationkey),
        s_phone CHAR(15) NOT NULL,
        s_acctbal DECIMAL(15,2) NOT NULL,
        s_comment VARCHAR(101)
    );
    """,
    """
    CREATE TABLE customer (
        c_custkey BIGINT PRIMARY KEY,
        c_name VARCHAR(25) NOT NULL,
        c_address VARCHAR(40) NOT NULL,
        c_nationkey INTEGER NOT NULL REFERENCES nation(n_nationkey),
        c_phone CHAR(15) NOT NULL,
        c_acctbal DOUBLE PRECISION NOT NULL,
        c_mktsegment CHAR(10) NOT NULL,
        c_comment VARCHAR(117)
    );
    """,
    """
    CREATE TABLE part (
        p_partkey INTEGER PRIMARY KEY,
        p_name VARCHAR(55) NOT NULL,
        p_mfgr CHAR(25) NOT NULL,
        p_brand CHAR(10) NOT NULL,
        p_type VARCHAR(25) NOT NULL,
        p_size INTEGER NOT NULL,
        p_container CHAR(10) NOT NULL,
        p_retailprice DECIMAL(15,2) NOT NULL,
        p_comment VARCHAR(23)
    );
    """,
    """
    CREATE TABLE partsupp (
        ps_partkey BIGINT NOT NULL REFERENCES part(p_partkey),
        ps_suppkey BIGINT NOT NULL REFERENCES supplier(s_suppkey),
        ps_availqty BIGINT NOT NULL,
        ps_supplycost DOUBLE PRECISION NOT NULL,
        ps_comment VARCHAR(199),
        PRIMARY KEY (ps_partkey, ps_suppkey)
    );
    """,
    """
    CREATE TABLE orders (
        o_orderkey BIGINT PRIMARY KEY,
        o_custkey BIGINT NOT NULL REFERENCES customer(c_custkey),
        o_orderstatus CHAR(1) NOT NULL,
        o_totalprice DOUBLE PRECISION NOT NULL,
        o_orderdate DATE NOT NULL,
        o_orderpriority CHAR(15) NOT NULL,
        o_clerk CHAR(15) NOT NULL,
        o_shippriority INTEGER NOT NULL,
        o_comment VARCHAR(79)
    );
    """,
    """
    CREATE TABLE lineitem (
        l_orderkey BIGINT NOT NULL REFERENCES orders(o_orderkey),
        l_partkey BIGINT NOT NULL REFERENCES part(p_partkey),
        l_suppkey BIGINT NOT NULL REFERENCES supplier(s_suppkey),
        l_linenumber BIGINT NOT NULL,
        l_quantity DOUBLE PRECISION NOT NULL,
        l_extendedprice DOUBLE PRECISION NOT NULL,
        l_discount DOUBLE PRECISION NOT NULL,
        l_tax DOUBLE PRECISION NOT NULL,
        l_returnflag CHAR(1) NOT NULL,
        l_linestatus CHAR(1) NOT NULL,
        l_shipdate DATE NOT NULL,
        l_commitdate DATE NOT NULL,
        l_receiptdate DATE NOT NULL,
        l_shipinstruct CHAR(25) NOT NULL,
        l_shipmode CHAR(10) NOT NULL,
        l_comment VARCHAR(44),
        PRIMARY KEY (l_orderkey, l_linenumber)
    );
    """,
]

TPCH_SEED_STATEMENTS = [
    (
        "INSERT INTO region (r_regionkey, r_name, r_comment) "
        "VALUES (2, 'ASIA', 'Asian continent');"
    ),
    (
        "INSERT INTO nation (n_nationkey, n_name, n_regionkey, n_comment) "
        "VALUES (12, 'JAPAN', 2, 'Land of the rising sun');"
    ),
    (
        "INSERT INTO customer (c_custkey, c_name, c_address, c_nationkey, c_phone, "
        "c_acctbal, c_mktsegment, c_comment) "
        "VALUES (1, 'Customer#000000001', 'Tokyo, Japan', 12, '81-3-1234-5678', "
        "1500.0, 'BUILDING', 'Regular customer');"
    ),
    (
        "INSERT INTO supplier (s_suppkey, s_name, s_address, s_nationkey, s_phone, "
        "s_acctbal, s_comment) "
        "VALUES (1, 'Supplier#000000001', 'Osaka, Japan', 12, '81-6-1234-5678', "
        "5000.0, 'Reliable');"
    ),
    (
        "INSERT INTO part (p_partkey, p_name, p_mfgr, p_brand, p_type, p_size, "
        "p_container, p_retailprice, p_comment) "
        "VALUES (201, 'golden part brass', 'Manufacturer#1', 'Brand#11', "
        "'PROMO BRUSHED BRASS', 15, 'SM CASE', 120.0, 'Brass component');"
    ),
    (
        "INSERT INTO partsupp (ps_partkey, ps_suppkey, ps_availqty, ps_supplycost, ps_comment) "
        "VALUES (201, 1, 5, 80.0, 'Limited supply available');"
    ),
    (
        "INSERT INTO orders (o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
        "o_orderpriority, o_clerk, o_shippriority, o_comment) "
        "VALUES (101, 1, 'O', 8500.5, '2026-01-15', '1-URGENT', 'Clerk#001', 0, 'Fast order');"
    ),
    (
        "INSERT INTO lineitem (l_orderkey, l_partkey, l_suppkey, l_linenumber, l_quantity, "
        "l_extendedprice, l_discount, l_tax, l_returnflag, l_linestatus, l_shipdate, "
        "l_commitdate, l_receiptdate, l_shipinstruct, l_shipmode, l_comment) "
        "VALUES (101, 201, 1, 1, 10.0, 1200.0, 0.05, 0.08, 'N', 'O', '2026-01-20', "
        "'2026-01-25', '2026-01-26', 'DELIVER IN PERSON', 'AIR', 'Urgent lineitem');"
    ),
]


@pytest.fixture(scope="session")
def tpch_schema() -> SchemaMetadata:
    """Load TPC-H schema metadata fixture."""
    return load_schema_from_json(FIXTURE_PATH)


@pytest.fixture
def sqlite_db() -> Generator[sqlite3.Connection, None, None]:
    """Provide an in-memory SQLite database pre-configured with TPC-H tables and seed data."""
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    for ddl in TPCH_DDL_STATEMENTS:
        cursor.execute(ddl)
    for seed in TPCH_SEED_STATEMENTS:
        cursor.execute(seed)
    conn.commit()
    yield conn
    conn.close()


def create_tpch_mock_client() -> JevClient:
    """Create a JevClient backed by httpx.MockTransport supporting the 5 representative cases."""
    mock_responses: dict[str, dict[str, Any]] = {
        # Case 1: Customer Aggregation (customer <-> nation <-> region)
        "アジア地域の顧客残高集計": {
            "results": {
                "primary_table": "customer",
                "secondary_table": "nation",
                "dimension": "c_name",
                "metric": "sum_acctbal",
                "ranking": "none",
                "ambiguity": "clear",
                "filters": ["ASIA"],
            }
        },
        # Case 2: Revenue Ranking (customer <-> orders, ORDER BY + LIMIT)
        "売上上位5件の顧客と注文情報": {
            "results": {
                "primary_table": "customer",
                "secondary_table": "orders",
                "dimension": "c_name",
                "metric": "sum_revenue",
                "ranking": "top_5",
                "ambiguity": "clear",
            }
        },
        # Case 3: Inventory Shortage (part <-> partsupp, ps_availqty <= 10)
        "在庫数10以下の部品一覧": {
            "results": {
                "primary_table": "part",
                "secondary_table": "partsupp",
                "dimension": "p_name",
                "metric": "count_records",
                "ranking": "none",
                "ambiguity": "clear",
                "filter_slots": [
                    {
                        "table": "partsupp",
                        "column": "ps_availqty",
                        "operator": "<=",
                        "value": 10,
                    }
                ],
            }
        },
        # Case 4a: Ambiguous query requiring clarification
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
        # Case 4b: Ambiguous query resolved via Option A (lineitem)
        "lineitem 商品明細売上": {
            "results": {
                "primary_table": "lineitem",
                "secondary_table": "none",
                "dimension": "none",
                "metric": "sum_revenue",
                "ranking": "top_5",
                "ambiguity": "clear",
            }
        },
        # Case 4c: Ambiguous query resolved via Option B (orders)
        "orders 注文伝票売上": {
            "results": {
                "primary_table": "orders",
                "secondary_table": "none",
                "dimension": "none",
                "metric": "sum_revenue",
                "ranking": "top_5",
                "ambiguity": "clear",
            }
        },
        # Case 5: Hierarchical Deduplication (ASIA + JAPAN)
        "アジアの日本の顧客残高": {
            "results": {
                "primary_table": "customer",
                "secondary_table": "nation",
                "dimension": "c_name",
                "metric": "sum_acctbal",
                "ranking": "none",
                "ambiguity": "clear",
                "filters": ["ASIA", "JAPAN"],
            }
        },
    }

    default_response = {
        "results": {
            "primary_table": "customer",
            "secondary_table": "none",
            "dimension": "c_name",
            "metric": "count_records",
            "ranking": "none",
            "ambiguity": "clear",
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        eval_state = body.get("state", body.get("eval_state", ""))

        # Match longest, most specific keys first to avoid premature prefix matches
        for key in sorted(mock_responses.keys(), key=len, reverse=True):
            if key in eval_state:
                return httpx.Response(200, json=mock_responses[key])

        return httpx.Response(200, json=default_response)

    client = JevClient(api_key="ts_test_key_integration")
    client._client = httpx.Client(
        base_url="https://api.typesafe.ai",
        transport=httpx.MockTransport(handler),
    )
    return client


# ==============================================================================
# 1. TPC-H Representative 5 Cases E2E Tests
# ==============================================================================


class TestTPCHRepresentativeE2E:
    """End-to-End integration tests for 5 representative TPC-H benchmark query patterns."""

    def test_case1_customer_aggregation_multi_hop_join(
        self, tpch_schema: SchemaMetadata
    ) -> None:
        """Case 1: Customer aggregation across customer -> nation -> region."""
        client = create_tpch_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)

        result = engine.process_query("アジア地域の顧客残高集計", dialect="sqlite")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None
        sql = result.sql

        # Verify semantic AST nodes and joins
        assert "customer" in sql
        assert "nation" in sql
        assert "region" in sql
        assert "c_name" in sql
        assert "total_acctbal" in sql
        assert "r_name = 'ASIA'" in sql
        assert "GROUP BY" in sql

        # Parse with sqlglot to guarantee syntactic validity
        parsed = sqlglot.parse_one(sql, read="sqlite")
        assert parsed is not None

    def test_case2_revenue_ranking_limit_and_order_by(
        self, tpch_schema: SchemaMetadata
    ) -> None:
        """Case 2: Top 5 customer revenue ranking with ORDER BY and LIMIT."""
        client = create_tpch_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)

        result = engine.process_query("売上上位5件の顧客と注文情報", dialect="sqlite")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None
        sql = result.sql

        # Verify tables and ranking semantics
        assert "customer" in sql
        assert "orders" in sql
        assert "total_revenue" in sql
        assert "ORDER BY" in sql
        assert "DESC" in sql
        assert "LIMIT 5" in sql

        parsed = sqlglot.parse_one(sql, read="sqlite")
        assert parsed is not None

    def test_case3_inventory_shortage_inequality_filter(
        self, tpch_schema: SchemaMetadata
    ) -> None:
        """Case 3: Inventory shortage query with inequality numeric filter (ps_availqty <= 10)."""
        client = create_tpch_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)

        result = engine.process_query("在庫数10以下の部品一覧", dialect="sqlite")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None
        sql = result.sql

        # Verify joins and numeric inequality filter
        assert "part" in sql
        assert "partsupp" in sql
        assert "ps_availqty <= 10" in sql
        assert "p_name" in sql

        parsed = sqlglot.parse_one(sql, read="sqlite")
        assert parsed is not None

    def test_case4_ambiguous_query_hitl_and_resolution(
        self, tpch_schema: SchemaMetadata
    ) -> None:
        """Case 4: Ambiguous query triggers HITL and resolves via slash syntax or --clarify."""
        client = create_tpch_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)

        # 4.1 First invocation triggers HITL intervention request
        result_hitl = engine.process_query("売上トップ5", dialect="sqlite")
        assert result_hitl.status == EngineStatus.HITL_REQUIRED
        assert result_hitl.sql is None
        assert result_hitl.hitl_issue is not None
        assert len(result_hitl.hitl_issue.options) >= 2
        assert result_hitl.hitl_issue.options[0].key == "A"
        assert result_hitl.hitl_issue.options[1].key == "B"

        # 4.2 Disambiguation via slash syntax: "/ Aで" -> lineitem
        result_resolved_slash = engine.process_query("売上トップ5 / Aで", dialect="sqlite")
        assert result_resolved_slash.status == EngineStatus.SUCCESS
        assert result_resolved_slash.sql is not None
        assert "lineitem" in result_resolved_slash.sql
        assert "total_revenue" in result_resolved_slash.sql
        assert "LIMIT 5" in result_resolved_slash.sql

        # 4.3 Disambiguation via --clarify parameter: clarify="B" -> orders
        result_resolved_clarify = engine.process_query(
            "売上トップ5", clarify="B", dialect="sqlite"
        )
        assert result_resolved_clarify.status == EngineStatus.SUCCESS
        assert result_resolved_clarify.sql is not None
        assert "orders" in result_resolved_clarify.sql
        assert "total_revenue" in result_resolved_clarify.sql

    def test_case5_parent_child_ontology_deduplication(
        self, tpch_schema: SchemaMetadata
    ) -> None:
        """Case 5: Deduplication prunes parent region when child nation is specified."""
        client = create_tpch_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)

        result = engine.process_query("アジアの日本の顧客残高", dialect="sqlite")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None
        sql = result.sql

        # Nation should be retained, region table and filter pruned
        assert "customer" in sql
        assert "nation" in sql
        assert "n_name = 'JAPAN'" in sql
        # Assert redundant parent region join and filter are excluded
        assert "JOIN region" not in sql
        assert "r_name" not in sql

        parsed = sqlglot.parse_one(sql, read="sqlite")
        assert parsed is not None


# ==============================================================================
# 2. Real Database (SQLite / DuckDB) Execution Verification
# ==============================================================================


class TestRealDatabaseExecution:
    """Verifies that generated SQL compiles and executes flawlessly against actual RDBMS engines."""

    def test_sqlite_execution_all_representative_cases(
        self, tpch_schema: SchemaMetadata, sqlite_db: sqlite3.Connection
    ) -> None:
        """Execute generated SQLite SQL for all representative cases against in-memory SQLite."""
        client = create_tpch_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)
        cursor = sqlite_db.cursor()

        queries_to_test = [
            ("アジア地域の顧客残高集計", None),
            ("売上上位5件の顧客と注文情報", None),
            ("在庫数10以下の部品一覧", None),
            ("売上トップ5 / Aで", None),
            ("アジアの日本の顧客残高", None),
        ]

        for raw_q, clarify in queries_to_test:
            result = engine.process_query(raw_q, clarify=clarify, dialect="sqlite")
            assert (
                result.status == EngineStatus.SUCCESS
            ), f"Failed for query '{raw_q}': {result.error_message}"
            assert result.sql is not None

            # Execute against actual SQLite instance
            cursor.execute(result.sql)
            rows = cursor.fetchall()
            # Must return results without syntax, column, or table errors
            assert isinstance(rows, list)
            assert len(rows) > 0, f"Query '{raw_q}' returned no rows from seeded SQLite DB"

    @pytest.mark.skipif(not HAS_DUCKDB, reason="duckdb is not installed in the environment")
    def test_duckdb_execution_all_representative_cases(
        self, tpch_schema: SchemaMetadata
    ) -> None:
        """Execute generated DuckDB SQL for all representative cases if duckdb is installed."""
        client = create_tpch_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)

        conn = duckdb.connect(":memory:")
        for ddl in TPCH_DDL_STATEMENTS:
            conn.execute(ddl)
        for seed in TPCH_SEED_STATEMENTS:
            conn.execute(seed)

        queries_to_test = [
            ("アジア地域の顧客残高集計", None),
            ("売上上位5件の顧客と注文情報", None),
            ("在庫数10以下の部品一覧", None),
            ("売上トップ5 / Aで", None),
            ("アジアの日本の顧客残高", None),
        ]

        for raw_q, clarify in queries_to_test:
            result = engine.process_query(raw_q, clarify=clarify, dialect="duckdb")
            assert (
                result.status == EngineStatus.SUCCESS
            ), f"Failed for query '{raw_q}': {result.error_message}"
            assert result.sql is not None

            rows = conn.execute(result.sql).fetchall()
            assert isinstance(rows, list)
            assert len(rows) > 0


# ==============================================================================
# 3. Performance Benchmark Verification
# ==============================================================================


class TestPerformanceBenchmark:
    """Validates ultra-low latency guarantees for deterministic AST compilation."""

    def test_ast_build_latency_under_10ms(self, tpch_schema: SchemaMetadata) -> None:
        """Verify each query's ast_build latency metric is strictly less than 10.0 ms."""
        client = create_tpch_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)

        # Warm up once to initialize internal caches / sqlglot tables
        engine.process_query("アジア地域の顧客残高集計", dialect="postgres")

        # Measure steady-state latency
        result = engine.process_query("アジア地域の顧客残高集計", dialect="postgres")

        assert result.status == EngineStatus.SUCCESS
        assert result.latency_ms.ast_build < 10.0, (
            f"AST build latency {result.latency_ms.ast_build:.2f}ms exceeded 10.0ms threshold"
        )

    def test_ast_build_100_runs_average_under_5ms(
        self, tpch_schema: SchemaMetadata
    ) -> None:
        """Execute 100 consecutive compilations and assert average AST build time is under 5ms."""
        client = create_tpch_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_schema, jev_client=client)

        # Warm up once
        engine.process_query("売上上位5件の顧客と注文情報", dialect="postgres")

        runs = 100
        ast_times: list[float] = []

        for _ in range(runs):
            res = engine.process_query("売上上位5件の顧客と注文情報", dialect="postgres")
            assert res.status == EngineStatus.SUCCESS
            ast_times.append(res.latency_ms.ast_build)

        avg_ast_ms = sum(ast_times) / runs

        # Assert strict performance budget specified in requirements (<5ms average)
        assert avg_ast_ms < 5.0, (
            f"Average AST build latency {avg_ast_ms:.3f}ms exceeded 5.0ms threshold over 100 runs"
        )


# ==============================================================================
# 4. Package CLI Executable Verification
# ==============================================================================


class _MockJevRequestHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP request handler serving deterministic Jev System 1 responses."""

    def do_POST(self) -> None:
        content_len = int(self.headers.get("Content-Length", 0))
        _ = json.loads(self.rfile.read(content_len).decode("utf-8")) if content_len > 0 else {}

        # Default deterministic response for CLI test query "日本の顧客"
        response_payload = {
            "results": {
                "primary_table": "customer",
                "secondary_table": "nation",
                "dimension": "c_name",
                "metric": "count_records",
                "ranking": "none",
                "ambiguity": "clear",
                "filters": ["JAPAN"],
            }
        }

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response_payload).encode("utf-8"))

    def log_message(self, *_args: Any) -> None:
        """Suppress default HTTP server stderr output in tests."""
        return


@pytest.fixture
def mock_jev_http_server() -> Any:
    """Launch a lightweight in-process HTTP mock server for CLI subprocess testing."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _MockJevRequestHandler)
    port = server.server_port
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    server_url = f"http://127.0.0.1:{port}"
    yield server_url

    server.shutdown()
    server.server_close()


class TestPackageCLICommand:
    """Validates the installed CLI executable end-to-end via subprocess execution."""

    def test_installed_cli_executable_e2e_subprocess(
        self, mock_jev_http_server: str
    ) -> None:
        """Run CLI query command and verify exit code 0 and valid JSON."""
        env = os.environ.copy()
        env["TYPESAFE_BASE_URL"] = mock_jev_http_server
        env["TYPESAFE_API_KEY"] = "ts_mock_key_cli_test"
        env["PYTHONIOENCODING"] = "utf-8"

        cli_executable = shutil.which("deterministic-text-to-sql")
        if cli_executable is not None:
            cli_cmd = [
                cli_executable,
                "query",
                "日本の顧客",
                "--schema",
                str(FIXTURE_PATH),
                "--json",
            ]
        else:
            cli_cmd = [
                sys.executable,
                "-m",
                "deterministic_text_to_sql.cli",
                "query",
                "日本の顧客",
                "--schema",
                str(FIXTURE_PATH),
                "--json",
            ]

        proc = subprocess.run(
            cli_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=15,
        )

        assert (
            proc.returncode == 0
        ), f"CLI command failed ({proc.returncode}). Stderr: {proc.stderr}\nStdout: {proc.stdout}"

        # Parse JSON output from stdout
        output_json = json.loads(proc.stdout)
        assert isinstance(output_json, dict)
        assert output_json["status"] == "success"
        assert "query" in output_json
        assert output_json["sql"] is not None
        assert "customer" in output_json["sql"]
        assert "n_name = 'JAPAN'" in output_json["sql"]
        assert output_json["latency_ms"]["total"] > 0.0
