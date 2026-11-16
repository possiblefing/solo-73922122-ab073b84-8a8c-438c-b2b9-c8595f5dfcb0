"""SQLite 存储层。

核心不变量：输入（字段目录 + SQL）的任何变更都会使 version 单调递增；
核查结论按 version 存放；只有“当前 version 存在 status=pass 的结论”才允许下载。
改动后旧通过立即失效，核查失败不会沿用旧结论。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    sql TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS catalog_table (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    pos INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_column (
    id INTEGER PRIMARY KEY,
    table_id INTEGER NOT NULL REFERENCES catalog_table(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    sensitive INTEGER NOT NULL DEFAULT 0,
    pos INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis (
    version INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        with self._conn:
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('version', '0')"
            )
            self._conn.execute("INSERT OR IGNORE INTO state(id, sql) VALUES(1, '')")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 内部 ----
    def _version(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'version'"
        ).fetchone()
        return int(row["value"])

    def _catalog(self) -> list[dict]:
        tables = []
        for t in self._conn.execute(
            "SELECT id, name FROM catalog_table ORDER BY pos"
        ).fetchall():
            cols = [
                {"name": c["name"], "sensitive": bool(c["sensitive"])}
                for c in self._conn.execute(
                    "SELECT name, sensitive FROM catalog_column "
                    "WHERE table_id = ? ORDER BY pos",
                    (t["id"],),
                ).fetchall()
            ]
            tables.append({"name": t["name"], "columns": cols})
        return tables

    def _analysis_for(self, version: int) -> dict | None:
        row = self._conn.execute(
            "SELECT version, status, result_json, created_at FROM analysis "
            "WHERE version = ?",
            (version,),
        ).fetchone()
        if row is None:
            return None
        return {
            "version": row["version"],
            "status": row["status"],
            "result": json.loads(row["result_json"]),
            "created_at": row["created_at"],
        }

    # ---- 对外 ----
    def get_state(self) -> dict:
        with self._lock:
            version = self._version()
            sql = self._conn.execute("SELECT sql FROM state WHERE id = 1").fetchone()[
                "sql"
            ]
            return {
                "version": version,
                "catalog": {"tables": self._catalog()},
                "sql": sql,
                # 只返回绑定当前版本的结论；旧版本结论视为已失效，不下发
                "analysis": self._analysis_for(version),
            }

    def save_input(self, tables: list[dict], sql: str) -> tuple[int, bool]:
        """保存输入。内容有变化才递增版本（使旧结论失效）；无变化保持原版本。"""
        with self._lock:
            current = {
                "tables": self._catalog(),
                "sql": self._conn.execute(
                    "SELECT sql FROM state WHERE id = 1"
                ).fetchone()["sql"],
            }
            incoming = {"tables": tables, "sql": sql}
            if current == incoming:
                return self._version(), False
            with self._conn:
                self._conn.execute("DELETE FROM catalog_column")
                self._conn.execute("DELETE FROM catalog_table")
                for ti, t in enumerate(tables):
                    cur = self._conn.execute(
                        "INSERT INTO catalog_table(name, pos) VALUES(?, ?)",
                        (t["name"], ti),
                    )
                    table_id = cur.lastrowid
                    for ci, c in enumerate(t["columns"]):
                        self._conn.execute(
                            "INSERT INTO catalog_column(table_id, name, sensitive, pos)"
                            " VALUES(?, ?, ?, ?)",
                            (table_id, c["name"], int(bool(c["sensitive"])), ci),
                        )
                self._conn.execute("UPDATE state SET sql = ? WHERE id = 1", (sql,))
                self._conn.execute(
                    "UPDATE meta SET value = CAST(value AS INTEGER) + 1 "
                    "WHERE key = 'version'"
                )
            return self._version(), True

    def save_analysis(self, version: int, status: str, result: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO analysis(version, status, result_json,"
                " created_at) VALUES(?, ?, ?, ?)",
                (version, status, json.dumps(result, ensure_ascii=False), _now()),
            )
