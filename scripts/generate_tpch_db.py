"""Generate TPC-H SQLite database using DuckDB dbgen.

Creates 'tpch.db' in the current working directory with scale factor 0.01 (~10MB)
or specified scale factor. Includes proper table schemas and data.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import duckdb

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def generate_tpch_sqlite(db_path: Path, scale_factor: float = 0.01) -> None:
    """Generate TPC-H SQLite database from DuckDB.

    Args:
        db_path: Path to target SQLite file.
        scale_factor: TPC-H Scale factor (0.01 is ~10MB, 0.1 is ~100MB).
    """
    print(f"🚀 Starting TPC-H data generation with scale factor {scale_factor}...")
    t0 = time.perf_counter()

    # 1. Connect to in-memory DuckDB and generate data
    con = duckdb.connect()
    con.execute("INSTALL tpch; LOAD tpch;")
    con.execute(f"CALL dbgen(sf={scale_factor});")
    print(f"✔ TPC-H data generated in memory ({time.perf_counter() - t0:.2f}s)")

    # 2. Attach SQLite via DuckDB's sqlite extension
    print(f"📦 Exporting tables to SQLite database: {db_path}...")
    if db_path.exists():
        db_path.unlink()

    con.execute("INSTALL sqlite; LOAD sqlite;")
    con.execute(f"ATTACH '{db_path.as_posix()}' AS sqlite_db (TYPE SQLITE);")

    tables = [
        "region",
        "nation",
        "supplier",
        "customer",
        "part",
        "partsupp",
        "orders",
        "lineitem",
    ]

    for tbl in tables:
        t_start = time.perf_counter()
        con.execute(f"CREATE TABLE sqlite_db.{tbl} AS SELECT * FROM {tbl};")
        count = con.execute(f"SELECT COUNT(*) FROM sqlite_db.{tbl}").fetchone()[0]
        print(f"  - {tbl:<10}: {count:>7} rows ({time.perf_counter() - t_start:.2f}s)")

    con.execute("DETACH sqlite_db;")
    con.close()

    total_time = time.perf_counter() - t0
    size_mb = db_path.stat().st_size / (1024 * 1024)
    print(f"\n🎉 Done! Generated '{db_path}' ({size_mb:.2f} MB) in {total_time:.2f}s!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate TPC-H SQLite database.")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("tpch.db"),
        help="Output SQLite database path (default: tpch.db)",
    )
    parser.add_argument(
        "--scale-factor",
        "-s",
        type=float,
        default=0.01,
        help="TPC-H Scale factor (default: 0.01 ~ 10MB)",
    )
    args = parser.parse_args()

    generate_tpch_sqlite(args.output, args.scale_factor)
