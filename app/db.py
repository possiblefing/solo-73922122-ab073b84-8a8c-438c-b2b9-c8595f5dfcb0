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

from .concurrency import fingerprint

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    sql TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT ''
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


class ConflictError(Exception):
    """乐观并发冲突：客户端依据的 base_version 已被另一窗口的写入超越。"""

    def __init__(self, base_version: int, current_version: int):
        self.base_version = base_version
        self.current_version = current_version
        super().__init__(
            f"base_version={base_version} 已过期，当前版本={current_version}"
        )


class Store:
    def __init__(self, path: str):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        with self._conn:
            # 旧库迁移必须先于任何引用 fingerprint 列的语句：
            # 历史 state 表可能没有该列，先补列再建/插数据。
            existing_tables = {
                r["name"]
                for r in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "state" in existing_tables:
                cols = {
                    r["name"]
                    for r in self._conn.execute(
                        "PRAGMA table_info(state)"
                    ).fetchall()
                }
                if "fingerprint" not in cols:
                    self._conn.execute(
                        "ALTER TABLE state ADD COLUMN fingerprint "
                        "TEXT NOT NULL DEFAULT ''"
                    )
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('version', '0')"
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO state(id, sql, fingerprint) "
                "VALUES(1, '', '')"
            )
            # 回填：历史数据指纹为空时按当前内容补算
            row = self._conn.execute(
                "SELECT fingerprint FROM state WHERE id = 1"
            ).fetchone()
            if row is not None and not row["fingerprint"]:
                tables = self._catalog()
                sql = self._conn.execute(
                    "SELECT sql FROM state WHERE id = 1"
                ).fetchone()["sql"]
                if tables or sql:
                    self._conn.execute(
                        "UPDATE state SET fingerprint = ? WHERE id = 1",
                        (fingerprint(tables, sql),),
                    )

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
            row = self._conn.execute(
                "SELECT sql, fingerprint FROM state WHERE id = 1"
            ).fetchone()
            return {
                "version": version,
                "catalog": {"tables": self._catalog()},
                "sql": row["sql"],
                "fingerprint": row["fingerprint"],
                # 只返回绑定当前版本的结论；旧版本结论视为已失效，不下发
                "analysis": self._analysis_for(version),
            }

    def save_input(
        self,
        tables: list[dict],
        sql: str,
        expected_version: int | None = None,
    ) -> tuple[int, bool, str]:
        """保存输入。内容有变化才递增版本（使旧结论失效）；无变化保持原版本。

        expected_version 不为 None 时做原子乐观并发校验：必须在同一把锁内
        先确认当前版本仍等于 expected_version 才允许写入，否则抛 ConflictError，
        杜绝“先检查后写入”的 TOCTOU 竞态（两个请求同时通过检查）。

        返回 (version, changed, fingerprint)。fingerprint 为本次保存内容的
        规范 SHA-256，供客户端把“已保存版本 ↔ 内容指纹”绑定起来。
        """
        with self._lock:
            current_version = self._version()
            if (
                expected_version is not None
                and expected_version != current_version
            ):
                raise ConflictError(expected_version, current_version)
            current = {
                "tables": self._catalog(),
                "sql": self._conn.execute(
                    "SELECT sql FROM state WHERE id = 1"
                ).fetchone()["sql"],
            }
            incoming = {"tables": tables, "sql": sql}
            fp = fingerprint(tables, sql)
            if current == incoming:
                # 内容无变化：校正历史空指纹并保持版本不变
                with self._conn:
                    self._conn.execute(
                        "UPDATE state SET fingerprint = ? WHERE id = 1", (fp,)
                    )
                return current_version, False, fp
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
                self._conn.execute(
                    "UPDATE state SET sql = ?, fingerprint = ? WHERE id = 1",
                    (sql, fp),
                )
                self._conn.execute(
                    "UPDATE meta SET value = CAST(value AS INTEGER) + 1 "
                    "WHERE key = 'version'"
                )
            return self._version(), True, fp

    def save_analysis(self, version: int, status: str, result: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO analysis(version, status, result_json,"
                " created_at) VALUES(?, ?, ?, ?)",
                (version, status, json.dumps(result, ensure_ascii=False), _now()),
            )
