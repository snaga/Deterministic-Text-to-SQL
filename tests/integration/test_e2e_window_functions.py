"""Detroit-style End-to-End integration tests for Level 2 Window Functions and CTEs (TASK-30).

Validates the complete end-to-end integration of:
1. Scenario 1: Intra-group Top-N cross-aggregation ("地域ごとの売上トップ3顧客")
   - Window operation partitioned_rank slot extraction
   - AST generation: WITH ranked_base AS (...) SELECT ... WHERE rank_num <= 3
   - Real SQLite execution verifying top-3 customer extraction partitioned by region
2. Scenario 2: Temporal shift / YoY analysis ("年別売上および前年比（YoY）")
   - Window operation time_shift slot extraction (LAG)
   - AST generation: WITH time_series_base AS (...) SELECT ..., LAG(...)
   - Real SQLite execution verifying NULL for first year and exact YoY growth rate calculation
3. Scenario 3: Installed package CLI executable verification via subprocess
   - CLI execution: query "地域ごとの売上トップ3顧客"
     --schema examples/tpch/tpch_semantic_model.yaml --json
   - Zero exit code, success status, and CTE + window SQL presence
4. Scenario 4: Ultra-low latency performance benchmark
   - 2-pass CTE AST compilation latency < 10ms (steady state)
   - 100 runs average AST build latency < 5ms
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


# ==============================================================================
# Full TPC-H DDL & Seed Data for Window Function Scenarios
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
    # Regions: AMERICA (1), ASIA (2)
    (
        "INSERT INTO region (r_regionkey, r_name, r_comment) "
        "VALUES (1, 'AMERICA', 'American continent');"
    ),
    (
        "INSERT INTO region (r_regionkey, r_name, r_comment) "
        "VALUES (2, 'ASIA', 'Asian continent');"
    ),
    # Nations: UNITED STATES (1 in AMERICA), JAPAN (12 in ASIA)
    (
        "INSERT INTO nation (n_nationkey, n_name, n_regionkey, n_comment) "
        "VALUES (1, 'UNITED STATES', 1, 'North America');"
    ),
    (
        "INSERT INTO nation (n_nationkey, n_name, n_regionkey, n_comment) "
        "VALUES (12, 'JAPAN', 2, 'Land of the rising sun');"
    ),
    # Supplier in Japan
    (
        "INSERT INTO supplier (s_suppkey, s_name, s_address, s_nationkey, s_phone, "
        "s_acctbal, s_comment) "
        "VALUES (1, 'Supplier#000000001', 'Tokyo, Japan', 12, '81-3-1111-2222', "
        "1000.0, 'Global supplier');"
    ),
    # Part & Partsupp
    (
        "INSERT INTO part (p_partkey, p_name, p_mfgr, p_brand, p_type, p_size, "
        "p_container, p_retailprice, p_comment) "
        "VALUES (201, 'Part#201', 'MFGR#1', 'Brand#1', 'STANDARD', 10, 'BOX', 500.0, "
        "'Standard part');"
    ),
    (
        "INSERT INTO partsupp (ps_partkey, ps_suppkey, ps_availqty, ps_supplycost, ps_comment) "
        "VALUES (201, 1, 100, 250.0, 'In stock');"
    ),
    # ASIA Customers (4 customers)
    (
        "INSERT INTO customer (c_custkey, c_name, c_address, c_nationkey, c_phone, "
        "c_acctbal, c_mktsegment, c_comment) "
        "VALUES (1, 'Customer#0001_Asia1', 'Tokyo', 12, '81-3-1', 1000.0, 'BUILDING', "
        "'Asia rank 1');"
    ),
    (
        "INSERT INTO customer (c_custkey, c_name, c_address, c_nationkey, c_phone, "
        "c_acctbal, c_mktsegment, c_comment) "
        "VALUES (2, 'Customer#0002_Asia2', 'Osaka', 12, '81-6-2', 1000.0, 'BUILDING', "
        "'Asia rank 2');"
    ),
    (
        "INSERT INTO customer (c_custkey, c_name, c_address, c_nationkey, c_phone, "
        "c_acctbal, c_mktsegment, c_comment) "
        "VALUES (3, 'Customer#0003_Asia3', 'Kyoto', 12, '81-75-3', 1000.0, 'BUILDING', "
        "'Asia rank 3');"
    ),
    (
        "INSERT INTO customer (c_custkey, c_name, c_address, c_nationkey, c_phone, "
        "c_acctbal, c_mktsegment, c_comment) "
        "VALUES (4, 'Customer#0004_Asia4', 'Nagoya', 12, '81-52-4', 1000.0, 'BUILDING', "
        "'Asia rank 4');"
    ),
    # AMERICA Customers (4 customers)
    (
        "INSERT INTO customer (c_custkey, c_name, c_address, c_nationkey, c_phone, "
        "c_acctbal, c_mktsegment, c_comment) "
        "VALUES (5, 'Customer#0005_Amer1', 'New York', 1, '1-212-5', 2000.0, 'AUTOMOBILE', "
        "'Amer rank 1');"
    ),
    (
        "INSERT INTO customer (c_custkey, c_name, c_address, c_nationkey, c_phone, "
        "c_acctbal, c_mktsegment, c_comment) "
        "VALUES (6, 'Customer#0006_Amer2', 'Chicago', 1, '1-312-6', 2000.0, 'AUTOMOBILE', "
        "'Amer rank 2');"
    ),
    (
        "INSERT INTO customer (c_custkey, c_name, c_address, c_nationkey, c_phone, "
        "c_acctbal, c_mktsegment, c_comment) "
        "VALUES (7, 'Customer#0007_Amer3', 'Houston', 1, '1-713-7', 2000.0, 'AUTOMOBILE', "
        "'Amer rank 3');"
    ),
    (
        "INSERT INTO customer (c_custkey, c_name, c_address, c_nationkey, c_phone, "
        "c_acctbal, c_mktsegment, c_comment) "
        "VALUES (8, 'Customer#0008_Amer4', 'Seattle', 1, '1-206-8', 2000.0, 'AUTOMOBILE', "
        "'Amer rank 4');"
    ),
    # ASIA Orders & Lineitems (All in 1995: Total 100,000)
    # Customer 1: 40,000 (Rank 1 in Asia)
    (
        "INSERT INTO orders (o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
        "o_orderpriority, o_clerk, o_shippriority, o_comment) "
        "VALUES (101, 1, 'F', 40000.0, '1995-02-10', '1-URGENT', 'Clerk#01', 0, 'Asia1 order');"
    ),
    (
        "INSERT INTO lineitem (l_orderkey, l_partkey, l_suppkey, l_linenumber, l_quantity, "
        "l_extendedprice, l_discount, l_tax, l_returnflag, l_linestatus, l_shipdate, "
        "l_commitdate, l_receiptdate, l_shipinstruct, l_shipmode, l_comment) "
        "VALUES (101, 201, 1, 1, 40.0, 40000.0, 0.0, 0.0, 'N', 'O', '1995-02-15', "
        "'1995-02-20', '1995-02-22', 'DELIVER', 'AIR', 'Lineitem 101');"
    ),
    # Customer 2: 30,000 (Rank 2 in Asia)
    (
        "INSERT INTO orders (o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
        "o_orderpriority, o_clerk, o_shippriority, o_comment) "
        "VALUES (102, 2, 'F', 30000.0, '1995-04-15', '2-HIGH', 'Clerk#02', 0, 'Asia2 order');"
    ),
    (
        "INSERT INTO lineitem (l_orderkey, l_partkey, l_suppkey, l_linenumber, l_quantity, "
        "l_extendedprice, l_discount, l_tax, l_returnflag, l_linestatus, l_shipdate, "
        "l_commitdate, l_receiptdate, l_shipinstruct, l_shipmode, l_comment) "
        "VALUES (102, 201, 1, 1, 30.0, 30000.0, 0.0, 0.0, 'N', 'O', '1995-04-20', "
        "'1995-04-25', '1995-04-26', 'DELIVER', 'AIR', 'Lineitem 102');"
    ),
    # Customer 3: 20,000 (Rank 3 in Asia)
    (
        "INSERT INTO orders (o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
        "o_orderpriority, o_clerk, o_shippriority, o_comment) "
        "VALUES (103, 3, 'F', 20000.0, '1995-06-20', '3-MEDIUM', 'Clerk#03', 0, 'Asia3 order');"
    ),
    (
        "INSERT INTO lineitem (l_orderkey, l_partkey, l_suppkey, l_linenumber, l_quantity, "
        "l_extendedprice, l_discount, l_tax, l_returnflag, l_linestatus, l_shipdate, "
        "l_commitdate, l_receiptdate, l_shipinstruct, l_shipmode, l_comment) "
        "VALUES (103, 201, 1, 1, 20.0, 20000.0, 0.0, 0.0, 'N', 'O', '1995-06-25', "
        "'1995-06-28', '1995-06-30', 'DELIVER', 'MAIL', 'Lineitem 103');"
    ),
    # Customer 4: 10,000 (Rank 4 in Asia - Should be EXCLUDED by Top 3)
    (
        "INSERT INTO orders (o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
        "o_orderpriority, o_clerk, o_shippriority, o_comment) "
        "VALUES (104, 4, 'F', 10000.0, '1995-08-05', '4-LOW', 'Clerk#04', 0, 'Asia4 order');"
    ),
    (
        "INSERT INTO lineitem (l_orderkey, l_partkey, l_suppkey, l_linenumber, l_quantity, "
        "l_extendedprice, l_discount, l_tax, l_returnflag, l_linestatus, l_shipdate, "
        "l_commitdate, l_receiptdate, l_shipinstruct, l_shipmode, l_comment) "
        "VALUES (104, 201, 1, 1, 10.0, 10000.0, 0.0, 0.0, 'N', 'O', '1995-08-10', "
        "'1995-08-15', '1995-08-16', 'DELIVER', 'MAIL', 'Lineitem 104');"
    ),
    # AMERICA Orders & Lineitems (All in 1996: Total 260,000)
    # Customer 5: 80,000 (Rank 1 in America)
    (
        "INSERT INTO orders (o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
        "o_orderpriority, o_clerk, o_shippriority, o_comment) "
        "VALUES (105, 5, 'F', 80000.0, '1996-01-10', '1-URGENT', 'Clerk#05', 0, 'Amer1 order');"
    ),
    (
        "INSERT INTO lineitem (l_orderkey, l_partkey, l_suppkey, l_linenumber, l_quantity, "
        "l_extendedprice, l_discount, l_tax, l_returnflag, l_linestatus, l_shipdate, "
        "l_commitdate, l_receiptdate, l_shipinstruct, l_shipmode, l_comment) "
        "VALUES (105, 201, 1, 1, 80.0, 80000.0, 0.0, 0.0, 'N', 'O', '1996-01-15', "
        "'1996-01-20', '1996-01-22', 'DELIVER', 'AIR', 'Lineitem 105');"
    ),
    # Customer 6: 70,000 (Rank 2 in America)
    (
        "INSERT INTO orders (o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
        "o_orderpriority, o_clerk, o_shippriority, o_comment) "
        "VALUES (106, 6, 'F', 70000.0, '1996-03-12', '2-HIGH', 'Clerk#06', 0, 'Amer2 order');"
    ),
    (
        "INSERT INTO lineitem (l_orderkey, l_partkey, l_suppkey, l_linenumber, l_quantity, "
        "l_extendedprice, l_discount, l_tax, l_returnflag, l_linestatus, l_shipdate, "
        "l_commitdate, l_receiptdate, l_shipinstruct, l_shipmode, l_comment) "
        "VALUES (106, 201, 1, 1, 70.0, 70000.0, 0.0, 0.0, 'N', 'O', '1996-03-18', "
        "'1996-03-22', '1996-03-25', 'DELIVER', 'AIR', 'Lineitem 106');"
    ),
    # Customer 7: 60,000 (Rank 3 in America)
    (
        "INSERT INTO orders (o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
        "o_orderpriority, o_clerk, o_shippriority, o_comment) "
        "VALUES (107, 7, 'F', 60000.0, '1996-05-18', '3-MEDIUM', 'Clerk#07', 0, 'Amer3 order');"
    ),
    (
        "INSERT INTO lineitem (l_orderkey, l_partkey, l_suppkey, l_linenumber, l_quantity, "
        "l_extendedprice, l_discount, l_tax, l_returnflag, l_linestatus, l_shipdate, "
        "l_commitdate, l_receiptdate, l_shipinstruct, l_shipmode, l_comment) "
        "VALUES (107, 201, 1, 1, 60.0, 60000.0, 0.0, 0.0, 'N', 'O', '1996-05-24', "
        "'1996-05-28', '1996-05-30', 'DELIVER', 'MAIL', 'Lineitem 107');"
    ),
    # Customer 8: 50,000 (Rank 4 in America - Should be EXCLUDED by Top 3)
    (
        "INSERT INTO orders (o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
        "o_orderpriority, o_clerk, o_shippriority, o_comment) "
        "VALUES (108, 8, 'F', 50000.0, '1996-07-22', '4-LOW', 'Clerk#08', 0, 'Amer4 order');"
    ),
    (
        "INSERT INTO lineitem (l_orderkey, l_partkey, l_suppkey, l_linenumber, l_quantity, "
        "l_extendedprice, l_discount, l_tax, l_returnflag, l_linestatus, l_shipdate, "
        "l_commitdate, l_receiptdate, l_shipinstruct, l_shipmode, l_comment) "
        "VALUES (108, 201, 1, 1, 50.0, 50000.0, 0.0, 0.0, 'N', 'O', '1996-07-28', "
        "'1996-08-01', '1996-08-03', 'DELIVER', 'MAIL', 'Lineitem 108');"
    ),
]


# ==============================================================================
# Jev Mock Client for Window Operations
# ==============================================================================


def create_window_mock_client() -> JevClient:
    """Create a JevClient backed by httpx.MockTransport for TASK-30 scenarios."""
    mock_responses: dict[str, dict[str, Any]] = {
        # Scenario 1: Partitioned intra-group Top-N
        "地域ごとの売上トップ3顧客": {
            "results": {
                "primary_table": "customer",
                "secondary_table": "lineitem",
                "dimensions": ["region.r_name", "customer.c_name"],
                "metric": "total_revenue",
                "ranking": "none",
                "ambiguity": "clear",
                "filters": [],
                "window_operation": "partitioned_rank",
                "window_partition_by": "region.r_name",
                "window_limit": 3,
            }
        },
        # Scenario 2: Time-series YoY shift
        "年別売上および前年比（YoY）": {
            "results": {
                "primary_table": "lineitem",
                "secondary_table": "orders",
                "dimensions": ["orders_year"],
                "metric": "total_revenue",
                "ranking": "none",
                "ambiguity": "clear",
                "filters": [],
                "window_operation": "time_shift",
                "window_partition_by": "none",
            }
        },
    }

    default_response = {
        "results": {
            "primary_table": "customer",
            "secondary_table": "lineitem",
            "dimensions": ["region.r_name", "customer.c_name"],
            "metric": "total_revenue",
            "ranking": "none",
            "ambiguity": "clear",
            "filters": [],
            "window_operation": "partitioned_rank",
            "window_partition_by": "region.r_name",
            "window_limit": 3,
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        eval_state = body.get("state", body.get("eval_state", ""))

        for key in sorted(mock_responses.keys(), key=len, reverse=True):
            if key in eval_state:
                return httpx.Response(200, json=mock_responses[key])

        return httpx.Response(200, json=default_response)

    client = JevClient(api_key="ts_test_key_window")
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
    """Load TPC-H Ossie semantic model and verify metadata."""
    assert SCHEMA_PATH.exists(), f"Schema file not found at {SCHEMA_PATH}"
    schema = OssieSchemaAdapter.load_from_yaml(SCHEMA_PATH)

    assert schema.get_table("customer") is not None
    assert schema.get_table("region") is not None
    assert schema.get_table("lineitem") is not None
    assert schema.get_metric("total_revenue") is not None
    return schema


@pytest.fixture
def sqlite_seeded_db() -> Generator[sqlite3.Connection, None, None]:
    """Provide an in-memory SQLite database seeded with TPC-H window test records."""
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
# Scenario 1: Intra-Group Top-N ("地域ごとの売上トップ3顧客")
# ==============================================================================


class TestScenario1PartitionedTopN:
    """Verifies intra-group ranking (partitioned_rank) E2E synthesis and SQLite execution."""

    def test_partitioned_rank_ast_and_sql_synthesis(
        self, tpch_ossie_schema: SchemaMetadata
    ) -> None:
        """Verify 2-pass CTE AST generation with ROW_NUMBER() and WHERE rank_num <= 3."""
        client = create_window_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("地域ごとの売上トップ3顧客", dialect="postgres")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None
        sql = result.sql

        # 1. CTE structure
        assert "WITH ranked_base AS (" in sql
        assert "FROM ranked_base" in sql

        # 2. Window function expression in base CTE
        assert "ROW_NUMBER() OVER (" in sql
        assert "PARTITION BY region.r_name" in sql
        assert "AS rank_num" in sql

        # 3. Outer query filtering and ordering
        assert "WHERE" in sql
        assert "rank_num <= 3" in sql
        assert "ORDER BY" in sql
        assert "r_name" in sql
        assert "rank_num" in sql

    def test_partitioned_rank_sqlite_execution(
        self,
        tpch_ossie_schema: SchemaMetadata,
        sqlite_seeded_db: sqlite3.Connection,
    ) -> None:
        """Execute synthesized SQL against SQLite and verify intra-group top-3 customer results."""
        client = create_window_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("地域ごとの売上トップ3顧客", dialect="sqlite")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None

        cursor = sqlite_seeded_db.cursor()
        cursor.execute(result.sql)
        rows = cursor.fetchall()

        # Each region has 4 customers, but only top 3 should be returned (Total: 6 rows)
        assert len(rows) == 6, f"Expected 6 rows (3 per region), got {len(rows)}: {rows}"

        # Group rows by region to verify partition constraint
        region_rows: dict[str, list[tuple[str, str, float]]] = {}
        for row in rows:
            r_name, c_name, rev = row[0].strip(), row[1], float(row[2])
            region_rows.setdefault(r_name, []).append((r_name, c_name, rev))

        assert set(region_rows.keys()) == {"AMERICA", "ASIA"}

        # Verify AMERICA: Top 3 are Customer 5 (80k), 6 (70k), 7 (60k)
        amer_rows = region_rows["AMERICA"]
        assert len(amer_rows) == 3
        assert amer_rows[0][1] == "Customer#0005_Amer1"
        assert amer_rows[0][2] == 80000.0
        assert amer_rows[1][1] == "Customer#0006_Amer2"
        assert amer_rows[1][2] == 70000.0
        assert amer_rows[2][1] == "Customer#0007_Amer3"
        assert amer_rows[2][2] == 60000.0
        # Verify Customer#0008_Amer4 (50k) is excluded
        assert not any(r[1] == "Customer#0008_Amer4" for r in amer_rows)

        # Verify ASIA: Top 3 are Customer 1 (40k), 2 (30k), 3 (20k)
        asia_rows = region_rows["ASIA"]
        assert len(asia_rows) == 3
        assert asia_rows[0][1] == "Customer#0001_Asia1"
        assert asia_rows[0][2] == 40000.0
        assert asia_rows[1][1] == "Customer#0002_Asia2"
        assert asia_rows[1][2] == 30000.0
        assert asia_rows[2][1] == "Customer#0003_Asia3"
        assert asia_rows[2][2] == 20000.0
        # Verify Customer#0004_Asia4 (10k) is excluded
        assert not any(r[1] == "Customer#0004_Asia4" for r in asia_rows)


# ==============================================================================
# Scenario 2: Time-Series YoY Shift ("年別売上および前年比（YoY）")
# ==============================================================================


class TestScenario2TimeSeriesYoY:
    """Verifies time-series window shift (time_shift with LAG) E2E synthesis and SQLite."""

    def test_time_shift_yoy_ast_and_sql_synthesis(
        self, tpch_ossie_schema: SchemaMetadata
    ) -> None:
        """Verify CTE AST generation with LAG(total_revenue, 1) and YoY growth formula."""
        client = create_window_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("年別売上および前年比（YoY）", dialect="postgres")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None
        sql = result.sql

        # 1. CTE structure
        assert "WITH time_series_base AS (" in sql
        assert "FROM time_series_base" in sql

        # 2. Window LAG expressions in outer query
        assert "LAG(total_revenue, 1) OVER (ORDER BY orders_year)" in sql
        assert "AS prev_total_revenue" in sql
        assert "AS total_revenue_yoy_growth_pct" in sql
        assert "ROUND(" in sql

    def test_time_shift_yoy_sqlite_execution(
        self,
        tpch_ossie_schema: SchemaMetadata,
        sqlite_seeded_db: sqlite3.Connection,
    ) -> None:
        """Execute YoY SQL against SQLite and verify NULL for 1995 and exact +160% for 1996."""
        client = create_window_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("年別売上および前年比（YoY）", dialect="sqlite")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None

        cursor = sqlite_seeded_db.cursor()
        cursor.execute(result.sql)
        rows = cursor.fetchall()

        # Rows expected for 1995 and 1996
        assert len(rows) == 2, f"Expected 2 annual summary rows, got: {rows}"

        # Row 1: 1995 (Total revenue: 100,000, Prev: NULL, YoY%: NULL)
        row_1995 = rows[0]
        assert row_1995[0] == "1995"
        assert float(row_1995[1]) == 100000.0
        assert row_1995[2] is None, (
            f"Expected prev_total_revenue to be NULL for 1995, got {row_1995[2]}"
        )
        assert row_1995[3] is None, (
            f"Expected YoY growth pct to be NULL for 1995, got {row_1995[3]}"
        )

        # Row 2: 1996 (Total revenue: 260,000, Prev: 100,000, YoY%: +160.0%)
        # Calculation: ((260,000 - 100,000) / 100,000) * 100 = 160.0%
        row_1996 = rows[1]
        assert row_1996[0] == "1996"
        assert float(row_1996[1]) == 260000.0
        assert float(row_1996[2]) == 100000.0
        assert round(float(row_1996[3]), 2) == 160.0


# ==============================================================================
# Scenario 3: CLI Subprocess Execution Verification
# ==============================================================================


class _MockJevWindowHTTPHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler serving deterministic Jev System 1 responses for CLI window testing."""

    def do_POST(self) -> None:
        content_len = int(self.headers.get("Content-Length", 0))
        _ = json.loads(self.rfile.read(content_len).decode("utf-8")) if content_len > 0 else {}

        response_payload = {
            "results": {
                "primary_table": "customer",
                "secondary_table": "lineitem",
                "dimensions": ["region.r_name", "customer.c_name"],
                "metric": "total_revenue",
                "ranking": "none",
                "ambiguity": "clear",
                "filters": [],
                "window_operation": "partitioned_rank",
                "window_partition_by": "region.r_name",
                "window_limit": 3,
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
def mock_jev_window_server() -> Generator[str, None, None]:
    """Launch a lightweight in-process HTTP mock server for CLI window subprocess testing."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _MockJevWindowHTTPHandler)
    port = server.server_port
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    server_url = f"http://127.0.0.1:{port}"
    yield server_url

    server.shutdown()
    server.server_close()


class TestScenario3PackageCLICommand:
    """Validates CLI executable end-to-end via subprocess execution with window query."""

    def test_installed_cli_window_subprocess(
        self, mock_jev_window_server: str
    ) -> None:
        """Run CLI query command for partitioned rank and verify JSON output contains window CTE."""
        env = os.environ.copy()
        env["TYPESAFE_BASE_URL"] = mock_jev_window_server
        env["TYPESAFE_API_KEY"] = "ts_mock_key_cli_window"
        env["PYTHONIOENCODING"] = "utf-8"

        cli_executable = shutil.which("deterministic-text-to-sql")
        if cli_executable is not None:
            cli_cmd = [
                cli_executable,
                "query",
                "地域ごとの売上トップ3顧客",
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
                "地域ごとの売上トップ3顧客",
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
        # Verify CTE and Window function keywords
        assert "WITH ranked_base AS" in sql or "ranked_base" in sql
        assert "ROW_NUMBER()" in sql or "row_number()" in sql
        assert "PARTITION BY" in sql or "partition by" in sql
        assert "rank_num <= 3" in sql or "rank_num <= 3" in "".join(sql.split())


# ==============================================================================
# Scenario 4: Performance Benchmark Verification
# ==============================================================================


class TestScenario4PerformanceBenchmark:
    """Validates ultra-low latency guarantees for 2-pass CTE and Window Function compilation."""

    def test_window_ast_build_latency_steady_state_under_10ms(
        self, tpch_ossie_schema: SchemaMetadata
    ) -> None:
        """Verify window CTE AST build latency is strictly < 10.0 ms in steady state."""
        client = create_window_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        # Warm up
        engine.process_query("地域ごとの売上トップ3顧客", dialect="postgres")
        engine.process_query("地域ごとの売上トップ3顧客", dialect="postgres")

        # Measure steady-state latency
        result = engine.process_query("地域ごとの売上トップ3顧客", dialect="postgres")

        assert result.status == EngineStatus.SUCCESS
        assert result.latency_ms.ast_build < 10.0, (
            f"Window AST build latency {result.latency_ms.ast_build:.2f}ms exceeded 10.0ms"
        )

    def test_window_ast_build_100_runs_average_under_5ms(
        self, tpch_ossie_schema: SchemaMetadata
    ) -> None:
        """Execute 100 compilations and assert average AST build time is strictly under 5.0ms."""
        client = create_window_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        # Warm up compiler to eliminate initial AST compilation / caching overhead
        for _ in range(3):
            engine.process_query("地域ごとの売上トップ3顧客", dialect="postgres")

        runs = 100
        ast_times: list[float] = []

        for _ in range(runs):
            res = engine.process_query("地域ごとの売上トップ3顧客", dialect="postgres")
            assert res.status == EngineStatus.SUCCESS
            ast_times.append(res.latency_ms.ast_build)

        avg_ast_ms = sum(ast_times) / runs

        assert avg_ast_ms < 5.0, (
            f"Average window AST latency {avg_ast_ms:.3f}ms exceeded 5.0ms threshold over 100 runs"
        )
