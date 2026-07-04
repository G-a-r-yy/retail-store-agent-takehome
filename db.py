from __future__ import annotations

import csv
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = ":memory:"


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db(path: Path | str = DB_PATH) -> sqlite3.Connection:
    if str(path) == ":memory:":
        conn = connect(":memory:")
        create_schema(conn)
        load_csvs(conn)
        return conn

    db_path = Path(path)
    first_load = not db_path.exists()
    conn = connect(db_path)
    create_schema(conn)
    if first_load:
        load_csvs(conn)
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            sku TEXT PRIMARY KEY, product_id TEXT NOT NULL, product_name TEXT NOT NULL,
            category TEXT NOT NULL, color TEXT, size TEXT, retail_price TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS customers (
            customer_id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT, joined_date TEXT
        );
        CREATE TABLE IF NOT EXISTS suppliers (
            supplier_id TEXT PRIMARY KEY, supplier_name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS supplier_catalog (
            supplier_id TEXT NOT NULL, product_id TEXT NOT NULL, unit_cost TEXT NOT NULL,
            lead_time_days INTEGER NOT NULL, PRIMARY KEY (supplier_id, product_id)
        );
        CREATE TABLE IF NOT EXISTS inventory_snapshots (
            sku TEXT PRIMARY KEY, snapshot_qty INTEGER NOT NULL, reorder_point INTEGER NOT NULL,
            reorder_qty INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inventory_movements (
            movement_id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT NOT NULL, delta INTEGER NOT NULL,
            reason TEXT NOT NULL, reference_id TEXT, timestamp TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY, order_date TEXT NOT NULL, customer_id TEXT,
            order_discount_pct TEXT NOT NULL, payment_method TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS order_lines (
            order_id TEXT NOT NULL, line_no INTEGER NOT NULL, sku TEXT NOT NULL,
            quantity INTEGER NOT NULL, unit_price TEXT NOT NULL, promo_id TEXT,
            PRIMARY KEY (order_id, line_no)
        );
        CREATE TABLE IF NOT EXISTS returns (
            return_id TEXT PRIMARY KEY, return_date TEXT NOT NULL, order_id TEXT NOT NULL,
            line_no INTEGER, sku TEXT NOT NULL, quantity INTEGER NOT NULL,
            condition TEXT NOT NULL, refund_amount TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS promotions (
            promo_id TEXT PRIMARY KEY, description TEXT NOT NULL, type TEXT NOT NULL, value TEXT NOT NULL,
            scope_type TEXT NOT NULL, scope_ref TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS purchase_orders (
            po_id TEXT PRIMARY KEY, supplier_id TEXT NOT NULL, po_date TEXT NOT NULL,
            status TEXT NOT NULL, synthesized INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS purchase_order_lines (
            po_id TEXT NOT NULL, line_no INTEGER NOT NULL, sku TEXT NOT NULL,
            qty_ordered INTEGER NOT NULL, qty_received INTEGER NOT NULL DEFAULT 0,
            unit_cost TEXT NOT NULL, PRIMARY KEY (po_id, line_no)
        );
        """
    )
    conn.commit()


def load_csvs(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        _load(conn, "products", DATA_DIR / "products.csv")
        _load(conn, "customers", DATA_DIR / "customers.csv")
        _load(conn, "suppliers", DATA_DIR / "suppliers.csv")
        _load(conn, "supplier_catalog", DATA_DIR / "supplier_catalog.csv")
        with (DATA_DIR / "inventory.csv").open(newline="") as f:
            for row in csv.DictReader(f):
                conn.execute(
                    "INSERT INTO inventory_snapshots VALUES (?, ?, ?, ?)",
                    (row["sku"], row["on_hand_qty"], row["reorder_point"], row["reorder_qty"]),
                )
        _load(conn, "orders", DATA_DIR / "orders.csv")
        _load(conn, "order_lines", DATA_DIR / "order_lines.csv")
        with (DATA_DIR / "returns.csv").open(newline="") as f:
            for row in csv.DictReader(f):
                conn.execute(
                    "INSERT INTO returns(return_id, return_date, order_id, sku, quantity, condition, refund_amount) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (row["return_id"], row["return_date"], row["order_id"], row["sku"], row["quantity"], row["condition"], row["refund_amount"]),
                )
        _load(conn, "promotions", DATA_DIR / "promotions.csv")


def _load(conn: sqlite3.Connection, table: str, path: Path) -> None:
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return
    cols = rows[0].keys()
    placeholders = ",".join(["?"] * len(cols))
    conn.executemany(
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
        [tuple(row[c] for c in cols) for row in rows],
    )


def next_id(conn: sqlite3.Connection, table: str, column: str, prefix: str, start: int = 1) -> str:
    rows = conn.execute(f"SELECT {column} AS id FROM {table} WHERE {column} LIKE ?", (f"{prefix}%",)).fetchall()
    max_seen = start - 1
    for row in rows:
        suffix = str(row["id"]).split("-")[-1]
        if suffix.isdigit():
            max_seen = max(max_seen, int(suffix))
    return f"{prefix}{max_seen + 1}"


def current_qty(conn: sqlite3.Connection, sku: str) -> int:
    row = conn.execute(
        """
        SELECT i.snapshot_qty + COALESCE(SUM(m.delta), 0) AS qty
        FROM inventory_snapshots i
        LEFT JOIN inventory_movements m ON m.sku = i.sku
        WHERE i.sku = ?
        GROUP BY i.sku
        """,
        (sku,),
    ).fetchone()
    return int(row["qty"]) if row else 0


def fetchone(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    return conn.execute(sql, params).fetchone()
