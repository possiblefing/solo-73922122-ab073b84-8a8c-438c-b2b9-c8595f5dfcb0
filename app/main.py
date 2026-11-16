"""FastAPI 应用：字段目录 + SQL 录入、涉敏血缘核查、受控下载。

接口（均围绕“版本 + 内容指纹”的显式绑定，修复多窗口交错竞态）：
  GET  /api/state     当前版本、指纹、目录、SQL、绑定当前版本的核查结论
  PUT  /api/input     保存目录与 SQL。携带 base_version 时做乐观并发校验：
                      若已被另一窗口的更新取代（当前版本 != base_version），
                      返回 409 且服务端状态不变，由前端保留本地草稿；
                      内容无变化则版本不递增。
  POST /api/analyze   body 携带 version（+ 可选 fingerprint）。仅当版本与指纹
                      都对得上当前已保存输入时才核查，结论绑定该版本；
                      否则 409 明确拒绝，绝不核查或沿用别的查询。
  GET  /api/download?version=&fingerprint=
                      仅当版本、指纹与当前已保存输入一致且该版本结论为 pass 时
                      才放行，且下载内容就是该指纹对应的 SQL；否则 409。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .analyzer import analyze
from .concurrency import fingerprint as compute_fingerprint
from .db import ConflictError, Store

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
    # 乐观并发：客户端保存时所依据的已保存版本。缺省（旧客户端）时不做版本校验，
    # 保持向后兼容；新前端始终携带。
    base_version: int | None = Field(default=None, ge=0)


class AnalyzeIn(BaseModel):
    # 客户端要求核查的已保存版本；缺省时退化为“核查当前版本”（旧客户端兼容）
    version: int | None = Field(default=None, ge=0)
    # 客户端认知的内容指纹；提供时必须与当前已保存输入一致，否则拒绝
    fingerprint: str | None = Field(default=None, max_length=64)


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

    app = FastAPI(title="遥测 SQL 涉敏血缘核查", version="1.1.0")
    app.state.store = store

    def _conflict(detail: str) -> HTTPException:
        """统一的 409 冲突：明确拒绝且不写入；前端随后拉取 /api/state 对齐事实。"""
        return HTTPException(status_code=409, detail=detail)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="static",
    )

    @app.get("/api/state")
    def get_state() -> dict:
        return store.get_state()

    @app.put("/api/input")
    def put_input(body: InputIn) -> dict:
        errors = _validate_catalog(body.catalog.tables)
        if errors:
            raise HTTPException(status_code=400, detail=errors)
        tables = _normalize(body.catalog.tables)
        # 乐观并发校验与写入在存储层同一把锁内原子完成：
        # 被另一窗口取代时抛 ConflictError，服务端状态保持不变
        try:
            version, changed, fp = store.save_input(
                tables, body.sql, expected_version=body.base_version
            )
        except ConflictError as exc:
            raise _conflict(
                f"保存被拒绝：该窗口依据的是 v{exc.base_version}，"
                f"当前已保存输入已被其他窗口更新到 v{exc.current_version}。"
                "本地草稿已保留，请确认后选择“刷新为当前输入”或重新保存。"
            ) from exc
        return {
            "version": version,
            "changed": changed,
            "fingerprint": fp,
            "state": store.get_state(),
        }

    @app.post("/api/analyze")
    def post_analyze(body: AnalyzeIn | None = None) -> dict:
        body = body or AnalyzeIn()
        state = store.get_state()
        want_version = body.version if body.version is not None else state["version"]
        # 版本绑定：窗口要求核查的版本必须仍是当前已保存版本，
        # 被另一窗口更新取代时明确拒绝，不能去核查另一份查询。
        if want_version != state["version"]:
            raise _conflict(
                f"核查被拒绝：该窗口要求核查 v{want_version}，"
                f"但当前已保存输入已被其他窗口更新到 v{state['version']}。"
                "请刷新确认当前输入后重新保存、核查。"
            )
        # 指纹绑定：版本相同也要确认内容就是窗口认知的那一份，杜绝放行不匹配内容
        if body.fingerprint is not None and body.fingerprint != state["fingerprint"]:
            raise _conflict(
                "核查被拒绝：请求携带的内容指纹与当前已保存输入不一致，"
                "请刷新确认当前输入后重新保存、核查。"
            )
        result = analyze(state["sql"], state["catalog"]["tables"])
        store.save_analysis(state["version"], result["status"], result)
        return {
            "version": state["version"],
            "fingerprint": state["fingerprint"],
            "status": result["status"],
            "result": result,
        }

    @app.get("/api/download")
    def download(
        version: int | None = None, fingerprint: str | None = None
    ) -> Response:
        state = store.get_state()
        analysis = state["analysis"]
        # 1) 版本绑定：只能下载该窗口已保存且仍为当前版本的那一份
        if version is not None and version != state["version"]:
            raise _conflict(
                f"下载被拒绝：该窗口依据的是 v{version}，"
                f"当前已保存输入已被其他窗口更新到 v{state['version']}。"
                "请对当前输入重新保存、核查通过后再下载。"
            )
        # 2) 指纹绑定：双重确认下载内容与窗口认知一致，不放行不匹配文件
        if fingerprint is not None and fingerprint != state["fingerprint"]:
            raise _conflict(
                "下载被拒绝：请求的内容指纹与当前已保存输入不一致，"
                "请刷新确认后重新保存、核查。"
            )
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
        # 3) 最终防线：实际返回内容的指纹必须等于已绑定指纹（无请求指纹时也校验自洽）
        actual_fp = compute_fingerprint(
            state["catalog"]["tables"], state["sql"]
        )
        if actual_fp != state["fingerprint"]:
            raise HTTPException(
                status_code=409,
                detail="下载被拒绝：服务端内容指纹自校验失败，请重新保存与核查",
            )
        return Response(
            content=state["sql"],
            media_type="application/sql; charset=utf-8",
            headers={
                "Content-Disposition": "attachment; filename=telemetry_query.sql",
                "X-Content-Fingerprint": state["fingerprint"],
            },
        )

    return app


app = create_app()
