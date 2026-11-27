"""SQLite 儲存層
================
主數據唔再散裝成 6 個 CSV，而是一個 `rp.db`：
  · 上載 Excel／CSV → 解析 → 一個 transaction 內整表替換（出錯自動回滾）
  · 匯出 → SQL 查詢 → CSV 或 Excel
  · 首次啟動若 DB 未有任何表，會由 `reference/*.csv` 播種

檔案位置：`RP_REF_DIR`（預設 ./reference，Railway 設為 /data）。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "reference")
FALLBACK_SEED_DIR = os.path.join(BASE_DIR, "seed")


def _default_seed_dir() -> str:
    """有真實主數據（reference/*.csv）就用它；否則用 seed/ 的虛擬資料。"""
    if os.path.isdir(DATA_DIR) and any(n.endswith(".csv") for n in os.listdir(DATA_DIR)):
        return DATA_DIR
    return FALLBACK_SEED_DIR


SEED_DIR = os.environ.get("RP_SEED_DIR") or _default_seed_dir()
REF_DIR = os.environ.get("RP_REF_DIR") or DATA_DIR
DB_PATH = os.environ.get("RP_DB_PATH") or os.path.join(REF_DIR, "rp.db")

if os.path.abspath(REF_DIR) != os.path.abspath(SEED_DIR) and os.path.isdir(SEED_DIR):
    os.makedirs(REF_DIR, exist_ok=True)
    if not any(name.endswith(".csv") for name in os.listdir(REF_DIR)):
        for name in os.listdir(SEED_DIR):
            if name.endswith(".csv") and not name.startswith("_"):
                shutil.copy2(os.path.join(SEED_DIR, name), os.path.join(REF_DIR, name))


@dataclass(frozen=True)
class TableSpec:
    name: str
    csv: str
    columns: List[str]
    keys: List[str]
    label: str

    @property
    def quoted(self) -> str:
        return '"' + self.name + '"'


EXEMPTION_COLUMNS = ["SKU"] + [f"{cls}_{abc}" for cls in "ABC" for abc in "ABC"]

TABLE_SPECS: Dict[str, TableSpec] = {
    "warehouse_calendar": TableSpec(
        "warehouse_calendar", "warehouse_calendar.csv", ["Shop", "P", "D"], ["Shop"], "Warehouse Calendar"),
    "shop_class": TableSpec(
        "shop_class", "shop_class.csv",
        ["Shop", "Class", "Status", "Coverage - A Items", "Coverage - B Items", "Coverage - C Items"],
        ["Shop"], "Shop_Class"),
    "vendor_schedule": TableSpec(
        "vendor_schedule", "vendor_schedule.csv",
        ["Shop", "Vendor", "Delivery S", "Planning S", "Lead Time"], ["Shop", "Vendor"], "Vendor Schedule"),
    "mss_list": TableSpec(
        "mss_list", "mss_list.csv", ["Shop", "MC", "A", "B", "C"], ["Shop", "MC"], "MSS List"),
    "d001_moq": TableSpec(
        "d001_moq", "d001_moq.csv", ["SKU", "MOQ"], ["SKU"], "D001 MOQ"),
    "exemption_qty": TableSpec(
        "exemption_qty", "exemption_qty.csv", EXEMPTION_COLUMNS, ["SKU"], "Exemption Qty"),
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def connect():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def _create(conn: sqlite3.Connection, spec: TableSpec) -> None:
    columns = ", ".join(f'"{c}" TEXT' for c in spec.columns)
    conn.execute(f"CREATE TABLE IF NOT EXISTS {spec.quoted} ({columns})")
    index = f'idx_{spec.name}_keys'
    conn.execute(f'CREATE INDEX IF NOT EXISTS "{index}" ON {spec.quoted} ({", ".join(chr(34) + k + chr(34) for k in spec.keys)})')


def count(name: str) -> int:
    spec = TABLE_SPECS[name]
    with connect() as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {spec.quoted}").fetchone()[0])


def replace_table(name: str, frame: pd.DataFrame) -> int:
    """整表替換，包在一個 transaction 內；任何錯誤自動回滾。"""
    spec = TABLE_SPECS[name]
    data = frame.copy()
    for column in spec.columns:
        if column not in data.columns:
            data[column] = ""
    data = data[spec.columns].fillna("").astype(str)
    rows = [tuple(row) for row in data.itertuples(index=False, name=None)]

    placeholders = ", ".join("?" for _ in spec.columns)
    columns = ", ".join(f'"{c}"' for c in spec.columns)
    with connect() as conn:
        try:
            conn.execute("BEGIN")
            conn.execute(f"DELETE FROM {spec.quoted}")
            conn.executemany(f"INSERT INTO {spec.quoted} ({columns}) VALUES ({placeholders})", rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return len(rows)


def load_frame(name: str) -> pd.DataFrame:
    spec = TABLE_SPECS[name]
    with connect() as conn:
        return pd.read_sql_query(f"SELECT * FROM {spec.quoted}", conn, dtype=str)


def _seed_one(name: str, spec: TableSpec) -> int:
    path = os.path.join(REF_DIR, spec.csv)
    if not os.path.exists(path):
        path = os.path.join(SEED_DIR, spec.csv)
    if not os.path.exists(path):
        return 0
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    frame.columns = [str(c).strip() for c in frame.columns]
    return replace_table(name, frame)


def init(force: bool = False) -> Dict[str, int]:
    """建表；DB 空的話由 CSV 播種。回傳各表筆數。"""
    with connect() as conn:
        for spec in TABLE_SPECS.values():
            _create(conn, spec)
        conn.commit()

    for name, spec in TABLE_SPECS.items():
        if force or count(name) == 0:
            _seed_one(name, spec)
    return stats()


def stats() -> Dict[str, int]:
    return {name: count(name) for name in TABLE_SPECS}


def info() -> Dict:
    size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    return {"path": DB_PATH, "size_kb": round(size / 1024, 1), "rows": stats()}


def export_frame(name: str) -> pd.DataFrame:
    return load_frame(name)


def normalise_columns(frame: pd.DataFrame, spec: TableSpec) -> pd.DataFrame:
    """把上載檔的欄名對回標準欄名（忽略大小寫、空格、符號）。"""
    import re

    def norm(v: str) -> str:
        return re.sub(r"[^a-z0-9]", "", str(v).lower())

    lookup = {norm(c): c for c in frame.columns}
    rename = {}
    for column in spec.columns:
        hit = lookup.get(norm(column))
        if hit:
            rename[hit] = column
    return frame.rename(columns=rename)
