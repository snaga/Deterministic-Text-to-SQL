"""End-to-End integration and multi-domain verification tests using Ossie models (TASK-20).

Detroit-style integration tests validating:
1. Section 1: TPC-H Ossie YAML Regression Verification (5 representative cases)
   - Case 1: Customer Aggregation (customer <-> nation <-> region shortest path join)
   - Case 2: Revenue Ranking (Ossie semantic metric total_revenue expands lineitem formula)
   - Case 3: Inventory Shortage (part <-> partsupp with numeric filter ps_availqty <= 10)
   - Case 4: Ambiguous Query HITL (EngineStatus.HITL_REQUIRED -> slash syntax -> SUCCESS)
   - Case 5: Hierarchical Deduplication (ASIA + JAPAN pruning redundant region joins)
2. Section 2: Multi-Domain Verification (Flights semantic model)
   - Loading flights.semantic_model.yaml into DeterministicTextToSQLEngine
   - Flight Query 1: "長距離フライト一覧" -> FLIGHT root inequality filter
   - Flight Query 2: "機材ごとの座席数上位10件" -> AIRCRAFT root, ORDER BY nr_seats DESC LIMIT 10
   - Clean domain separation with no TPC-H table/column leakage
3. Section 3: Real Database Execution Verification (SQLite / DuckDB)
   - Execution of generated SQL for TPC-H against seeded SQLite in-memory database
   - Execution of generated SQL for Flights against seeded SQLite in-memory database
   - Execution against DuckDB if available
4. Section 4: Performance Benchmark Verification
   - Each query ast_build latency strictly < 10.0 ms
   - 100 consecutive compilations average < 5.0 ms
5. Section 5: Installed CLI Subprocess Execution Verification
   - Subprocess execution of deterministic-text-to-sql with --schema tpch_ossie.yaml --json
   - Zero exit code and valid structured JSON output
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
from deterministic_text_to_sql.schema.loader import load_schema_from_yaml
from deterministic_text_to_sql.schema.models import SchemaMetadata

TPCH_OSSIE_YAML_PATH = Path("tests/fixtures/tpch_ossie.yaml")
FLIGHTS_OSSIE_YAML_PATH = Path("doc_internal/ossie/examples/flights.semantic_model.yaml")

try:
    import duckdb  # type: ignore

    HAS_DUCKDB = True
except ImportError:
    HAS_DUCKDB = False


# ==============================================================================
# DDL & Seed Data for Real Database Execution (TPC-H & Flights)
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
        p_retailprice DOUBLE PRECISION NOT NULL,
        p_comment VARCHAR(23) NOT NULL
    );
    """,
    """
    CREATE TABLE partsupp (
        ps_partkey BIGINT NOT NULL REFERENCES part(p_partkey),
        ps_suppkey BIGINT NOT NULL REFERENCES supplier(s_suppkey),
        ps_availqty INTEGER NOT NULL,
        ps_supplycost DOUBLE PRECISION NOT NULL,
        ps_comment VARCHAR(199) NOT NULL,
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
        o_comment VARCHAR(79) NOT NULL
    );
    """,
    """
    CREATE TABLE lineitem (
        l_orderkey BIGINT NOT NULL REFERENCES orders(o_orderkey),
        l_partkey BIGINT NOT NULL REFERENCES part(p_partkey),
        l_suppkey BIGINT NOT NULL REFERENCES supplier(s_suppkey),
        l_linenumber INTEGER NOT NULL,
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

FLIGHTS_DDL_STATEMENTS = [
    """
    CREATE TABLE FLIGHT (
        id INTEGER PRIMARY KEY,
        dep_delay REAL,
        air_time REAL,
        nr VARCHAR(20),
        carrier_code VARCHAR(10),
        duration REAL,
        scheduled_departure VARCHAR(30),
        arr_delay REAL,
        cancelled VARCHAR(5),
        cancel_code VARCHAR(5),
        distance REAL,
        scheduled_arrival VARCHAR(30),
        wheels_on VARCHAR(30),
        date VARCHAR(20),
        wheels_off VARCHAR(30),
        scheduled_duration REAL,
        tail_nr VARCHAR(20),
        departure VARCHAR(30),
        route_id INTEGER,
        diverted VARCHAR(5),
        arrival VARCHAR(30)
    );
    """,
    """
    CREATE TABLE AIRCRAFT (
        serial_nr VARCHAR(50) PRIMARY KEY,
        name VARCHAR(100),
        nr_seats INTEGER,
        tail_nr VARCHAR(20),
        carrier_code VARCHAR(10),
        manufacturer VARCHAR(100),
        model VARCHAR(50),
        year INTEGER,
        capacity INTEGER
    );
    """,
]

FLIGHTS_SEED_STATEMENTS = [
    (
        "INSERT INTO FLIGHT (id, nr, distance, dep_delay) "
        "VALUES (1, 'FL001', 1500.0, 5.0);"
    ),
    (
        "INSERT INTO FLIGHT (id, nr, distance, dep_delay) "
        "VALUES (2, 'FL002', 300.0, 0.0);"
    ),
    (
        "INSERT INTO FLIGHT (id, nr, distance, dep_delay) "
        "VALUES (3, 'FL003', 2500.0, 15.0);"
    ),
    (
        "INSERT INTO AIRCRAFT (serial_nr, name, nr_seats) "
        "VALUES ('SN-001', 'Boeing 777-300ER', 396);"
    ),
    (
        "INSERT INTO AIRCRAFT (serial_nr, name, nr_seats) "
        "VALUES ('SN-002', 'Airbus A350-900', 369);"
    ),
    (
        "INSERT INTO AIRCRAFT (serial_nr, name, nr_seats) "
        "VALUES ('SN-003', 'Boeing 787-9', 290);"
    ),
    (
        "INSERT INTO AIRCRAFT (serial_nr, name, nr_seats) "
        "VALUES ('SN-004', 'Airbus A320neo', 180);"
    ),
]


# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture(scope="session")
def tpch_ossie_schema() -> SchemaMetadata:
    """Load TPC-H Ossie semantic model fixture."""
    return load_schema_from_yaml(TPCH_OSSIE_YAML_PATH)


@pytest.fixture(scope="session")
def flights_ossie_schema() -> SchemaMetadata:
    """Load Flights Ossie semantic model fixture."""
    return load_schema_from_yaml(FLIGHTS_OSSIE_YAML_PATH)


@pytest.fixture
def sqlite_tpch_db() -> Generator[sqlite3.Connection, None, None]:
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


@pytest.fixture
def sqlite_flights_db() -> Generator[sqlite3.Connection, None, None]:
    """Provide an in-memory SQLite database pre-configured with Flights tables and seed data."""
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    for ddl in FLIGHTS_DDL_STATEMENTS:
        cursor.execute(ddl)
    for seed in FLIGHTS_SEED_STATEMENTS:
        cursor.execute(seed)
    conn.commit()
    yield conn
    conn.close()


def create_ossie_mock_client() -> JevClient:
    """Create a JevClient backed by httpx.MockTransport with real Jev API answers structure."""
    mock_responses: dict[str, dict[str, Any]] = {
        # --- TPC-H Representative 5 Cases ---
        # Case 1: Customer Aggregation (customer <-> nation <-> region)
        "アジア地域の顧客残高集計": {
            "model": "jev-latest",
            "answers": {
                "primary_table": {"choice": "customer"},
                "secondary_table": {"choice": "nation"},
                "dimension": {"choice": "c_name"},
                "metric": {"choice": "sum_acctbal"},
                "ranking": {"choice": "none"},
                "ambiguity": {"choice": "clear"},
                "filters": ["ASIA"],
            },
        },
        # Case 2: Revenue Ranking (Ossie semantic metric total_revenue)
        "売上上位5件の顧客と注文情報": {
            "model": "jev-latest",
            "answers": {
                "primary_table": {"choice": "customer"},
                "secondary_table": {"choice": "orders"},
                "dimension": {"choice": "c_name"},
                "metric": {"choice": "total_revenue"},
                "ranking": {"choice": "top_5"},
                "ambiguity": {"choice": "clear"},
            },
        },
        # Case 3: Inventory Shortage (part <-> partsupp, ps_availqty <= 10)
        "在庫数10以下の部品一覧": {
            "model": "jev-latest",
            "answers": {
                "primary_table": {"choice": "part"},
                "secondary_table": {"choice": "partsupp"},
                "dimension": {"choice": "p_name"},
                "metric": {"choice": "count_records"},
                "ranking": {"choice": "none"},
                "ambiguity": {"choice": "clear"},
                "has_numeric_filter": {"value": 1.0, "choice": "yes"},
                "numeric_column": {"choice": "ps_availqty", "confidence": 0.95},
                "numeric_operator": {"choice": "LTE"},
            },
        },
        # Case 4a: Ambiguous query requiring clarification
        "売上トップ5": {
            "model": "jev-latest",
            "answers": {
                "primary_table": {"choice": "orders"},
                "secondary_table": {"choice": "lineitem"},
                "dimension": {"choice": "none"},
                "metric": {"choice": "total_revenue"},
                "ranking": {"choice": "top_5"},
                "ambiguity": {"choice": "ambiguous_revenue_definition"},
                "needs_clarification": True,
            },
        },
        # Case 4b: Ambiguous query resolved via Option A (lineitem)
        "lineitem 商品明細売上": {
            "model": "jev-latest",
            "answers": {
                "primary_table": {"choice": "lineitem"},
                "secondary_table": {"choice": "none"},
                "dimension": {"choice": "none"},
                "metric": {"choice": "total_revenue"},
                "ranking": {"choice": "top_5"},
                "ambiguity": {"choice": "clear"},
            },
        },
        # Case 4c: Ambiguous query resolved via Option B (orders)
        "orders 注文伝票売上": {
            "model": "jev-latest",
            "answers": {
                "primary_table": {"choice": "orders"},
                "secondary_table": {"choice": "none"},
                "dimension": {"choice": "none"},
                "metric": {"choice": "total_revenue"},
                "ranking": {"choice": "top_5"},
                "ambiguity": {"choice": "clear"},
            },
        },
        # Case 5: Hierarchical Deduplication (ASIA + JAPAN)
        "アジアの日本の顧客残高": {
            "model": "jev-latest",
            "answers": {
                "primary_table": {"choice": "customer"},
                "secondary_table": {"choice": "nation"},
                "dimension": {"choice": "c_name"},
                "metric": {"choice": "sum_acctbal"},
                "ranking": {"choice": "none"},
                "ambiguity": {"choice": "clear"},
                "filters": ["ASIA", "JAPAN"],
            },
        },
        # --- Flights Semantic Model Cases ---
        # Flight Query 1: Long distance flights (distance > 1000)
        "長距離フライト一覧（distance > 1000）": {
            "model": "jev-latest",
            "answers": {
                "primary_table": {"choice": "FLIGHT"},
                "secondary_table": {"choice": "none"},
                "dimension": {"choice": "none"},
                "metric": {"choice": "count_records"},
                "ranking": {"choice": "none"},
                "ambiguity": {"choice": "clear"},
                "has_numeric_filter": {"value": 1.0, "choice": "yes"},
                "numeric_column": {"choice": "distance", "confidence": 0.95},
                "numeric_operator": {"choice": "GT"},
            },
        },
        # Flight Query 2: Aircraft top 10 seats
        "機材ごとの座席数上位10件": {
            "model": "jev-latest",
            "answers": {
                "primary_table": {"choice": "AIRCRAFT"},
                "secondary_table": {"choice": "none"},
                "dimension": {"choice": "name"},
                "metric": {"choice": "nr_seats"},
                "ranking": {"choice": "top_10"},
                "ambiguity": {"choice": "clear"},
            },
        },
    }

    default_response = {
        "model": "jev-latest",
        "answers": {
            "primary_table": {"choice": "customer"},
            "secondary_table": {"choice": "none"},
            "dimension": {"choice": "c_name"},
            "metric": {"choice": "count_records"},
            "ranking": {"choice": "none"},
            "ambiguity": {"choice": "clear"},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        eval_state = body.get("state", body.get("eval_state", ""))

        for key in sorted(mock_responses.keys(), key=len, reverse=True):
            if key in eval_state:
                resp = dict(mock_responses[key])
                resp["state"] = eval_state
                return httpx.Response(200, json=resp)

        def_resp = dict(default_response)
        def_resp["state"] = eval_state
        return httpx.Response(200, json=def_resp)

    client = JevClient(api_key="ts_test_key_ossie_e2e")
    client._client = httpx.Client(
        base_url="https://api.typesafe.ai",
        transport=httpx.MockTransport(handler),
    )
    return client


# ==============================================================================
# Section 1: TPC-H Ossie YAML Regression Verification (5 Representative Cases)
# ==============================================================================


class TestSection1TPCHOssieRegression:
    """Verifies all 5 representative TPC-H benchmark cases using Ossie YAML."""

    def test_case1_customer_aggregation_multi_hop_join(
        self, tpch_ossie_schema: SchemaMetadata
    ) -> None:
        """Case 1: customer <-> nation <-> region multi-hop join with regional filter."""
        client = create_ossie_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("アジア地域の顧客残高集計", dialect="sqlite")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None
        sql = result.sql

        # Verify semantic AST nodes and joins
        assert "customer" in sql
        assert "nation" in sql
        assert "region" in sql
        assert "r_name = 'ASIA'" in sql or "r_name = 'ASIA'" in sql.upper()
        assert "c_name" in sql
        assert "SUM(customer.c_acctbal)" in sql or "SUM(c_acctbal)" in sql

        # Transpile check
        parsed = sqlglot.parse_one(sql, read="sqlite")
        assert parsed is not None

    def test_case2_revenue_ranking_with_semantic_metric(
        self, tpch_ossie_schema: SchemaMetadata
    ) -> None:
        """Case 2: Semantic metric total_revenue expands lineitem formula with LIMIT 5."""
        client = create_ossie_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("売上上位5件の顧客と注文情報", dialect="sqlite")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None
        sql = result.sql

        # Verify Ossie metric formula: SUM(lineitem.l_extendedprice * (1 - lineitem.l_discount))
        assert "lineitem" in sql
        assert "customer" in sql
        assert "orders" in sql
        assert "l_extendedprice" in sql
        assert "l_discount" in sql
        assert "ORDER BY" in sql
        assert "total_revenue DESC" in sql
        assert "LIMIT 5" in sql

        parsed = sqlglot.parse_one(sql, read="sqlite")
        assert parsed is not None

    def test_case3_inventory_shortage_numeric_filter(
        self, tpch_ossie_schema: SchemaMetadata
    ) -> None:
        """Case 3: Inequality filter ps_availqty <= 10 connecting part <-> partsupp."""
        client = create_ossie_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("在庫数10以下の部品一覧", dialect="sqlite")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None
        sql = result.sql

        assert "part" in sql
        assert "partsupp" in sql
        assert "ps_availqty <= 10" in sql
        assert "p_name" in sql

        parsed = sqlglot.parse_one(sql, read="sqlite")
        assert parsed is not None

    def test_case4_ambiguity_hitl_and_slash_resolution(
        self, tpch_ossie_schema: SchemaMetadata
    ) -> None:
        """Case 4: Ambiguous query triggers HITL -> resolved via slash syntax."""
        client = create_ossie_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        # 1. First invocation: ambiguous query triggers HITL
        hitl_res = engine.process_query("売上トップ5", dialect="sqlite")
        assert hitl_res.status == EngineStatus.HITL_REQUIRED
        assert hitl_res.hitl_issue is not None
        assert len(hitl_res.hitl_issue.options) >= 2

        # 2. Resolution via slash syntax: "/ Aで"
        resolved_slash = engine.process_query("売上トップ5 / Aで", dialect="sqlite")
        assert resolved_slash.status == EngineStatus.SUCCESS
        assert resolved_slash.sql is not None
        assert "lineitem" in resolved_slash.sql

        # 3. Resolution via explicit clarify argument: clarify="A"
        resolved_arg = engine.process_query("売上トップ5", clarify="A", dialect="sqlite")
        assert resolved_arg.status == EngineStatus.SUCCESS
        assert resolved_arg.sql is not None
        assert "lineitem" in resolved_arg.sql

    def test_case5_hierarchical_deduplication_subsumption(
        self, tpch_ossie_schema: SchemaMetadata
    ) -> None:
        """Case 5: ASIA + JAPAN prunes parent region table and WHERE condition."""
        client = create_ossie_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("アジアの日本の顧客残高", dialect="sqlite")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None
        sql = result.sql

        # Child condition JAPAN must be retained
        assert "nation.n_name = 'JAPAN'" in sql or "n_name = 'JAPAN'" in sql

        # Redundant parent region join and filter must be eliminated
        assert "region" not in sql.lower()
        assert "ASIA" not in sql


# ==============================================================================
# Section 2: Multi-Domain Verification (Flights Semantic Model)
# ==============================================================================


class TestSection2MultiDomainFlightsVerification:
    """Verifies that a completely new domain (Flights) works seamlessly without engine changes."""

    def test_flights_domain_long_distance_filter(
        self, flights_ossie_schema: SchemaMetadata
    ) -> None:
        """Flight Query 1: long distance filter synthesizes FLIGHT.distance > 1000."""
        client = create_ossie_mock_client()
        engine = DeterministicTextToSQLEngine(schema=flights_ossie_schema, jev_client=client)

        result = engine.process_query("長距離フライト一覧（distance > 1000）", dialect="sqlite")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None
        sql = result.sql

        # Must target FLIGHT table
        assert "FLIGHT" in sql
        assert "FLIGHT.distance > 1000" in sql or "distance > 1000" in sql

        # Ensure no TPC-H tables or columns leaked
        for tpch_kw in [
            "customer", "orders", "lineitem", "nation", "region", "c_name", "o_totalprice"
        ]:
            assert tpch_kw not in sql.lower()

        parsed = sqlglot.parse_one(sql, read="sqlite")
        assert parsed is not None

    def test_flights_domain_aircraft_seats_ranking(
        self, flights_ossie_schema: SchemaMetadata
    ) -> None:
        """Flight Query 2: aircraft ranking synthesizes ORDER BY nr_seats DESC LIMIT 10."""
        client = create_ossie_mock_client()
        engine = DeterministicTextToSQLEngine(schema=flights_ossie_schema, jev_client=client)

        result = engine.process_query("機材ごとの座席数上位10件", dialect="sqlite")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None
        sql = result.sql

        assert "AIRCRAFT" in sql
        assert "AIRCRAFT.name" in sql or "name" in sql
        assert "nr_seats" in sql
        assert "ORDER BY" in sql
        assert "nr_seats DESC" in sql
        assert "LIMIT 10" in sql

        # Ensure zero leakage of TPC-H entities
        for tpch_kw in ["customer", "orders", "lineitem", "nation", "part", "supplier"]:
            assert tpch_kw not in sql.lower()

        parsed = sqlglot.parse_one(sql, read="sqlite")
        assert parsed is not None


# ==============================================================================
# Section 3: Real Database Execution Verification (SQLite & DuckDB)
# ==============================================================================


class TestSection3RealDatabaseExecution:
    """Verifies syntactic and semantic execution of synthesized SQL in SQLite & DuckDB."""

    def test_sqlite_execution_tpch_cases(
        self,
        tpch_ossie_schema: SchemaMetadata,
        sqlite_tpch_db: sqlite3.Connection,
    ) -> None:
        """Execute all 5 TPC-H representative queries against in-memory SQLite DB."""
        client = create_ossie_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)
        cursor = sqlite_tpch_db.cursor()

        queries = [
            ("アジア地域の顧客残高集計", None),
            ("売上上位5件の顧客と注文情報", None),
            ("在庫数10以下の部品一覧", None),
            ("売上トップ5 / Aで", None),
            ("アジアの日本の顧客残高", None),
        ]

        for query, clarify in queries:
            result = engine.process_query(query, clarify=clarify, dialect="sqlite")
            assert result.status == EngineStatus.SUCCESS
            assert result.sql is not None

            cursor.execute(result.sql)
            rows = cursor.fetchall()
            assert isinstance(rows, list)
            assert len(rows) > 0, f"Query '{query}' returned no rows from seeded SQLite TPC-H DB"

    def test_sqlite_execution_flights_cases(
        self,
        flights_ossie_schema: SchemaMetadata,
        sqlite_flights_db: sqlite3.Connection,
    ) -> None:
        """Execute Flights queries against in-memory SQLite DB."""
        client = create_ossie_mock_client()
        engine = DeterministicTextToSQLEngine(schema=flights_ossie_schema, jev_client=client)
        cursor = sqlite_flights_db.cursor()

        # Query 1
        res1 = engine.process_query("長距離フライト一覧（distance > 1000）", dialect="sqlite")
        assert res1.status == EngineStatus.SUCCESS
        assert res1.sql is not None
        cursor.execute(res1.sql)
        rows1 = cursor.fetchall()
        assert len(rows1) > 0

        # Query 2
        res2 = engine.process_query("機材ごとの座席数上位10件", dialect="sqlite")
        assert res2.status == EngineStatus.SUCCESS
        assert res2.sql is not None
        cursor.execute(res2.sql)
        rows2 = cursor.fetchall()
        assert len(rows2) > 0
        # Verify ordering: Boeing 777-300ER (396 seats) should be first
        assert rows2[0][0] == "Boeing 777-300ER"

    @pytest.mark.skipif(not HAS_DUCKDB, reason="duckdb is not installed in the environment")
    def test_duckdb_execution_all_cases(
        self,
        tpch_ossie_schema: SchemaMetadata,
        flights_ossie_schema: SchemaMetadata,
    ) -> None:
        """Execute generated SQL against DuckDB for both TPC-H and Flights."""
        client = create_ossie_mock_client()

        # 1. TPC-H in DuckDB
        engine_tpch = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)
        conn_tpch = duckdb.connect(":memory:")
        for ddl in TPCH_DDL_STATEMENTS:
            conn_tpch.execute(ddl)
        for seed in TPCH_SEED_STATEMENTS:
            conn_tpch.execute(seed)

        res_tpch = engine_tpch.process_query("売上上位5件の顧客と注文情報", dialect="duckdb")
        assert res_tpch.status == EngineStatus.SUCCESS
        assert res_tpch.sql is not None
        rows_tpch = conn_tpch.execute(res_tpch.sql).fetchall()
        assert len(rows_tpch) > 0

        # 2. Flights in DuckDB
        engine_flights = DeterministicTextToSQLEngine(
            schema=flights_ossie_schema, jev_client=client
        )
        conn_flights = duckdb.connect(":memory:")
        for ddl in FLIGHTS_DDL_STATEMENTS:
            conn_flights.execute(ddl)
        for seed in FLIGHTS_SEED_STATEMENTS:
            conn_flights.execute(seed)

        res_flights = engine_flights.process_query("機材ごとの座席数上位10件", dialect="duckdb")
        assert res_flights.status == EngineStatus.SUCCESS
        assert res_flights.sql is not None
        rows_flights = conn_flights.execute(res_flights.sql).fetchall()
        assert len(rows_flights) > 0


# ==============================================================================
# Section 4: Performance Benchmark Verification
# ==============================================================================


class TestSection4PerformanceBenchmark:
    """Validates ultra-low latency guarantees for Ossie AST compilation."""

    def test_ast_build_latency_under_10ms(
        self, tpch_ossie_schema: SchemaMetadata
    ) -> None:
        """Verify each query's ast_build latency metric is strictly less than 10.0 ms."""
        client = create_ossie_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        # Warm up
        engine.process_query("アジア地域の顧客残高集計", dialect="postgres")

        # Measure steady-state latency
        result = engine.process_query("アジア地域の顧客残高集計", dialect="postgres")

        assert result.status == EngineStatus.SUCCESS
        assert result.latency_ms.ast_build < 10.0, (
            f"AST build latency {result.latency_ms.ast_build:.2f}ms exceeded 10.0ms threshold"
        )

    def test_ast_build_100_runs_average_under_5ms(
        self, tpch_ossie_schema: SchemaMetadata
    ) -> None:
        """Execute 100 consecutive compilations and assert average AST build time is under 5ms."""
        client = create_ossie_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        # Warm up
        engine.process_query("売上上位5件の顧客と注文情報", dialect="postgres")

        runs = 100
        ast_times: list[float] = []

        for _ in range(runs):
            res = engine.process_query("売上上位5件の顧客と注文情報", dialect="postgres")
            assert res.status == EngineStatus.SUCCESS
            ast_times.append(res.latency_ms.ast_build)

        avg_ast_ms = sum(ast_times) / runs
        assert avg_ast_ms < 5.0, (
            f"Average AST build latency {avg_ast_ms:.3f}ms exceeded 5.0ms threshold over 100 runs"
        )


