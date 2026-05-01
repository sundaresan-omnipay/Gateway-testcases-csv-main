"""
Gateway module coverage summary.

Reads gateway CSVs from adyen_direct_intergration/ and prints a module table.
With --write-mysql, writes ONLY into:
  - automation_coverage_gateway_module
  - automation_coverage_gateway_overall
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def _debug(msg: str) -> None:
    if os.environ.get("DEBUG_MYSQL") in {"1", "true", "TRUE", "yes", "YES"}:
        print(f"[mysql-debug] {msg}", file=sys.stderr)


def _classify_field(status: str | None) -> str:
    if status is None:
        return "not_set"
    s = status.strip().lower().replace("’", "'")
    if not s:
        return "not_set"
    # Excluded from total_automatable (pending + automated only).
    if "out of scope" in s or "out_of_scope" in s:
        return "out_of_scope"
    if "prod checklist" in s:
        return "prod_monitoring"
    if "prod monitoring" in s or "prod_monitoring" in s:
        return "prod_monitoring"
    if "can't be automated" in s or "cant be automated" in s:
        return "cant"
    if s == "pending":
        return "pending"
    if s in {"n/a", "na", "not applicable"}:
        return "not_set"
    return "automated"


def _bucket_for_count(bucket: str) -> str:
    return "pending" if bucket == "not_set" else bucket


def _find_status_field(fieldnames: list[str]) -> str | None:
    for f in fieldnames:
        lf = f.strip().lower().replace(" ", "_")
        if lf in {"automation_status", "automationstatus"}:
            return f
    for f in fieldnames:
        lf = f.strip().lower().replace(" ", "_")
        if "automation" in lf and "status" in lf:
            return f
    return None


def _is_empty_row(row: dict[str, str | None]) -> bool:
    return all(v is None or str(v).strip() == "" for v in row.values())


def _pretty_module_name_from_filename(filename_stem: str) -> str:
    words = [w for w in filename_stem.replace("-", "_").split("_") if w]
    base = " ".join(w.capitalize() for w in words)
    if base.lower().startswith("adyen "):
        return base
    return f"Adyen {base}"


@dataclass(frozen=True)
class CoverageRow:
    module_name: str
    total_cases: int
    cant_be_automated: int
    prod_monitoring: int
    out_of_scope: int
    total_automatable: int
    total_automated: int
    pending: int
    automation_pct: float
    last_updated: str


def _mysql_connect():
    # Ensure scripts/ is on path so db_connector (same dir) can be imported
    _script_dir = Path(__file__).resolve().parent
    if str(_script_dir) not in sys.path:
        sys.path.insert(0, str(_script_dir))
    from db_connector import MySQLConnector

    host = os.environ.get("DB_HOST")
    user = os.environ.get("DB_USER")
    password = os.environ.get("DB_PASS")
    database = os.environ.get("DB_NAME")
    if not all([host, user, password, database]):
        raise SystemExit(
            "MySQL env vars required for --write-mysql: DB_HOST, DB_USER, DB_PASS, DB_NAME"
        )

    port_raw = os.environ.get("DB_PORT")
    port = int(port_raw) if port_raw is not None else None
    ssh_host = os.environ.get("SSH_HOST") or None
    ssh_user = os.environ.get("SSH_USER") or None
    ssh_pkey = os.environ.get("SSH_PKEY") or None

    _debug(
        "MySQL env summary: "
        f"DB_HOST={host!r} DB_NAME={database!r} DB_USER={user!r} "
        f"DB_PORT={port_raw!r} SSH_HOST={'set' if ssh_host else 'unset'} SSH_USER={'set' if ssh_user else 'unset'} "
        f"SSH_PKEY={'set' if ssh_pkey else 'unset'}"
    )

    if ssh_host and ssh_user:
        connector = MySQLConnector(
            db_host=host,
            db_user=user,
            db_password=password,
            db_name=database,
            ssh_host=ssh_host,
            ssh_user=ssh_user,
            db_port=None,
            ssh_pkey=ssh_pkey,
        )
    else:
        connector = MySQLConnector(
            db_host=host,
            db_user=user,
            db_password=password,
            db_name=database,
            ssh_host=None,
            ssh_user=None,
            db_port=port if port is not None else 3306,
        )
    connector.connection.autocommit(True)
    return connector


def _mysql_upsert_many(conn, table: str, columns: list[str], update_columns: list[str], rows: list[tuple]) -> None:
    if not rows:
        return
    placeholders = ", ".join(["%s"] * len(columns))
    col_sql = ", ".join(columns)
    update_assignments = [f"{c}=VALUES({c})" for c in update_columns]
    update_assignments.append("last_updated = CURRENT_TIMESTAMP")
    sql = (
        f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {', '.join(update_assignments)}"
    )
    with conn.cursor() as cur:
        cur.executemany(sql, rows)


def _create_tables(conn) -> None:
    create_module_table_sql = """
