"""FastAPI 应用：字段目录 + SQL 录入、涉敏血缘核查、受控下载。

接口：
  GET  /api/state     当前版本、目录、SQL、绑定当前版本的核查结论
  PUT  /api/input     保存目录与 SQL（有改动则版本递增，旧结论立即失效）
  POST /api/analyze   对当前保存的输入执行核查，结论绑定当前版本
  GET  /api/download  仅当前版本存在 pass 结论时允许下载，否则 409
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from .analyzer import analyze
from .db import Store

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_TABLES = 4
MAX_COLUMNS_PER_TABLE = 200
MAX_NAME_LEN = 64
MAX_SQL_LEN = 50000

STATIC_DIR = Path(__file__).parent / "static"


class ColumnIn(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_LEN)
    sensitive: bool = False


class TableIn(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_LEN)
    columns: list[ColumnIn] = Field(min_length=1, max_length=MAX_COLUMNS_PER_TABLE)


class CatalogIn(BaseModel):
    tables: list[TableIn] = Field(max_length=MAX_TABLES)


class InputIn(BaseModel):
    catalog: CatalogIn
    sql: str = Field(max_length=MAX_SQL_LEN)


def _validate_catalog(tables: list[TableIn]) -> list[str]:
    errors: list[str] = []
    seen_tables: set[str] = set()
    for t in tables:
        name = t.name.strip()
        if not IDENT_RE.match(name):
            errors.append(
                f"表名 {t.name!r} 非法：仅支持字母、数字、下划线，且不以数字开头"
            )
        key = name.lower()
        if key in seen_tables:
            errors.append(f"表名重复：{name}")
        seen_tables.add(key)
        seen_cols: set[str] = set()
        for c in t.columns:
            cname = c.name.strip()
            if not IDENT_RE.match(cname):
                errors.append(
                    f"列名 {c.name!r}（表 {name}）非法：仅支持字母、数字、下划线，且不以数字开头"
                )
            ckey = cname.lower()
            if ckey in seen_cols:
                errors.append(f"表 {name} 中列名重复：{cname}")
            seen_cols.add(ckey)
    return errors


def _normalize(tables: list[TableIn]) -> list[dict]:
    return [
        {
            "name": t.name.strip(),
            "columns": [
                {"name": c.name.strip(), "sensitive": bool(c.sensitive)}
                for c in t.columns
            ],
        }
        for t in tables
    ]


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or os.environ.get("DB_PATH", "data/app.db")
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    store = Store(path)

    app = FastAPI(title="遥测 SQL 涉敏血缘核查", version="1.0.0")
    app.state.store = store

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    def get_state() -> dict:
        return store.get_state()

    @app.put("/api/input")
    def put_input(body: InputIn) -> dict:
        errors = _validate_catalog(body.catalog.tables)
        if errors:
            raise HTTPException(status_code=400, detail=errors)
        tables = _normalize(body.catalog.tables)
        version, changed = store.save_input(tables, body.sql)
        return {"version": version, "changed": changed, "state": store.get_state()}

    @app.post("/api/analyze")
    def post_analyze() -> dict:
        state = store.get_state()
        result = analyze(state["sql"], state["catalog"]["tables"])
        store.save_analysis(state["version"], result["status"], result)
        return {
            "version": state["version"],
            "status": result["status"],
            "result": result,
        }

    @app.get("/api/download")
    def download() -> Response:
        state = store.get_state()
        analysis = state["analysis"]
        if analysis is None:
            raise HTTPException(
                status_code=409,
                detail="当前输入尚未核查通过（输入可能已变更，旧结论已失效），请先执行核查",
            )
        if analysis["status"] != "pass":
            raise HTTPException(
                status_code=409,
                detail=f"当前输入核查结论为 {analysis['status']}，禁止下载；请修改后重新核查",
            )
        return Response(
            content=state["sql"],
            media_type="application/sql; charset=utf-8",
            headers={
                "Content-Disposition": "attachment; filename=telemetry_query.sql"
            },
        )

    return app


app = create_app()
