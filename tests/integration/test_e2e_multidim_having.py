"""Detroit-style End-to-End integration tests for multi-dimensional aggregation,
temporal analytics, and HAVING filters (TASK-26).

Validates the complete end-to-end integration of:
1. Scenario 1: Multi-axis cross-aggregation ("年別かつ国別の注文金額の集計")
   - Shortest join paths across fact (lineitem) to orders and nation
   - SELECT and GROUP BY with EXTRACT(year FROM orders.o_orderdate) and nation.n_name
   - Real execution on SQLite and DuckDB yielding non-empty rows.
2. Scenario 2: Aggregate HAVING filter integration ("売上1000万以上の年別集計")
   - HAVING SUM(...) >= 10000000 strictly placed in HAVING clause, not WHERE
   - Real execution against seeded DB verifying aggregate threshold filtering.
3. Scenario 3: Temporal granularity (monthly) with date range filter ("1995年の月別売上集計")
   - orders_month extraction with WHERE date range filter
   - Real execution against seeded DB.
4. Scenario 4: Installed package CLI executable verification via subprocess
   - CLI execution with --schema examples/tpch/tpch_semantic_model.yaml --json
   - Zero exit code and valid structured JSON output.
5. Scenario 5: Ultra-low latency performance benchmark
   - Multi-dim + HAVING AST compilation strictly < 10ms.
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

from deterministic_text_to_sql import (
    DeterministicTextToSQLEngine,
    EngineStatus,
)
from deterministic_text_to_sql.jev.client import JevClient
from deterministic_text_to_sql.schema.models import SchemaMetadata
from deterministic_text_to_sql.schema.ossie import OssieSchemaAdapter

SCHEMA_PATH = Path("examples/tpch/tpch_semantic_model.yaml")

try:
    import duckdb  # type: ignore

    HAS_DUCKDB = True
except ImportError:
    HAS_DUCKDB = False


# ==============================================================================
# Full TPC-H 8 Tables DDL & Seed Data (with temporal and high-revenue rows)
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
        p_partkey BIGINT PRIMARY KEY,
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
        ps_availqty INTEGER NOT NULL,
        ps_supplycost DECIMAL(15,2) NOT NULL,
        ps_comment VARCHAR(199),
        PRIMARY KEY (ps_partkey, ps_suppkey)
    );
    """,
    """
    CREATE TABLE orders (
        o_orderkey BIGINT PRIMARY KEY,
        o_custkey BIGINT NOT NULL REFERENCES customer(c_custkey),
        o_orderstatus CHAR(1) NOT NULL,
        o_totalprice DECIMAL(15,2) NOT NULL,
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
        l_linenumber INTEGER NOT NULL,
        l_quantity DECIMAL(15,2) NOT NULL,
        l_extendedprice DECIMAL(15,2) NOT NULL,
        l_discount DECIMAL(15,2) NOT NULL,
        l_tax DECIMAL(15,2) NOT NULL,
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
        "5000.0, 'Primary supplier in Japan');"
    ),
    (
        "INSERT INTO part (p_partkey, p_name, p_mfgr, p_brand, p_type, p_size, "
        "p_container, p_retailprice, p_comment) "
        "VALUES (201, 'Part#201', 'MFGR#1', 'Brand#1', 'PROMO BURNISHED COPPER', "
        "10, 'JUMBO BOX', 900.0, 'Essential metal part');"
    ),
    (
        "INSERT INTO partsupp (ps_partkey, ps_suppkey, ps_availqty, ps_supplycost, ps_comment) "
        "VALUES (201, 1, 5, 450.0, 'Low inventory stock');"
    ),
    # Row 1: Order in 2026
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
    # Row 2: Order in 1995 with revenue >= 10,000,000 (16,000,000 * 0.95 = 15,200,000)
    (
        "INSERT INTO orders (o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
        "o_orderpriority, o_clerk, o_shippriority, o_comment) "
        "VALUES (102, 1, 'O', 15200000.0, '1995-03-15', '1-URGENT', 'Clerk#002', 0, "
        "'Large order 1995');"
    ),
    (
        "INSERT INTO lineitem (l_orderkey, l_partkey, l_suppkey, l_linenumber, l_quantity, "
        "l_extendedprice, l_discount, l_tax, l_returnflag, l_linestatus, l_shipdate, "
        "l_commitdate, l_receiptdate, l_shipinstruct, l_shipmode, l_comment) "
        "VALUES (102, 201, 1, 1, 100.0, 16000000.0, 0.05, 0.08, 'N', 'O', '1995-03-20', "
        "'1995-03-25', '1995-03-26', 'DELIVER IN PERSON', 'AIR', 'Large 1995 lineitem');"
    ),
    # Row 3: Order in 2026 with high revenue (25,000,000 * 0.90 = 22,500,000)
    (
        "INSERT INTO orders (o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
        "o_orderpriority, o_clerk, o_shippriority, o_comment) "
        "VALUES (103, 1, 'O', 22500000.0, '2026-07-10', '2-HIGH', 'Clerk#003', 0, "
        "'High revenue 2026');"
    ),
    (
        "INSERT INTO lineitem (l_orderkey, l_partkey, l_suppkey, l_linenumber, l_quantity, "
        "l_extendedprice, l_discount, l_tax, l_returnflag, l_linestatus, l_shipdate, "
        "l_commitdate, l_receiptdate, l_shipinstruct, l_shipmode, l_comment) "
        "VALUES (103, 201, 1, 1, 50.0, 25000000.0, 0.10, 0.08, 'N', 'O', '2026-07-15', "
        "'2026-07-20', '2026-07-21', 'TAKE BACK RETURN', 'MAIL', 'High 2026 lineitem');"
    ),
]


# ==============================================================================
# Jev Mock Client for Multi-Dimensional & HAVING Queries
# ==============================================================================


def create_multidim_having_mock_client() -> JevClient:
    """Create a JevClient backed by httpx.MockTransport for TASK-26 scenarios."""
    mock_responses: dict[str, dict[str, Any]] = {
        # Scenario 1: Multi-axis aggregation (year + nation)
        "年別かつ国別の注文金額の集計": {
            "results": {
                "primary_table": "lineitem",
                "secondary_table": "orders",
                "dimensions": ["orders_year", "nation.n_name"],
                "metric": "total_revenue",
                "ranking": "none",
                "ambiguity": "clear",
                "filters": [],
            }
        },
        # Scenario 1 variant (synonym test)
        "年別かつ国別の売上": {
            "results": {
                "primary_table": "lineitem",
                "secondary_table": "orders",
                "dimensions": ["orders_year", "nation.n_name"],
                "metric": "total_revenue",
                "ranking": "none",
                "ambiguity": "clear",
                "filters": [],
            }
        },
        # Scenario 2: HAVING aggregate filter
        "売上1000万以上の年別集計": {
            "results": {
                "primary_table": "lineitem",
                "secondary_table": "orders",
                "dimensions": ["orders_year"],
                "metric": "total_revenue",
                "ranking": "none",
                "ambiguity": "clear",
                "having_slots": [
                    {
                        "metric": "total_revenue",
                        "operator": ">=",
                        "value": 10000000,
                    }
                ],
                "filters": [],
            }
        },
        # Scenario 3: Monthly temporal granularity with date filter
        "1995年の月別売上集計": {
            "results": {
                "primary_table": "lineitem",
                "secondary_table": "orders",
                "dimensions": ["orders_month"],
                "metric": "total_revenue",
                "ranking": "none",
                "ambiguity": "clear",
                "filter_slots": [
                    {
                        "table": "orders",
                        "column": "o_orderdate",
                        "operator": ">=",
                        "value": "1995-01-01",
                    },
                    {
                        "table": "orders",
                        "column": "o_orderdate",
                        "operator": "<=",
                        "value": "1995-12-31",
                    },
                ],
                "filters": [],
            }
        },
    }

    default_response = {
        "results": {
            "primary_table": "lineitem",
            "secondary_table": "orders",
            "dimensions": ["orders_year", "nation.n_name"],
            "metric": "total_revenue",
            "ranking": "none",
            "ambiguity": "clear",
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        eval_state = body.get("state", body.get("eval_state", ""))

        for key in sorted(mock_responses.keys(), key=len, reverse=True):
            if key in eval_state:
                return httpx.Response(200, json=mock_responses[key])

        return httpx.Response(200, json=default_response)

    client = JevClient(api_key="ts_test_key_multidim")
    client._client = httpx.Client(
        base_url="https://api.typesafe.ai",
        transport=httpx.MockTransport(handler),
    )
    return client


# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture(scope="session")
def tpch_ossie_schema() -> SchemaMetadata:
    """Load TPC-H Ossie semantic model and verify auto-extracted time dimensions."""
    assert SCHEMA_PATH.exists(), f"Schema file not found at {SCHEMA_PATH}"
    schema = OssieSchemaAdapter.load_from_yaml(SCHEMA_PATH)

    # Ensure required tables exist
    assert schema.get_table("lineitem") is not None
    assert schema.get_table("orders") is not None
    assert schema.get_table("nation") is not None

    # Ensure semantic metric total_revenue exists
    assert schema.get_metric("total_revenue") is not None

    # Ensure time dimensions orders_year and orders_month are automatically extracted
    year_dim = schema.get_dimension("orders_year")
    assert year_dim is not None, "orders_year dimension was not auto-extracted"
    assert year_dim.is_time is True
    assert year_dim.time_granularity == "year"
    assert year_dim.expression is not None
    assert "EXTRACT(year FROM orders.o_orderdate)" in year_dim.expression

    month_dim = schema.get_dimension("orders_month")
    assert month_dim is not None, "orders_month dimension was not auto-extracted"
    assert month_dim.is_time is True
    assert month_dim.time_granularity == "month"

    return schema


@pytest.fixture
def sqlite_seeded_db() -> Generator[sqlite3.Connection, None, None]:
    """Provide an in-memory SQLite database seeded with TPC-H tables and records."""
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    for ddl in TPCH_DDL_STATEMENTS:
        cursor.execute(ddl)
    for seed in TPCH_SEED_STATEMENTS:
        cursor.execute(seed)
    conn.commit()

    yield conn

    conn.close()


# ==============================================================================
# Scenario 1: Multi-Axis Cross-Aggregation ("年別かつ国別の注文金額の集計")
# ==============================================================================


class TestScenario1MultiDimCrossAggregation:
    """Verifies user problem: multi-axis grouping across time and geography."""

    def test_multidim_year_nation_ast_and_sql_synthesis(
        self, tpch_ossie_schema: SchemaMetadata
    ) -> None:
        """Verify AST generation with orders_year, nation.n_name, and shortest JOINs."""
        client = create_multidim_having_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("年別かつ国別の注文金額の集計", dialect="postgres")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None
        sql = result.sql

        # 1. SELECT clause verification
        assert "EXTRACT(YEAR FROM orders.o_orderdate) AS orders_year" in sql
        assert "nation.n_name" in sql
        assert "AS total_revenue" in sql

        # 2. FROM and JOIN path verification (lineitem -> orders and lineitem -> supplier -> nation)
        assert "FROM lineitem" in sql
        assert "JOIN orders" in sql
        assert "JOIN supplier" in sql
        assert "JOIN nation" in sql

        # 3. GROUP BY clause verification
        assert "GROUP BY" in sql
        assert "EXTRACT(YEAR FROM orders.o_orderdate)" in sql.split("GROUP BY")[1]
        assert "nation.n_name" in sql.split("GROUP BY")[1]

    def test_multidim_year_nation_sqlite_execution(
        self,
        tpch_ossie_schema: SchemaMetadata,
        sqlite_seeded_db: sqlite3.Connection,
    ) -> None:
        """Execute synthesized SQLite SQL against in-memory SQLite and verify rows returned."""
        client = create_multidim_having_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("年別かつ国別の注文金額の集計", dialect="sqlite")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None

        # Verify SQLite transpilation uses STRFTIME
        assert "STRFTIME('%Y', orders.o_orderdate)" in result.sql

        cursor = sqlite_seeded_db.cursor()
        cursor.execute(result.sql)
        rows = cursor.fetchall()

        # Must successfully return rows without SQLite syntax error
        assert isinstance(rows, list)
        assert len(rows) >= 2, f"Expected at least 2 aggregated rows (1995, 2026), got {len(rows)}"

        # Verify year and nation values in result rows
        years = {row[0] for row in rows}
        assert "1995" in years or 1995 in years
        assert "2026" in years or 2026 in years
        nations = {row[1].strip() if isinstance(row[1], str) else row[1] for row in rows}
        assert "JAPAN" in nations

    @pytest.mark.skipif(not HAS_DUCKDB, reason="duckdb is not installed in the environment")
    def test_multidim_year_nation_duckdb_execution(
        self, tpch_ossie_schema: SchemaMetadata
    ) -> None:
        """Execute synthesized DuckDB SQL against DuckDB instance."""
        client = create_multidim_having_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("年別かつ国別の注文金額の集計", dialect="duckdb")
        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None

        conn = duckdb.connect(":memory:")
        for ddl in TPCH_DDL_STATEMENTS:
            conn.execute(ddl)
        for seed in TPCH_SEED_STATEMENTS:
            conn.execute(seed)

        rows = conn.execute(result.sql).fetchall()
        assert len(rows) >= 2


# ==============================================================================
# Scenario 2: HAVING Aggregate Filter Integration ("売上1000万以上の年別集計")
# ==============================================================================


class TestScenario2HavingFilterIntegration:
    """Verifies type-safe HAVING filter assembly and execution against real database."""

    def test_having_filter_ast_synthesis(
        self, tpch_ossie_schema: SchemaMetadata
    ) -> None:
        """Verify HAVING SUM(...) >= 10000000 is placed strictly in HAVING clause."""
        client = create_multidim_having_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("売上1000万以上の年別集計", dialect="postgres")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None
        sql = result.sql

        # Verify HAVING is present and WHERE is NOT present (no row-level filter)
        assert "HAVING" in sql
        assert "WHERE" not in sql

        # Verify HAVING contains metric calculation with threshold (ignoring whitespace)
        having_part = sql.split("HAVING")[1]
        having_compact = "".join(having_part.split())
        assert "SUM(lineitem.l_extendedprice*(1-lineitem.l_discount))>=10000000" in having_compact

        # Verify GROUP BY is present
        assert "GROUP BY" in sql
        assert "EXTRACT(YEAR FROM orders.o_orderdate)" in sql

    def test_having_filter_sqlite_execution(
        self,
        tpch_ossie_schema: SchemaMetadata,
        sqlite_seeded_db: sqlite3.Connection,
    ) -> None:
        """Execute HAVING query against SQLite and verify rows exceeding 10M are returned."""
        client = create_multidim_having_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("売上1000万以上の年別集計", dialect="sqlite")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None

        cursor = sqlite_seeded_db.cursor()
        cursor.execute(result.sql)
        rows = cursor.fetchall()

        # Both 1995 (15.2M) and 2026 (22.5M) exceed 10M, so both should be returned
        assert len(rows) == 2, f"Expected 2 rows exceeding 10,000,000, got: {rows}"
        for row in rows:
            # row: (year, total_revenue)
            revenue = float(row[1])
            assert revenue >= 10000000.0, f"Row revenue {revenue} did not satisfy >= 10000000"

    @pytest.mark.skipif(not HAS_DUCKDB, reason="duckdb is not installed in the environment")
    def test_having_filter_duckdb_execution(
        self, tpch_ossie_schema: SchemaMetadata
    ) -> None:
        """Execute HAVING query against DuckDB."""
        client = create_multidim_having_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("売上1000万以上の年別集計", dialect="duckdb")
        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None

        conn = duckdb.connect(":memory:")
        for ddl in TPCH_DDL_STATEMENTS:
            conn.execute(ddl)
        for seed in TPCH_SEED_STATEMENTS:
            conn.execute(seed)

        rows = conn.execute(result.sql).fetchall()
        assert len(rows) == 2


# ==============================================================================
# Scenario 3: Monthly Granularity with Date Range Filter ("1995年の月別売上集計")
# ==============================================================================


class TestScenario3TemporalGranularityWithFilter:
    """Verifies monthly time dimension combined with row-level date filter."""

    def test_monthly_granularity_and_date_filter_ast(
        self, tpch_ossie_schema: SchemaMetadata
    ) -> None:
        """Verify orders_month in GROUP BY and orders.o_orderdate range filter in WHERE."""
        client = create_multidim_having_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("1995年の月別売上集計", dialect="postgres")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None
        sql = result.sql

        # Verify month extraction in SELECT and GROUP BY
        assert "EXTRACT(MONTH FROM orders.o_orderdate) AS orders_month" in sql
        assert "GROUP BY" in sql
        assert "EXTRACT(MONTH FROM orders.o_orderdate)" in sql.split("GROUP BY")[1]

        # Verify WHERE date filters
        assert "WHERE" in sql
        where_part = sql.split("WHERE")[1].split("GROUP BY")[0]
        assert "orders.o_orderdate >= '1995-01-01'" in where_part
        assert "orders.o_orderdate <= '1995-12-31'" in where_part

    def test_monthly_granularity_sqlite_execution(
        self,
        tpch_ossie_schema: SchemaMetadata,
        sqlite_seeded_db: sqlite3.Connection,
    ) -> None:
        """Execute monthly query against SQLite and verify that 1995 month 3 is returned."""
        client = create_multidim_having_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("1995年の月別売上集計", dialect="sqlite")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None

        cursor = sqlite_seeded_db.cursor()
        cursor.execute(result.sql)
        rows = cursor.fetchall()

        # Should return exactly 1 row (Month '03' of 1995)
        assert len(rows) == 1, f"Expected 1 row for 1995-03, got {len(rows)} rows: {rows}"
        month_val = str(rows[0][0])
        assert month_val in ("03", "3")
        revenue = float(rows[0][1])
        assert revenue == 15200000.0


# ==============================================================================
# Scenario 4: Installed Package CLI Subprocess Execution Verification
# ==============================================================================


class _MockJevMultidimRequestHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler serving deterministic Jev System 1 responses for CLI testing."""

    def do_POST(self) -> None:
        content_len = int(self.headers.get("Content-Length", 0))
        _ = json.loads(self.rfile.read(content_len).decode("utf-8")) if content_len > 0 else {}

        response_payload = {
            "results": {
                "primary_table": "lineitem",
                "secondary_table": "orders",
                "dimensions": ["orders_year", "nation.n_name"],
                "metric": "total_revenue",
                "ranking": "none",
                "ambiguity": "clear",
                "filters": [],
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
def mock_jev_http_server() -> Generator[str, None, None]:
    """Launch a lightweight in-process HTTP mock server for CLI subprocess testing."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _MockJevMultidimRequestHandler)
    port = server.server_port
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    server_url = f"http://127.0.0.1:{port}"
    yield server_url

    server.shutdown()
    server.server_close()


class TestScenario4PackageCLICommand:
    """Validates CLI executable end-to-end via subprocess execution with multi-dim query."""

    def test_installed_cli_multidim_subprocess(
        self, mock_jev_http_server: str
    ) -> None:
        """Run CLI query command for multi-axis aggregation and verify JSON output."""
        env = os.environ.copy()
        env["TYPESAFE_BASE_URL"] = mock_jev_http_server
        env["TYPESAFE_API_KEY"] = "ts_mock_key_cli_test"
        env["PYTHONIOENCODING"] = "utf-8"

        cli_executable = shutil.which("deterministic-text-to-sql")
        if cli_executable is not None:
            cli_cmd = [
                cli_executable,
                "query",
                "年別かつ国別の売上",
                "--schema",
                str(SCHEMA_PATH),
                "--json",
            ]
        else:
            cli_cmd = [
                sys.executable,
                "-m",
                "deterministic_text_to_sql.cli",
                "query",
                "年別かつ国別の売上",
                "--schema",
                str(SCHEMA_PATH),
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

        output_json = json.loads(proc.stdout)
        assert isinstance(output_json, dict)
        assert output_json["status"] == "success"
        assert output_json["sql"] is not None

        sql = output_json["sql"]
        assert "orders_year" in sql or "o_orderdate" in sql
        assert "nation" in sql or "n_name" in sql
        assert "total_revenue" in sql or "SUM" in sql
        assert "GROUP BY" in sql


# ==============================================================================
# Scenario 5: Performance Benchmark Verification
# ==============================================================================


class TestScenario5PerformanceBenchmark:
    """Validates ultra-low latency guarantees for multi-dimensional and HAVING AST compilation."""

    def test_multidim_having_ast_build_latency_under_10ms(
        self, tpch_ossie_schema: SchemaMetadata
    ) -> None:
        """Verify multi-dim + HAVING ast_build latency metric is strictly less than 10.0 ms."""
        client = create_multidim_having_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        # Warm up once
        engine.process_query("売上1000万以上の年別集計", dialect="postgres")

        # Measure steady-state latency
        result = engine.process_query("売上1000万以上の年別集計", dialect="postgres")

        assert result.status == EngineStatus.SUCCESS
        assert result.latency_ms.ast_build < 10.0, (
            f"AST build latency {result.latency_ms.ast_build:.2f}ms exceeded 10.0ms threshold"
        )

    def test_multidim_having_ast_build_100_runs_average_under_10ms(
        self, tpch_ossie_schema: SchemaMetadata
    ) -> None:
        """Execute 100 compilations and assert average AST build time is strictly under 10ms."""
        client = create_multidim_having_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        # Warm up once
        engine.process_query("年別かつ国別の注文金額の集計", dialect="postgres")

        runs = 100
        ast_times: list[float] = []

        for _ in range(runs):
            res = engine.process_query("年別かつ国別の注文金額の集計", dialect="postgres")
            assert res.status == EngineStatus.SUCCESS
            ast_times.append(res.latency_ms.ast_build)

        avg_ast_ms = sum(ast_times) / runs

        assert avg_ast_ms < 10.0, (
            f"Average AST build latency {avg_ast_ms:.3f}ms exceeded 10.0ms threshold over 100 runs"
        )
