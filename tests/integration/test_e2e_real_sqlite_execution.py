"""Detroit-style End-to-End integration tests executing on real SQLite database.

Validates the complete end-to-end integration and actual execution of synthesized SQL:
1. Scenario 1: Overall ranking and filter ("アジアの顧客の売上トップ5 / Aで")
   - Real SQLite execution yields exactly 5 rows in strictly descending revenue order.
2. Scenario 2: Multi-dimensional aggregation ("年別かつ国別の注文金額の集計 / Aで")
   - Real SQLite execution yields non-empty rows across year, nation, and aggregated revenue.
3. Scenario 3: Temporal granularity ("1995年の月別売上集計 / Aで")
   - Real SQLite execution yields monthly sales rows within 1995.
4. Scenario 4: HAVING aggregate filter ("売上1000万以上の年別集計 / Aで")
   - Real SQLite execution yields only annual aggregations with total_revenue >= 10,000,000.
5. Scenario 5: Intra-group Top-N Window CTE ("各地域ごとの売上トップ3顧客 / Aで")
   - Synthesized with 2-pass CTE + ROW_NUMBER() OVER (PARTITION BY region.r_name ...).
   - Real SQLite execution yields at most 3 top customers per geographic region.
6. Scenario 6: Time-series YoY Window CTE ("年別の売上および前年比（YoY）成長率 / Aで")
   - Synthesized with 2-pass CTE + LAG() OVER (ORDER BY orders_year).
   - Real SQLite execution yields NULL for first year, and exact YoY growth percentage.

DB Connection Strategy:
- Connects to real 'tpch.db' if it exists in repository root (with mmap and cache optimizations).
- Falls back to in-memory SQLite database seeded with TPC-H 8 tables schema & seed data if absent.
"""

from __future__ import annotations

import json
import sqlite3
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
DB_PATH = Path("tpch.db")


