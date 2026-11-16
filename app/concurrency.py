"""输入内容的规范指纹与并发绑定工具。

修复要点：
  - 输入（字段目录 + SQL）的规范序列化在前端（JS）与后端（Python）必须完全一致，
    这样两端可对“同一份已保存输入”算出同一个 SHA-256 指纹。
  - 保存 / 核查 / 下载三个动作都显式携带客户端所依据的版本号与内容指纹，
    服务端据此判断该窗口的认知是否已被另一窗口的更新取代：
      * 版本不匹配 → 409，明确拒绝，服务端状态不动（本地草稿由前端保留）；
      * 版本匹配但指纹不匹配 → 同样 409（防御性，正常情况下不会发生）。
  - 下载返回的文件内容指纹必须与请求指纹一致，杜绝放行不匹配文件。

规范序列化规则（与 app/static/app.js 的 canonicalFingerprint 严格对齐）：
  JSON.stringify([tables, sql], 键名固定、无多余空白)，
  其中 tables 为 [[表名, [[列名, 0|1], ...]], ...]，列名/表名按保存前的
  strip 规则处理；对序列化后的 UTF-8 字节计算 SHA-256 十六进制摘要。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_bytes(tables: list[dict], sql: str) -> bytes:
    """与前端一致的规范字节序列。

    tables: 已 normalize 的目录 [{"name": str, "columns": [{"name", "sensitive"}]}]
    """
    compact_tables = [
        [
            t["name"],
            [[c["name"], 1 if c["sensitive"] else 0] for c in t["columns"]],
        ]
        for t in tables
    ]
    # ensure_ascii=False：与 JS TextEncoder 对非 ASCII 的 UTF-8 编码保持一致；
    # separators 去掉所有可选空白，等价于 JS JSON.stringify 的紧凑输出；
    # sort_keys=False：键顺序由我们自己固定（表名、列对），不依赖排序。
    payload = json.dumps(
        [compact_tables, sql],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return payload.encode("utf-8")


def fingerprint(tables: list[dict], sql: str) -> str:
    """返回输入内容的 SHA-256 十六进制指纹。"""
    return hashlib.sha256(canonical_bytes(tables, sql)).hexdigest()


def state_fingerprint(state: dict[str, Any]) -> str:
    """对 get_state() 返回的结构计算指纹。"""
    return fingerprint(state["catalog"]["tables"], state["sql"])
