#!/usr/bin/env python3
"""
Convert all CSV test case files into a single SQLite database.

Output: converted-files-sqlite/gateway_testcases.db
Every CSV becomes its own table named <folder>__<file>.

Usage:
    python3 csv_to_sqlite_converted.py
"""

import csv
import sqlite3
import re
import sys
from pathlib import Path


SOURCE_DIR = Path(__file__).parent
COMBINED_DB = SOURCE_DIR / "gateway_testcases.db"
SKIP_DIRS = {".git", "__pycache__"}


def sanitize_name(name: str) -> str:
    """Convert a string to a valid SQLite identifier."""
    name = name.strip()
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    if not name:
        name = "column"
    if name[0].isdigit():
        name = "col_" + name
    return name.lower()


def deduplicate_columns(cols: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result = []
    for col in cols:
        if col in seen:
            seen[col] += 1
            result.append(f"{col}_{seen[col]}")
        else:
            seen[col] = 0
            result.append(col)
    return result


def read_csv(csv_path: Path) -> tuple[list[str], list[list[str]]]:
    """Return (column_names, rows) from a CSV file."""
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        try:
            headers = next(reader)
        except StopIteration:
            return [], []
        return deduplicate_columns([sanitize_name(h) for h in headers]), list(reader)


def write_table(conn: sqlite3.Connection, table_name: str,
                col_names: list[str], rows: list[list[str]]) -> None:
    """Create a table and insert rows into the given connection."""
    col_defs = ", ".join(f'"{c}" TEXT' for c in col_names)
    conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
    conn.execute(f'CREATE TABLE "{table_name}" ({col_defs})')
    placeholders = ", ".join("?" for _ in col_names)
    for row in rows:
        padded = (list(row) + [""] * len(col_names))[: len(col_names)]
        conn.execute(f'INSERT INTO "{table_name}" VALUES ({placeholders})', padded)


def main():
    print(f"Source  : {SOURCE_DIR}")
    print(f"Output  : {COMBINED_DB}\n")

    csv_files = sorted(
        p for p in SOURCE_DIR.rglob("*.csv")
        if not any(part in SKIP_DIRS for part in p.parts)
    )

    if not csv_files:
        print("No CSV files found.")
        sys.exit(0)

    # Open combined DB once — fresh each run
    if COMBINED_DB.exists():
        COMBINED_DB.unlink()
    combined_conn = sqlite3.connect(COMBINED_DB)

    total = 0
    current_folder = None

    for csv_path in csv_files:
        rel = csv_path.relative_to(SOURCE_DIR)

        folder = csv_path.parent
        if folder != current_folder:
            current_folder = folder
            print(f"[{rel.parent}]")

        col_names, rows = read_csv(csv_path)
        if not col_names:
            print(f"  ✗ {csv_path.name} — skipped (empty)")
            continue

        # Table in combined DB — named <folder>__<file>
        folder_part = sanitize_name(rel.parent.name)
        file_part = sanitize_name(csv_path.stem)
        combined_table = f"{folder_part}__{file_part}"
        write_table(combined_conn, combined_table, col_names, rows)

        total += 1
        print(f"  ✓ {csv_path.name:45s} → table [{combined_table}]  ({len(rows)} rows)")

    combined_conn.commit()

    # Print a summary of tables in the combined DB
    tables = [
        r[0] for r in combined_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]
    combined_conn.close()

    print(f"\n{'─' * 60}")
    print(f"Combined DB  : gateway_testcases.db")
    print(f"Tables inside: {len(tables)}")
    for t in tables:
        print(f"  • {t}")
    print(f"\nDone — {total} CSV(s) converted.")


if __name__ == "__main__":
    main()