# ==============================================================================
# Full TPC-H 8 Tables DDL & Comprehensive Seed Data for Offline / CI Fallback
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
        ps_suppkey INTEGER NOT NULL REFERENCES supplier(s_suppkey),
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
        l_partkey BIGINT NOT NULL,
        l_suppkey INTEGER NOT NULL,
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
        PRIMARY KEY (l_orderkey, l_linenumber),
        FOREIGN KEY (l_partkey, l_suppkey) REFERENCES partsupp(ps_partkey, ps_suppkey)
    );
    """,
]

TPCH_SEED_STATEMENTS = [
    # Regions
    (
        "INSERT INTO region (r_regionkey, r_name, r_comment) "
        "VALUES (1, 'AMERICA', 'American continent');"
    ),
    ("INSERT INTO region (r_regionkey, r_name, r_comment) VALUES (2, 'ASIA', 'Asian continent');"),
    # Nations
    (
        "INSERT INTO nation (n_nationkey, n_name, n_regionkey, n_comment) "
        "VALUES (1, 'UNITED STATES', 1, 'North America');"
    ),
    (
        "INSERT INTO nation (n_nationkey, n_name, n_regionkey, n_comment) "
        "VALUES (12, 'JAPAN', 2, 'East Asia');"
    ),
    (
        "INSERT INTO nation (n_nationkey, n_name, n_regionkey, n_comment) "
        "VALUES (18, 'CHINA', 2, 'East Asia Mainland');"
    ),
    # Suppliers
    (
        "INSERT INTO supplier (s_suppkey, s_name, s_address, s_nationkey, s_phone, "
        "s_acctbal, s_comment) "
        "VALUES (1, 'Supplier#0001', 'Tokyo 1-1', 12, '81-3-0001', 5000.0, 'Supp Japan');"
    ),
    (
        "INSERT INTO supplier (s_suppkey, s_name, s_address, s_nationkey, s_phone, "
        "s_acctbal, s_comment) "
        "VALUES (2, 'Supplier#0002', 'New York 5th', 1, '1-212-0002', 8000.0, 'Supp US');"
    ),
    # Part & Partsupp
    (
        "INSERT INTO part (p_partkey, p_name, p_mfgr, p_brand, p_type, p_size, p_container, "
        "p_retailprice, p_comment) "
        "VALUES (201, 'Part#201', 'MFG#1', 'Brand#1', 'STANDARD', 10, 'BOX', 100.0, 'Good part');"
    ),
    (
        "INSERT INTO partsupp (ps_partkey, ps_suppkey, ps_availqty, ps_supplycost, ps_comment) "
        "VALUES (201, 1, 100, 50.0, 'Stock Japan');"
    ),
    (
        "INSERT INTO partsupp (ps_partkey, ps_suppkey, ps_availqty, ps_supplycost, ps_comment) "
        "VALUES (201, 2, 200, 45.0, 'Stock US');"
    ),
    # ASIA Customers (6 customers: Top 5 + 1 excluded)
    (
        "INSERT INTO customer (c_custkey, c_name, c_address, c_nationkey, c_phone, "
        "c_acctbal, c_mktsegment, c_comment) "
        "VALUES (1, 'Customer#0001_Asia1', 'Tokyo', 12, '81-3-1', 9000.0, 'BUILDING', 'Asia 1');"
    ),
    (
        "INSERT INTO customer (c_custkey, c_name, c_address, c_nationkey, c_phone, "
        "c_acctbal, c_mktsegment, c_comment) "
        "VALUES (2, 'Customer#0002_Asia2', 'Osaka', 12, '81-6-2', 8000.0, 'BUILDING', 'Asia 2');"
    ),
    (
        "INSERT INTO customer (c_custkey, c_name, c_address, c_nationkey, c_phone, "
        "c_acctbal, c_mktsegment, c_comment) "
        "VALUES (3, 'Customer#0003_Asia3', 'Kyoto', 12, '81-75-3', 7000.0, 'BUILDING', 'Asia 3');"
    ),
    (
        "INSERT INTO customer (c_custkey, c_name, c_address, c_nationkey, c_phone, "
        "c_acctbal, c_mktsegment, c_comment) "
        "VALUES (4, 'Customer#0004_Asia4', 'Nagoya', 12, '81-52-4', 6000.0, 'BUILDING', 'Asia 4');"
    ),
    (
        "INSERT INTO customer (c_custkey, c_name, c_address, c_nationkey, c_phone, "
        "c_acctbal, c_mktsegment, c_comment) "
        "VALUES (5, 'Customer#0005_Asia5', 'Yokohama', 12, '81-45-5', 5000.0, 'BUILDING', "
        "'Asia 5');"
    ),
    (
        "INSERT INTO customer (c_custkey, c_name, c_address, c_nationkey, c_phone, "
        "c_acctbal, c_mktsegment, c_comment) "
        "VALUES (6, 'Customer#0006_Asia6', 'Beijing', 18, '86-10-6', 4000.0, 'BUILDING', 'Asia 6');"
    ),
    # AMERICA Customers (4 customers)
    (
        "INSERT INTO customer (c_custkey, c_name, c_address, c_nationkey, c_phone, "
        "c_acctbal, c_mktsegment, c_comment) "
        "VALUES (11, 'Customer#0011_Amer1', 'New York', 1, '1-212-1', 9500.0, 'AUTO', 'Amer 1');"
    ),
    (
        "INSERT INTO customer (c_custkey, c_name, c_address, c_nationkey, c_phone, "
        "c_acctbal, c_mktsegment, c_comment) "
        "VALUES (12, 'Customer#0012_Amer2', 'Chicago', 1, '1-312-2', 8500.0, 'AUTO', 'Amer 2');"
    ),
    (
        "INSERT INTO customer (c_custkey, c_name, c_address, c_nationkey, c_phone, "
        "c_acctbal, c_mktsegment, c_comment) "
        "VALUES (13, 'Customer#0013_Amer3', 'Houston', 1, '1-713-3', 7500.0, 'AUTO', 'Amer 3');"
    ),
    (
        "INSERT INTO customer (c_custkey, c_name, c_address, c_nationkey, c_phone, "
        "c_acctbal, c_mktsegment, c_comment) "
        "VALUES (14, 'Customer#0014_Amer4', 'Seattle', 1, '1-206-4', 6500.0, 'AUTO', 'Amer 4');"
    ),
    # 1995 Orders & Lineitems for ASIA Customers (Total = 165,000,000 >= 10M)
    # Cust 1: 50,000,000 (Rank 1 Asia)
    (
        "INSERT INTO orders (o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
        "o_orderpriority, o_clerk, o_shippriority, o_comment) "
        "VALUES (101, 1, 'F', 50000000.0, '1995-02-10', '1-URGENT', 'Clerk#01', 0, 'Asia1 95');"
    ),
    (
        "INSERT INTO lineitem (l_orderkey, l_partkey, l_suppkey, l_linenumber, l_quantity, "
        "l_extendedprice, l_discount, l_tax, l_returnflag, l_linestatus, l_shipdate, "
        "l_commitdate, l_receiptdate, l_shipinstruct, l_shipmode, l_comment) "
        "VALUES (101, 201, 1, 1, 50.0, 50000000.0, 0.0, 0.0, 'N', 'O', '1995-02-15', "
        "'1995-02-20', '1995-02-22', 'DELIVER', 'AIR', 'L101');"
    ),
    # Cust 2: 40,000,000 (Rank 2 Asia)
    (
        "INSERT INTO orders (o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
        "o_orderpriority, o_clerk, o_shippriority, o_comment) "
        "VALUES (102, 2, 'F', 40000000.0, '1995-04-15', '2-HIGH', 'Clerk#02', 0, 'Asia2 95');"
    ),
    (
        "INSERT INTO lineitem (l_orderkey, l_partkey, l_suppkey, l_linenumber, l_quantity, "
        "l_extendedprice, l_discount, l_tax, l_returnflag, l_linestatus, l_shipdate, "
        "l_commitdate, l_receiptdate, l_shipinstruct, l_shipmode, l_comment) "
        "VALUES (102, 201, 1, 1, 40.0, 40000000.0, 0.0, 0.0, 'N', 'O', '1995-04-20', "
        "'1995-04-25', '1995-04-26', 'DELIVER', 'AIR', 'L102');"
    ),
    # Cust 3: 30,000,000 (Rank 3 Asia)
    (
        "INSERT INTO orders (o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
        "o_orderpriority, o_clerk, o_shippriority, o_comment) "
        "VALUES (103, 3, 'F', 30000000.0, '1995-06-20', '3-MED', 'Clerk#03', 0, 'Asia3 95');"
    ),
    (
        "INSERT INTO lineitem (l_orderkey, l_partkey, l_suppkey, l_linenumber, l_quantity, "
        "l_extendedprice, l_discount, l_tax, l_returnflag, l_linestatus, l_shipdate, "
        "l_commitdate, l_receiptdate, l_shipinstruct, l_shipmode, l_comment) "
        "VALUES (103, 201, 1, 1, 30.0, 30000000.0, 0.0, 0.0, 'N', 'O', '1995-06-25', "
        "'1995-06-28', '1995-06-30', 'DELIVER', 'MAIL', 'L103');"
    ),
    # Cust 4: 20,000,000 (Rank 4 Asia)
    (
        "INSERT INTO orders (o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
        "o_orderpriority, o_clerk, o_shippriority, o_comment) "
        "VALUES (104, 4, 'F', 20000000.0, '1995-08-05', '4-LOW', 'Clerk#04', 0, 'Asia4 95');"
    ),
    (
        "INSERT INTO lineitem (l_orderkey, l_partkey, l_suppkey, l_linenumber, l_quantity, "
        "l_extendedprice, l_discount, l_tax, l_returnflag, l_linestatus, l_shipdate, "
        "l_commitdate, l_receiptdate, l_shipinstruct, l_shipmode, l_comment) "
        "VALUES (104, 201, 1, 1, 20.0, 20000000.0, 0.0, 0.0, 'N', 'O', '1995-08-10', "
        "'1995-08-15', '1995-08-16', 'DELIVER', 'MAIL', 'L104');"
    ),
    # Cust 5: 15,000,000 (Rank 5 Asia)
    (
        "INSERT INTO orders (o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
        "o_orderpriority, o_clerk, o_shippriority, o_comment) "
        "VALUES (105, 5, 'F', 15000000.0, '1995-10-10', '5-LOW', 'Clerk#05', 0, 'Asia5 95');"
    ),
    (
        "INSERT INTO lineitem (l_orderkey, l_partkey, l_suppkey, l_linenumber, l_quantity, "
        "l_extendedprice, l_discount, l_tax, l_returnflag, l_linestatus, l_shipdate, "
        "l_commitdate, l_receiptdate, l_shipinstruct, l_shipmode, l_comment) "
        "VALUES (105, 201, 1, 1, 15.0, 15000000.0, 0.0, 0.0, 'N', 'O', '1995-10-15', "
        "'1995-10-20', '1995-10-22', 'DELIVER', 'AIR', 'L105');"
    ),
    # Cust 6: 10,000,000 (Rank 6 Asia - Excluded by Top 5)
    (
        "INSERT INTO orders (o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
        "o_orderpriority, o_clerk, o_shippriority, o_comment) "
        "VALUES (106, 6, 'F', 10000000.0, '1995-11-12', '5-LOW', 'Clerk#06', 0, 'Asia6 95');"
    ),
    (
        "INSERT INTO lineitem (l_orderkey, l_partkey, l_suppkey, l_linenumber, l_quantity, "
        "l_extendedprice, l_discount, l_tax, l_returnflag, l_linestatus, l_shipdate, "
        "l_commitdate, l_receiptdate, l_shipinstruct, l_shipmode, l_comment) "
        "VALUES (106, 201, 1, 1, 10.0, 10000000.0, 0.0, 0.0, 'N', 'O', '1995-11-15', "
        "'1995-11-20', '1995-11-21', 'DELIVER', 'MAIL', 'L106');"
    ),
    # 1996 Orders & Lineitems for AMERICA Customers (Total = 260,000,000 >= 10M)
    # Cust 11: 80,000,000 (Rank 1 America)
    (
        "INSERT INTO orders (o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
        "o_orderpriority, o_clerk, o_shippriority, o_comment) "
        "VALUES (111, 11, 'F', 80000000.0, '1996-01-10', '1-URGENT', 'Clerk#11', 0, 'Amer1 96');"
    ),
    (
        "INSERT INTO lineitem (l_orderkey, l_partkey, l_suppkey, l_linenumber, l_quantity, "
        "l_extendedprice, l_discount, l_tax, l_returnflag, l_linestatus, l_shipdate, "
        "l_commitdate, l_receiptdate, l_shipinstruct, l_shipmode, l_comment) "
        "VALUES (111, 201, 2, 1, 80.0, 80000000.0, 0.0, 0.0, 'N', 'O', '1996-01-15', "
        "'1996-01-20', '1996-01-22', 'DELIVER', 'AIR', 'L111');"
    ),
    # Cust 12: 70,000,000 (Rank 2 America)
    (
        "INSERT INTO orders (o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
        "o_orderpriority, o_clerk, o_shippriority, o_comment) "
        "VALUES (112, 12, 'F', 70000000.0, '1996-03-12', '2-HIGH', 'Clerk#12', 0, 'Amer2 96');"
    ),
    (
        "INSERT INTO lineitem (l_orderkey, l_partkey, l_suppkey, l_linenumber, l_quantity, "
        "l_extendedprice, l_discount, l_tax, l_returnflag, l_linestatus, l_shipdate, "
        "l_commitdate, l_receiptdate, l_shipinstruct, l_shipmode, l_comment) "
        "VALUES (112, 201, 2, 1, 70.0, 70000000.0, 0.0, 0.0, 'N', 'O', '1996-03-18', "
        "'1996-03-22', '1996-03-24', 'DELIVER', 'AIR', 'L112');"
    ),
    # Cust 13: 60,000,000 (Rank 3 America)
    (
        "INSERT INTO orders (o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
        "o_orderpriority, o_clerk, o_shippriority, o_comment) "
        "VALUES (113, 13, 'F', 60000000.0, '1996-05-18', '3-MED', 'Clerk#13', 0, 'Amer3 96');"
    ),
    (
        "INSERT INTO lineitem (l_orderkey, l_partkey, l_suppkey, l_linenumber, l_quantity, "
        "l_extendedprice, l_discount, l_tax, l_returnflag, l_linestatus, l_shipdate, "
        "l_commitdate, l_receiptdate, l_shipinstruct, l_shipmode, l_comment) "
        "VALUES (113, 201, 2, 1, 60.0, 60000000.0, 0.0, 0.0, 'N', 'O', '1996-05-22', "
        "'1996-05-26', '1996-05-28', 'DELIVER', 'MAIL', 'L113');"
    ),
    # Cust 14: 50,000,000 (Rank 4 America - Excluded by Top 3)
    (
        "INSERT INTO orders (o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
        "o_orderpriority, o_clerk, o_shippriority, o_comment) "
        "VALUES (114, 14, 'F', 50000000.0, '1996-07-22', '4-LOW', 'Clerk#14', 0, 'Amer4 96');"
    ),
    (
        "INSERT INTO lineitem (l_orderkey, l_partkey, l_suppkey, l_linenumber, l_quantity, "
        "l_extendedprice, l_discount, l_tax, l_returnflag, l_linestatus, l_shipdate, "
        "l_commitdate, l_receiptdate, l_shipinstruct, l_shipmode, l_comment) "
        "VALUES (114, 201, 2, 1, 50.0, 50000000.0, 0.0, 0.0, 'N', 'O', '1996-07-28', "
        "'1996-08-01', '1996-08-03', 'DELIVER', 'MAIL', 'L114');"
    ),
]


# ==============================================================================
# Jev Mock Client for Real SQLite Execution Scenarios
# ==============================================================================


def create_real_sqlite_mock_client() -> JevClient:
    """Create a deterministic JevClient backed by httpx.MockTransport."""
    mock_responses: dict[str, dict[str, Any]] = {
        # Scenario 1: Overall ranking and filter
        "アジアの顧客の売上トップ5": {
            "primary_table": "customer",
            "secondary_table": "orders",
            "dimension": "c_name",
            "metric": "total_revenue",
            "ranking": "top_5",
            "ambiguity": "clear",
            "filters": ["ASIA"],
        },
        # Scenario 2: Multi-dimensional aggregation
        "年別かつ国別の注文金額の集計": {
            "primary_table": "lineitem",
            "secondary_table": "orders",
            "dimensions": ["orders_year", "nation.n_name"],
            "metric": "total_revenue",
            "ranking": "none",
            "ambiguity": "clear",
            "filters": [],
        },
        # Scenario 3: Temporal granularity (monthly) with date range filter
        "1995年の月別売上集計": {
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
        },
        # Scenario 4: HAVING aggregate filter
        "売上1000万以上の年別集計": {
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
        },
        # Scenario 5: Intra-group Top-N Window CTE
        "各地域ごとの売上トップ3顧客": {
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
        },
        # Scenario 6: Time-series YoY Window CTE
        "年別の売上および前年比（YoY）成長率": {
            "primary_table": "lineitem",
            "secondary_table": "orders",
            "dimensions": ["orders_year"],
            "metric": "total_revenue",
            "ranking": "none",
            "ambiguity": "clear",
            "filters": [],
            "window_operation": "time_shift",
            "window_partition_by": "none",
        },
    }

    default_response = {
        "primary_table": "customer",
        "secondary_table": "orders",
        "dimension": "c_name",
        "metric": "total_revenue",
        "ranking": "top_5",
        "ambiguity": "clear",
        "filters": ["ASIA"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        eval_state = body.get("state", body.get("eval_state", ""))

        for key in sorted(mock_responses.keys(), key=len, reverse=True):
            if key in eval_state:
                return httpx.Response(200, json={"results": mock_responses[key]})

        return httpx.Response(200, json={"results": default_response})

    client = JevClient(api_key="ts_test_key_real_sqlite")
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
    assert schema.get_metric("total_revenue") is not None
    return schema


@pytest.fixture(scope="session")
def sqlite_db_conn() -> Generator[sqlite3.Connection, None, None]:
    """Provide a SQLite connection to tpch.db if present, or fallback to an in-memory seeded DB."""
    if DB_PATH.exists():
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA mmap_size = 1073741824;")  # 1GB mmap
        conn.execute("PRAGMA cache_size = -128000;")  # 128MB cache
        conn.execute("PRAGMA temp_store = MEMORY;")
        yield conn
        conn.close()
    else:
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
# Scenario 1: Overall Ranking and Filter ("アジアの顧客の売上トップ5 / Aで")
# ==============================================================================


class TestOverallRankingAndFilter:
    """Verifies synthesis and real SQLite execution of top-N ranking with categorical filter."""

    def test_overall_ranking_and_filter_sqlite_execution(
        self,
        tpch_ossie_schema: SchemaMetadata,
        sqlite_db_conn: sqlite3.Connection,
    ) -> None:
        """Verify generated SQL executes in SQLite, returns exactly 5 rows in descending order."""
        client = create_real_sqlite_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("アジアの顧客の売上トップ5 / Aで", dialect="sqlite")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None

        # SQL Syntax & Clause Verification
        assert "customer.c_name" in result.sql
        assert "total_revenue" in result.sql
        assert "ORDER BY" in result.sql
        assert "LIMIT 5" in result.sql

        # Real Database Execution
        cursor = sqlite_db_conn.cursor()
        cursor.execute(result.sql)
        rows = cursor.fetchall()

        # Verification of returned rows
        assert len(rows) == 5, f"Expected exactly 5 rows, got {len(rows)}"
        assert all(isinstance(row[0], str) for row in rows)
        assert all(float(row[1]) > 0 for row in rows)

        # Descending revenue verification
        for i in range(len(rows) - 1):
            assert float(rows[i][1]) >= float(rows[i + 1][1]), (
                f"Row {i} ({rows[i][1]}) must be >= Row {i + 1} ({rows[i + 1][1]})"
            )


# ==============================================================================
# Scenario 2: Multi-Dimensional Aggregation ("年別かつ国別の注文金額の集計 / Aで")
# ==============================================================================


class TestMultiDimensionalAggregation:
    """Verifies synthesis and real SQLite execution of multi-axis cross-aggregation."""

    def test_multidimensional_aggregation_sqlite_execution(
        self,
        tpch_ossie_schema: SchemaMetadata,
        sqlite_db_conn: sqlite3.Connection,
    ) -> None:
        """Verify generated SQL executes in SQLite, returns non-empty multi-dim rows."""
        client = create_real_sqlite_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("年別かつ国別の注文金額の集計 / Aで", dialect="sqlite")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None

        # SQL Syntax & SQLite-specific STRFTIME Verification
        assert "STRFTIME('%Y', orders.o_orderdate)" in result.sql
        assert "nation.n_name" in result.sql
        assert "GROUP BY" in result.sql

        # Real Database Execution
        cursor = sqlite_db_conn.cursor()
        cursor.execute(result.sql)
        rows = cursor.fetchall()

        # Verification of returned non-empty rows
        assert len(rows) > 0, "Expected non-empty result rows for multi-dimensional aggregation"
        for row in rows:
            assert row[0] is not None, "Year must not be NULL"
            assert row[1] is not None, "Nation must not be NULL"
            assert row[2] is not None, "Revenue must not be NULL"
            assert float(row[2]) > 0, "Revenue must be positive"


# ==============================================================================
# Scenario 3: Temporal Granularity ("1995年の月別売上集計 / Aで")
# ==============================================================================


class TestTemporalGranularity:
    """Verifies monthly granularity with date range filter."""

    def test_temporal_granularity_sqlite_execution(
        self,
        tpch_ossie_schema: SchemaMetadata,
        sqlite_db_conn: sqlite3.Connection,
    ) -> None:
        """Verify generated SQL executes in SQLite, returns monthly sales rows within 1995."""
        client = create_real_sqlite_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("1995年の月別売上集計 / Aで", dialect="sqlite")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None

        # SQL Syntax Verification
        assert "STRFTIME('%m', orders.o_orderdate)" in result.sql
        assert "WHERE" in result.sql
        assert "1995-01-01" in result.sql
        assert "1995-12-31" in result.sql

        # Real Database Execution
        cursor = sqlite_db_conn.cursor()
        cursor.execute(result.sql)
        rows = cursor.fetchall()

        # Verification of returned monthly rows
        assert len(rows) > 0, "Expected at least one monthly row in 1995"
        for row in rows:
            month_str = str(row[0])
            # Month is formatted as '01' through '12'
            assert int(month_str) in range(1, 13), f"Expected month 1..12, got {month_str}"
            assert row[1] is not None
            assert float(row[1]) > 0, "Monthly revenue must be positive"


# ==============================================================================
# Scenario 4: HAVING Aggregate Filter ("売上1000万以上の年別集計 / Aで")
# ==============================================================================


class TestHavingAggregateFilter:
    """Verifies synthesis and real SQLite execution of post-aggregation HAVING threshold filter."""

    def test_having_aggregate_filter_sqlite_execution(
        self,
        tpch_ossie_schema: SchemaMetadata,
        sqlite_db_conn: sqlite3.Connection,
    ) -> None:
        """Verify generated SQL executes in SQLite, returns only rows with revenue >= 10,000,000."""
        client = create_real_sqlite_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("売上1000万以上の年別集計 / Aで", dialect="sqlite")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None

        # Verify HAVING clause presence and threshold
        assert "HAVING" in result.sql
        assert "10000000" in result.sql

        # Real Database Execution
        cursor = sqlite_db_conn.cursor()
        cursor.execute(result.sql)
        rows = cursor.fetchall()

        # Verification that every row strictly satisfies HAVING threshold
        assert len(rows) > 0, "Expected rows with revenue >= 10,000,000"
        for row in rows:
            year_val = row[0]
            revenue = float(row[1])
            assert year_val is not None
            assert revenue >= 10000000.0, (
                f"Row for year {year_val} has revenue {revenue} which is < 10,000,000"
            )


# ==============================================================================
# Scenario 5: Intra-Group Top-N Window CTE ("各地域ごとの売上トップ3顧客 / Aで")
# ==============================================================================


class TestIntraGroupTopNWindow:
    """Verifies synthesis and real SQLite execution of intra-group partitioned Top-N ranking."""

    def test_intra_group_top_n_window_sqlite_execution(
        self,
        tpch_ossie_schema: SchemaMetadata,
        sqlite_db_conn: sqlite3.Connection,
    ) -> None:
        """Verify synthesized 2-pass CTE executes in SQLite, returning <= 3 customers per region."""
        client = create_real_sqlite_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("各地域ごとの売上トップ3顧客 / Aで", dialect="sqlite")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None

        # Verify CTE and Window Function syntax
        assert "WITH ranked_base AS" in result.sql
        assert "ROW_NUMBER() OVER" in result.sql
        assert "PARTITION BY region.r_name" in result.sql
        assert "rank_num <= 3" in result.sql

        # Real Database Execution
        cursor = sqlite_db_conn.cursor()
        cursor.execute(result.sql)
        rows = cursor.fetchall()

        assert len(rows) > 0, "Expected partitioned rank rows"

        # Verify at most 3 customers per geographic region
        region_counts: dict[str, int] = {}
        for row in rows:
            region_name = str(row[0])
            region_counts[region_name] = region_counts.get(region_name, 0) + 1

        for reg, count in region_counts.items():
            assert 1 <= count <= 3, f"Region {reg} returned {count} rows, expected <= 3"


# ==============================================================================
# Scenario 6: Time-Series YoY Window CTE ("年別の売上および前年比（YoY）成長率 / Aで")
# ==============================================================================


class TestTimeSeriesYoYWindow:
    """Verifies synthesis and real SQLite execution of time-series LAG shift and YoY growth."""

    def test_time_series_yoy_window_sqlite_execution(
        self,
        tpch_ossie_schema: SchemaMetadata,
        sqlite_db_conn: sqlite3.Connection,
    ) -> None:
        """Verify synthesized CTE with LAG: first year NULL, subsequent computed."""
        client = create_real_sqlite_mock_client()
        engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

        result = engine.process_query("年別の売上および前年比（YoY）成長率 / Aで", dialect="sqlite")

        assert result.status == EngineStatus.SUCCESS
        assert result.sql is not None

        # Verify CTE and LAG Window syntax
        assert "WITH time_series_base AS" in result.sql
        assert "LAG(total_revenue, 1) OVER (ORDER BY orders_year)" in result.sql
        assert "total_revenue_yoy_growth_pct" in result.sql

        # Real Database Execution
        cursor = sqlite_db_conn.cursor()
        cursor.execute(result.sql)
        rows = cursor.fetchall()

        assert len(rows) >= 2, f"Expected at least 2 time-series years, got {len(rows)}"

        # Initial year: LAG previous revenue and YoY percentage must both be NULL (None in Python)
        first_row = rows[0]
        assert first_row[2] is None, (
            f"First year prev_total_revenue must be NULL, got {first_row[2]}"
        )
        assert first_row[3] is None, f"First year YoY growth must be NULL, got {first_row[3]}"

        # Subsequent years: previous revenue and YoY percentage must be successfully computed
        for i, row in enumerate(rows[1:], start=2):
            year_val, prev_rev, yoy_pct = row[0], row[2], row[3]
            assert prev_rev is not None, f"Row {i} ({year_val}) prev_rev must not be None"
            assert yoy_pct is not None, f"Row {i} ({year_val}) yoy_pct must not be None"
            assert float(prev_rev) > 0, f"Row {i} prev_rev must be positive"
            assert isinstance(float(yoy_pct), float), f"Row {i} yoy_pct must be float"


# ==============================================================================
# Scenario 7: Offline / CI Fallback In-Memory Seeded Execution Verification
# ==============================================================================


class TestFallbackSeededDatabase:
    """Verifies that offline fallback in-memory SQLite database satisfies constraints."""

    def test_fallback_seeded_database_execution(self, tpch_ossie_schema: SchemaMetadata) -> None:
        """Verify end-to-end execution against in-memory seeded DB for all 6 queries."""
        conn = sqlite3.connect(":memory:")
        try:
            cursor = conn.cursor()
            for ddl in TPCH_DDL_STATEMENTS:
                cursor.execute(ddl)
            for seed in TPCH_SEED_STATEMENTS:
                cursor.execute(seed)
            conn.commit()

            client = create_real_sqlite_mock_client()
            engine = DeterministicTextToSQLEngine(schema=tpch_ossie_schema, jev_client=client)

            # 1. Ranking
            res1 = engine.process_query("アジアの顧客の売上トップ5 / Aで", dialect="sqlite")
            assert res1.status == EngineStatus.SUCCESS
            assert res1.sql is not None
            rows1 = cursor.execute(res1.sql).fetchall()
            assert len(rows1) == 5

            # 2. Multi-dim
            res2 = engine.process_query("年別かつ国別の注文金額の集計 / Aで", dialect="sqlite")
            assert res2.status == EngineStatus.SUCCESS
            assert res2.sql is not None
            rows2 = cursor.execute(res2.sql).fetchall()
            assert len(rows2) > 0

            # 3. Monthly
            res3 = engine.process_query("1995年の月別売上集計 / Aで", dialect="sqlite")
            assert res3.status == EngineStatus.SUCCESS
            assert res3.sql is not None
            rows3 = cursor.execute(res3.sql).fetchall()
            assert len(rows3) > 0

            # 4. HAVING
            res4 = engine.process_query("売上1000万以上の年別集計 / Aで", dialect="sqlite")
            assert res4.status == EngineStatus.SUCCESS
            assert res4.sql is not None
            rows4 = cursor.execute(res4.sql).fetchall()
            assert len(rows4) > 0

            # 5. Window intra-group
            res5 = engine.process_query("各地域ごとの売上トップ3顧客 / Aで", dialect="sqlite")
            assert res5.status == EngineStatus.SUCCESS
            assert res5.sql is not None
            rows5 = cursor.execute(res5.sql).fetchall()
            assert len(rows5) > 0

            # 6. YoY
            res6 = engine.process_query(
                "年別の売上および前年比（YoY）成長率 / Aで", dialect="sqlite"
            )
            assert res6.status == EngineStatus.SUCCESS
            assert res6.sql is not None
            rows6 = cursor.execute(res6.sql).fetchall()
            assert len(rows6) >= 2
            assert rows6[0][2] is None
            assert rows6[1][2] is not None
        finally:
            conn.close()