# ==============================================================================
# Section 5: CLI Subprocess Execution Verification
# ==============================================================================


class _MockJevOssieRequestHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler serving deterministic Jev System 1 responses for CLI testing."""

    def do_POST(self) -> None:
        content_len = int(self.headers.get("Content-Length", 0))
        _ = json.loads(self.rfile.read(content_len).decode("utf-8")) if content_len > 0 else {}

        response_payload = {
            "model": "jev-latest",
            "answers": {
                "primary_table": {"choice": "customer"},
                "secondary_table": {"choice": "nation"},
                "dimension": {"choice": "c_name"},
                "metric": {"choice": "count_records"},
                "ranking": {"choice": "none"},
                "ambiguity": {"choice": "clear"},
                "filters": ["JAPAN"],
            },
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
    server = http.server.HTTPServer(("127.0.0.1", 0), _MockJevOssieRequestHandler)
    port = server.server_port
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    server_url = f"http://127.0.0.1:{port}"
    yield server_url

    server.shutdown()
    server.server_close()


class TestSection5PackageCLICommand:
    """Validates CLI executable end-to-end via subprocess execution with Ossie YAML."""

    def test_installed_cli_executable_with_ossie_yaml_subprocess(
        self, mock_jev_http_server: str
    ) -> None:
        """Run CLI query command with --schema tpch_ossie.yaml and verify JSON output."""
        env = os.environ.copy()
        env["TYPESAFE_BASE_URL"] = mock_jev_http_server
        env["TYPESAFE_API_KEY"] = "ts_mock_key_cli_ossie"
        env["PYTHONIOENCODING"] = "utf-8"

        cli_executable = shutil.which("deterministic-text-to-sql")
        if cli_executable is not None:
            cli_cmd = [
                cli_executable,
                "query",
                "日本の顧客",
                "--schema",
                str(TPCH_OSSIE_YAML_PATH),
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
                str(TPCH_OSSIE_YAML_PATH),
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
        assert "customer" in output_json["sql"]
        assert "n_name = 'JAPAN'" in output_json["sql"]
        assert output_json["latency_ms"]["total"] > 0.0
