"""Create performance indexes on tpch.db tables for fast JOIN execution."""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = Path("tpch.db")

INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_region_pk ON region (r_regionkey);",
    "CREATE INDEX IF NOT EXISTS idx_nation_pk ON nation (n_nationkey);",
    "CREATE INDEX IF NOT EXISTS idx_nation_region ON nation (n_regionkey);",
    "CREATE INDEX IF NOT EXISTS idx_supplier_pk ON supplier (s_suppkey);",
    "CREATE INDEX IF NOT EXISTS idx_supplier_nation ON supplier (s_nationkey);",
    "CREATE INDEX IF NOT EXISTS idx_customer_pk ON customer (c_custkey);",
    "CREATE INDEX IF NOT EXISTS idx_customer_nation ON customer (c_nationkey);",
    "CREATE INDEX IF NOT EXISTS idx_orders_pk ON orders (o_orderkey);",
    "CREATE INDEX IF NOT EXISTS idx_orders_cust ON orders (o_custkey);",
    "CREATE INDEX IF NOT EXISTS idx_orders_date ON orders (o_orderdate);",
    "CREATE INDEX IF NOT EXISTS idx_lineitem_order ON lineitem (l_orderkey);",
    "CREATE INDEX IF NOT EXISTS idx_lineitem_supp ON lineitem (l_suppkey);",
    "CREATE INDEX IF NOT EXISTS idx_lineitem_part ON lineitem (l_partkey);",
    "ANALYZE;",
]


def add_indexes() -> None:
    if not DB_PATH.exists():
        print(f"Error: {DB_PATH} not found.")
        return

    print("⚡ Creating indexes on tpch.db for ultra-fast JOINs...")
    t0 = time.perf_counter()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    for stmt in INDEX_STATEMENTS:
        cur.execute(stmt)

    conn.commit()
    conn.close()
    print(f"✔ Indexes created & ANALYZE complete in {time.perf_counter() - t0:.2f}s!")


if __name__ == "__main__":
    add_indexes()
