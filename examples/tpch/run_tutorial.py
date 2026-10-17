"""TPC-H Tutorial: Deterministic Text-to-SQL Engine with Apache Ossie.

This script demonstrates how to:
1. Initialize an in-memory SQLite database with TPC-H benchmark tables & sample data.
2. Load an Apache Ossie semantic model (YAML).
3. Synthesize deterministic, syntax-guaranteed SQL from natural language queries.
4. Execute generated queries against the real database and inspect results.
5. Demonstrate Human-in-the-Loop (HITL) ambiguity detection and slash-syntax resolution.

Run:
    python examples/tpch/run_tutorial.py
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Ensure src/ is importable even without editable install
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

if sys.platform == "win32":
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        with contextlib.suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8")
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        with contextlib.suppress(Exception):
            sys.stderr.reconfigure(encoding="utf-8")

import httpx  # noqa: E402

from deterministic_text_to_sql import DeterministicTextToSQLEngine, EngineStatus  # noqa: E402
from deterministic_text_to_sql.jev.client import JevClient  # noqa: E402

# ==============================================================================
# 1. TPC-H DDL and Sample Seed Data (SQLite)
# ==============================================================================

TPCH_DDL = [
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

TPCH_SEEDS = [
    # Region & Nations
    "INSERT INTO region VALUES (2, 'ASIA', 'Asian continent');",
    "INSERT INTO region VALUES (3, 'EUROPE', 'European continent');",
    "INSERT INTO nation VALUES (12, 'JAPAN', 2, 'Land of the rising sun');",
    "INSERT INTO nation VALUES (8, 'GERMANY', 3, 'Central Europe');",
    # Customer
    (
        "INSERT INTO customer VALUES "
        "(1, 'Customer#000000001', 'Tokyo, Japan', 12, '81-3-1234-5678', 15000.0, "
        "'BUILDING', 'Regular customer'), "
        "(2, 'Customer#000000002', 'Berlin, Germany', 8, '49-30-1234-567', 8200.0, "
        "'AUTOMOBILE', 'Important customer');"
    ),
    # Supplier
    (
        "INSERT INTO supplier VALUES "
        "(1, 'Supplier#000000001', 'Osaka, Japan', 12, '81-6-1234-5678', 50000.0, 'Reliable');"
    ),
    # Part & Partsupp
    (
        "INSERT INTO part VALUES "
        "(201, 'golden part brass', 'Manufacturer#1', 'Brand#11', 'PROMO BRUSHED BRASS', 15, "
        "'SM CASE', 120.0, 'Brass component'), "
        "(202, 'steel bolts pack', 'Manufacturer#2', 'Brand#22', 'STANDARD STEEL', 10, "
        "'MED BAG', 45.0, 'Steel bolts');"
    ),
    (
        "INSERT INTO partsupp VALUES "
        "(201, 1, 5, 80.0, 'Critically low inventory'), "
        "(202, 1, 250, 30.0, 'Ample stock');"
    ),
    # Orders
    (
        "INSERT INTO orders VALUES "
        "(101, 1, 'O', 8500.5, '2026-01-15', '1-URGENT', 'Clerk#001', 0, 'First order'), "
        "(102, 2, 'F', 3200.0, '2026-02-10', '2-NORMAL', 'Clerk#002', 0, 'Second order');"
    ),
    # Lineitem
    (
        "INSERT INTO lineitem VALUES "
        "(101, 201, 1, 1, 10.0, 1200.0, 0.05, 0.08, 'N', 'O', '2026-01-20', "
        "'2026-01-25', '2026-01-26', 'DELIVER IN PERSON', 'AIR', 'Urgent item'), "
        "(102, 202, 1, 1, 5.0, 450.0, 0.00, 0.05, 'N', 'F', '2026-02-15', "
        "'2026-02-18', '2026-02-19', 'MAIL', 'TRUCK', 'Standard delivery');"
    ),
]


def setup_database() -> sqlite3.Connection:
    """Create in-memory SQLite database populated with TPC-H sample data."""
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    for ddl in TPCH_DDL:
        cursor.execute(ddl)
    for seed in TPCH_SEEDS:
        cursor.execute(seed)
    conn.commit()
    return conn


# ==============================================================================
# 2. Mock Jev Client for Offline Demo
# ==============================================================================

MOCK_JEV_RESPONSES: dict[str, dict[str, Any]] = {
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

DEFAULT_MOCK_RESPONSE = {
    "results": {
        "primary_table": "customer",
        "secondary_table": "none",
        "dimension": "c_name",
        "metric": "count_records",
        "ranking": "none",
        "ambiguity": "clear",
    }
}


def create_demo_jev_client(use_live_api: bool = False) -> JevClient:
    """Create a JevClient backed by mock transport for deterministic tutorial execution."""
    if use_live_api and os.environ.get("TYPESAFE_API_KEY"):
        return JevClient()

    def mock_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        eval_state = body.get("state", body.get("eval_state", body.get("text", "")))

        for key in sorted(MOCK_JEV_RESPONSES.keys(), key=len, reverse=True):
            if key in eval_state:
                return httpx.Response(200, json=MOCK_JEV_RESPONSES[key])

        return httpx.Response(200, json=DEFAULT_MOCK_RESPONSE)

    transport = httpx.MockTransport(mock_handler)
    client = JevClient(api_key="ts_mock_tutorial_key")
    client._client = httpx.Client(
        base_url="https://api.typesafe.ai",
        transport=transport,
    )
    return client


# ==============================================================================
# 3. Tutorial Execution Flow
# ==============================================================================


def main() -> None:
    """Run interactive demonstration of TPC-H Text-to-SQL synthesis."""
    print("=" * 72)
    print(" 🚀 Deterministic Text-to-SQL Engine - TPC-H Benchmark Tutorial")
    print("    Semantic Model: Apache Ossie (YAML)")
    print("=" * 72)

    # 1. Initialize DB
    print("\n[Step 1] Initializing In-Memory SQLite Database with TPC-H schema...")
    db_conn = setup_database()
    print("  ✔ Created 8 tables: region, nation, supplier, customer, ...")
    print("  ✔ Inserted seed records for queries.")

    engine: DeterministicTextToSQLEngine | None = None
    try:
        # 2. Locate Ossie Semantic Model
        script_dir = Path(__file__).resolve().parent
        schema_yaml = script_dir / "tpch_semantic_model.yaml"
        if not schema_yaml.is_file():
            schema_yaml = script_dir.parent.parent / "tests" / "fixtures" / "tpch_ossie.yaml"

        print(f"\n[Step 2] Loading Apache Ossie Semantic Model from:\n  {schema_yaml}")
        client = create_demo_jev_client(use_live_api=False)
        engine = DeterministicTextToSQLEngine(schema=schema_yaml, jev_client=client)

        # Inspect model
        info = engine.get_schema_info()
        print(f"  ✔ Model Name: {info['title']}")
        print(f"  ✔ Datasets: {len(info['tables'])} tables")
        print(f"  ✔ Metrics: {len(info.get('metrics', []))} semantic expressions")
        print(f"  ✔ Hierarchies: {len(info.get('hierarchies', []))} ontology rules")

        # 3. Demonstration Scenarios
        scenarios: list[dict[str, Any]] = [
            {
                "title": "Scenario 1: Multi-Hop Join Customer Aggregation",
                "query": "アジア地域の顧客残高集計",
                "desc": "Customer -> nation -> region traversal via Dijkstra shortest path.",
                "clarify": None,
            },
            {
                "title": "Scenario 2: Revenue Ranking with ORDER BY & LIMIT",
                "query": "売上上位5件の顧客と注文情報",
                "desc": "Customer and orders join with ORDER BY revenue DESC LIMIT 5.",
                "clarify": None,
            },
            {
                "title": "Scenario 3: Inventory Shortage with Inequality Numeric Filter",
                "query": "在庫数10以下の部品一覧",
                "desc": "Part and partsupp join with numeric filter (ps_availqty <= 10).",
                "clarify": None,
            },
            {
                "title": "Scenario 4: Ontology Hierarchy Deduplication (ASIA + JAPAN)",
                "query": "アジアの日本の顧客残高",
                "desc": (
                    "Mathematical subsumption prunes redundant parent 'region' table and filter."
                ),
                "clarify": None,
            },
        ]

        print("\n" + "=" * 72)
        print(" [Step 3] Executing 4 Representative Text-to-SQL Scenarios")
        print("=" * 72)

        for i, sc in enumerate(scenarios, 1):
            print(f"\n▶ [{i}/4] {sc['title']}")
            print(f"  Description: {sc['desc']}")
            print(f"  Input Query: '{sc['query']}'")

            result = engine.process_query(
                raw_query=sc["query"],
                dialect="sqlite",
                clarify=sc["clarify"],
            )

            if result.status == EngineStatus.SUCCESS and result.sql is not None:
                lat = result.latency_ms.ast_build
                print(f"  Status: SUCCESS (AST Build Latency: {lat:.2f} ms)")
                print("  Generated SQL:")
                for line in result.sql.strip().splitlines():
                    print(f"    {line}")

                # Execute on SQLite
                cursor = db_conn.cursor()
                cursor.execute(result.sql)
                rows = cursor.fetchall()
                col_names = [desc[0] for desc in cursor.description]
                print(f"  Query Results ({len(rows)} row(s)):")
                print(f"    Columns: {col_names}")
                for row in rows:
                    print(f"    Row: {row}")
            else:
                print(f"  Status: {result.status.value}")

        # 4. Demonstrate Human-in-the-Loop (HITL) Detection & Resolution
        print("\n" + "=" * 72)
        print(" [Step 4] Human-in-the-Loop (HITL) Disambiguation & Resolution Demo")
        print("=" * 72)
        ambiguous_query = "売上トップ5"
        print(f"  Ambiguous Query: '{ambiguous_query}' (No specific table/metric clarified)")
        hitl_result = engine.process_query(ambiguous_query, dialect="sqlite")

        if hitl_result.status == EngineStatus.HITL_REQUIRED and hitl_result.hitl_issue:
            print("  Status: HITL_REQUIRED (Exit code: 2)")
            print(f"  Clarification Question: {hitl_result.hitl_issue.question}")
            for opt in hitl_result.hitl_issue.options:
                print(f"    [{opt.key}] {opt.label} - {opt.hint}")
            print("  ✔ Engine safely halts and requests clarification instead of hallucinating!")

            # Resolve via slash syntax
            print("\n  ▶ Resolving ambiguity via slash syntax: '売上トップ5 / Aで' (lineitem)")
            resolved_result = engine.process_query("売上トップ5 / Aで", dialect="sqlite")
            if resolved_result.status == EngineStatus.SUCCESS and resolved_result.sql is not None:
                lat = resolved_result.latency_ms.ast_build
                print(f"  Status: SUCCESS (AST Build Latency: {lat:.2f} ms)")
                print("  Generated SQL:")
                for line in resolved_result.sql.strip().splitlines():
                    print(f"    {line}")

                cursor = db_conn.cursor()
                cursor.execute(resolved_result.sql)
                rows = cursor.fetchall()
                print(f"  Query Results ({len(rows)} row(s)):")
                for row in rows:
                    print(f"    Row: {row}")

        print("\n" + "=" * 72)
        print(" 🏆 Tutorial completed successfully! All SQL executed deterministically.")
        print("=" * 72)
    finally:
        if engine is not None:
            engine.close()
        db_conn.close()


if __name__ == "__main__":
    main()