CREATE TABLE IF NOT EXISTS automation_coverage_gateway_module (
  module_name VARCHAR(255) NOT NULL PRIMARY KEY,
  total_cases INT NOT NULL,
  cant_be_automated INT NOT NULL,
  prod_monitoring INT NOT NULL,
  out_of_scope INT NOT NULL,
  total_automatable INT NOT NULL,
  total_automated INT NOT NULL,
  pending INT NOT NULL,
  automation_pct DECIMAL(5,2) NOT NULL,
  last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
""".strip()

    create_overall_table_sql = """
CREATE TABLE IF NOT EXISTS automation_coverage_gateway_overall (
  scope ENUM('gateway') NOT NULL PRIMARY KEY,
  total_cases INT NOT NULL,
  cant_be_automated INT NOT NULL,
  prod_monitoring INT NOT NULL,
  out_of_scope INT NOT NULL,
  total_automatable INT NOT NULL,
  total_automated INT NOT NULL,
  pending INT NOT NULL,
  automation_pct DECIMAL(5,2) NOT NULL,
  last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
""".strip()

    with conn.cursor() as cur:
        cur.execute(create_module_table_sql)
        cur.execute(create_overall_table_sql)


def summarize_gateway(gateway_dir: Path, write_mysql: bool = False) -> None:
    gateway_dir = gateway_dir.resolve()
    if not gateway_dir.exists() or not gateway_dir.is_dir():
        raise SystemExit(f"Gateway dir not found: {gateway_dir}")

    last_updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "total_cases": 0,
            "cant_be_automated": 0,
            "prod_monitoring": 0,
            "out_of_scope": 0,
            "total_automated": 0,
            "pending": 0,
        }
    )

    csv_files = sorted(p for p in gateway_dir.glob("*.csv") if p.is_file())
    if not csv_files:
        print(f"No gateway CSVs found under: {gateway_dir}")
        return

    for csv_path in csv_files:
        module_name = _pretty_module_name_from_filename(csv_path.stem)
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                continue
            status_field = _find_status_field(reader.fieldnames)
            if not status_field:
                print(f"[skip] {csv_path.name}: couldn't find Automation Status column")
                continue
            for r in reader:
                if not r or _is_empty_row(r):
                    continue
                bucket = _classify_field(r.get(status_field))
                key = _bucket_for_count(bucket)
                stats[module_name]["total_cases"] += 1
                if bucket == "cant":
                    stats[module_name]["cant_be_automated"] += 1
                elif bucket == "prod_monitoring":
                    stats[module_name]["prod_monitoring"] += 1
                elif bucket == "out_of_scope":
                    stats[module_name]["out_of_scope"] += 1
                elif key == "pending":
                    stats[module_name]["pending"] += 1
                else:
                    stats[module_name]["total_automated"] += 1

    rows: list[CoverageRow] = []
    for module_name in sorted(stats.keys(), key=lambda x: x.lower()):
        s = stats[module_name]
        autoable = s["total_automated"] + s["pending"]
        pct = round((s["total_automated"] / autoable * 100.0), 2) if autoable else 0.0
        rows.append(
            CoverageRow(
                module_name=module_name,
                total_cases=s["total_cases"],
                cant_be_automated=s["cant_be_automated"],
                prod_monitoring=s["prod_monitoring"],
                out_of_scope=s["out_of_scope"],
                total_automatable=autoable,
                total_automated=s["total_automated"],
                pending=s["pending"],
                automation_pct=pct,
                last_updated=last_updated,
            )
        )

    module_w = max(len("Module"), *(len(r.module_name) for r in rows))
    sep = "-" * 132
    print()
    print(sep)
    print("Gateway Module Coverage")
    print(f"Gateway dir  : {gateway_dir}")
    print(f"Last updated : {last_updated}")
    print(sep)
    print(
        f"{'Module':<{module_w}} | {'Total':>5} {'Cant':>5} {'ProdMon':>7} {'OOS':>5} "
        f"{'Auto-able':>9} {'Auto':>6} {'Pend':>6} {'Auto %':>7}"
    )
    print(sep)
    for r in rows:
        print(
            f"{r.module_name:<{module_w}} | {r.total_cases:>5} {r.cant_be_automated:>5} {r.prod_monitoring:>7} {r.out_of_scope:>5} "
            f"{r.total_automatable:>9} {r.total_automated:>6} {r.pending:>6} {r.automation_pct:>6.2f}%"
        )
    print(sep)

    total_cases = sum(r.total_cases for r in rows)
    total_cant = sum(r.cant_be_automated for r in rows)
    total_prod = sum(r.prod_monitoring for r in rows)
    total_oos = sum(r.out_of_scope for r in rows)
    total_autoable = sum(r.total_automatable for r in rows)
    total_auto = sum(r.total_automated for r in rows)
    total_pending = sum(r.pending for r in rows)
    total_pct = round((total_auto / total_autoable * 100.0), 2) if total_autoable else 0.0

    print(
        f"{'TOTAL':<{module_w}} | {total_cases:>5} {total_cant:>5} {total_prod:>7} {total_oos:>5} "
        f"{total_autoable:>9} {total_auto:>6} {total_pending:>6} {total_pct:>6.2f}%"
    )
    print(sep)

    if write_mysql:
        connector = _mysql_connect()
        try:
            conn = connector.connection
            # Tables are expected to exist already (created via migrations).

            mod_cols = [
                "module_name",
                "total_cases",
                "cant_be_automated",
                "prod_monitoring",
                "out_of_scope",
                "total_automatable",
                "total_automated",
                "pending",
                "automation_pct",
            ]
            _mysql_upsert_many(
                conn,
                "automation_coverage_gateway_module",
                mod_cols,
                mod_cols[1:],
                [
                    (
                        r.module_name,
                        r.total_cases,
                        r.cant_be_automated,
                        r.prod_monitoring,
                        r.out_of_scope,
                        r.total_automatable,
                        r.total_automated,
                        r.pending,
                        r.automation_pct,
                    )
                    for r in rows
                ],
            )

            overall_cols = [
                "scope",
                "total_cases",
                "cant_be_automated",
                "prod_monitoring",
                "out_of_scope",
                "total_automatable",
                "total_automated",
                "pending",
                "automation_pct",
            ]
            _mysql_upsert_many(
                conn,
                "automation_coverage_gateway_overall",
                overall_cols,
                overall_cols[1:],
                [
                    (
                        "gateway",
                        total_cases,
                        total_cant,
                        total_prod,
                        total_oos,
                        total_autoable,
                        total_auto,
                        total_pending,
                        total_pct,
                    )
                ],
            )
            _debug(f"Upserted module rows={len(rows)} and overall row=1")
        finally:
            connector.close_connection()


def main() -> None:
    parser = argparse.ArgumentParser(description="Gateway module coverage summary.")
    parser.add_argument(
        "gateway_dir",
        type=Path,
        nargs="?",
        default=Path("adyen_direct_intergration"),
        help="Path to gateway CSV folder (default: adyen_direct_intergration)",
    )
    parser.add_argument(
        "--write-mysql",
        action="store_true",
        help="Write module + overall gateway coverage into MySQL (2 tables only).",
    )
    args = parser.parse_args()
    summarize_gateway(args.gateway_dir, write_mysql=args.write_mysql)


if __name__ == "__main__":
    main()

