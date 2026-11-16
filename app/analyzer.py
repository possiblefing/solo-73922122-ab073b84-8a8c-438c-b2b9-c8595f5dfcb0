"""涉敏字段血缘与影响路径分析器。

基于 sqlglot 解析单条 SELECT 查询（支持非递归 CTE、别名、星号、JOIN、
WHERE、GROUP BY、HAVING、聚合、UNION ALL），结合字段目录计算每个输出列的：
  - 原始来源（直接取值的基表字段）
  - 影响路径（经 WHERE 筛选 / JOIN 连接 / GROUP BY 分组 / HAVING / CTE / UNION ALL 传来）

判定规则：任一来源（含影响路径上的字段）涉敏，该输出列即涉敏；
聚合不视为脱敏。未知列、歧义引用、不支持的语法一律明确拒绝（AnalysisError）。

关键设计：
  - value_lineage：本层直接取值来源（不含本层筛选/连接/分组标记），
    供 GROUP BY 序号/别名、HAVING 别名引用，避免角色标记串层污染。
  - row_lineage：影响“行集”的血缘（本层 WHERE/JOIN/GROUP BY/HAVING 及
    CTE 来源的行集血缘）。COUNT(*) 等不引用任何列的输出也会被行集血缘影响，
    例如 SELECT COUNT(*) FROM (被涉敏字段筛选过的 CTE) 必须判定为涉敏。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

DIALECT = "sqlite"

VIA_DIRECT = "直接来源"
VIA_JOIN = "JOIN 连接"
VIA_WHERE = "WHERE 筛选"
VIA_GROUP = "GROUP BY 分组"
VIA_HAVING = "HAVING 筛选"
VIA_UNION = "UNION ALL 合并"
VIA_CTE_PREFIX = "经由 CTE "

_VIA_ORDER = [VIA_DIRECT, VIA_JOIN, VIA_WHERE, VIA_GROUP, VIA_HAVING, VIA_UNION]


def _via_rank(via: str) -> int:
    try:
        return _VIA_ORDER.index(via)
    except ValueError:
        return len(_VIA_ORDER)  # CTE 标记排在最后


class AnalysisError(Exception):
    """对用户输入的明确拒绝（未知列 / 歧义引用 / 不支持的语法等）。"""


@dataclass
class SourceRef:
    """一条血缘记录：基表字段 + 到达输出列的途径集合。"""

    table: str
    column: str
    sensitive: bool
    via: set[str] = field(default_factory=set)


# 血缘：{(表名小写, 列名小写): SourceRef}
Lineage = dict[tuple[str, str], SourceRef]


@dataclass
class ScopeColumn:
    name: str
    lineage: Lineage


@dataclass
class Source:
    qualifier: str  # 小写限定名（别名或表名）
    display: str
    columns: list[ScopeColumn]
    row_lineage: Lineage | None = None  # 仅 CTE 来源携带：影响其行集的血缘


@dataclass
class OutputColumn:
    name: str
    expression: str
    is_aggregate: bool
    value_lineage: Lineage  # 直接取值来源（不含本层角色标记）
    lineage: Lineage  # 完整血缘（含本层影响路径），用于展示与判定


@dataclass
class QueryResult:
    columns: list[OutputColumn]
    row_lineage: Lineage  # 影响该查询行集的血缘


def _copy_lineage(lin: Lineage) -> Lineage:
    return {k: SourceRef(v.table, v.column, v.sensitive, set(v.via)) for k, v in lin.items()}


def _merge_into(dst: Lineage, src: Lineage, via: str | None = None) -> None:
    """把 src 血缘合并进 dst（拷贝，不共享引用），可选追加途径标记。"""
    for key, ref in src.items():
        cur = dst.get(key)
        if cur is None:
            dst[key] = SourceRef(ref.table, ref.column, ref.sensitive, set(ref.via))
            cur = dst[key]
        else:
            cur.via |= ref.via
        if via:
            cur.via.add(via)


def _tag_via(lin: Lineage, via: str) -> Lineage:
    out = _copy_lineage(lin)
    for ref in out.values():
        ref.via.add(via)
    return out


class Catalog:
    """字段目录：最多 4 张表，每列带涉敏标记。名称匹配大小写不敏感。"""

    def __init__(self, tables: list[dict]):
        self.tables: dict[str, dict] = {}
        for t in tables:
            cols = {}
            for c in t["columns"]:
                cols[c["name"].lower()] = {
                    "display": c["name"],
                    "sensitive": bool(c["sensitive"]),
                }
            self.tables[t["name"].lower()] = {"display": t["name"], "columns": cols}

    def get(self, name_lower: str) -> dict | None:
        return self.tables.get(name_lower)


def analyze(sql_text: str, catalog_tables: list[dict]) -> dict:
    """入口：返回 {status: pass|blocked|error, columns|errors, summary}。"""
    try:
        result = _analyze_inner(sql_text, Catalog(catalog_tables))
    except AnalysisError as exc:
        return {"status": "error", "errors": [str(exc)], "columns": []}
    return _to_response(result)


def _analyze_inner(sql_text: str, catalog: Catalog) -> QueryResult:
    if not sql_text or not sql_text.strip():
        raise AnalysisError("SQL 为空，请输入一条 SELECT 查询")
    try:
        statements = [s for s in sqlglot.parse(sql_text, dialect=DIALECT) if s is not None]
    except sqlglot.errors.ParseError as exc:
        raise AnalysisError(f"SQL 解析失败：{exc}") from exc
    if len(statements) != 1:
        raise AnalysisError("仅支持单条 SELECT 语句，不支持多条语句")
    tree = _normalize(statements[0])
    if not isinstance(tree, (exp.Select, exp.Union)):
        raise AnalysisError(
            f"仅支持 SELECT 查询（可含 CTE / UNION ALL），不支持 {type(tree).__name__}"
        )
    _validate_supported(tree)
    return _analyze_query(tree, {}, catalog)


def _unwrap_paren(node: exp.Expression) -> exp.Expression:
    """查询位置上的括号/子查询包装：还原为内部查询。"""
    while isinstance(node, (exp.Paren, exp.Subquery)) and isinstance(
        node.this, (exp.Select, exp.Union, exp.Paren, exp.Subquery)
    ):
        node = node.this
    return node


def _normalize(node: exp.Expression) -> exp.Expression:
    """把查询位置（根 / UNION 分支 / CTE 体）上的括号包装就地还原，
    之后残留的 exp.Subquery 一律视为不支持的子查询。"""
    node = _unwrap_paren(node)
    if isinstance(node, exp.Union):
        node.set("this", _normalize(node.this))
        node.set("expression", _normalize(node.expression))
    with_ = node.args.get("with_")
    if with_ is not None:
        for cte in with_.expressions:
            cte.set("this", _normalize(cte.this))
    return node


def _validate_supported(tree: exp.Expression) -> None:
    """全树检查不支持的语法，明确拒绝。"""
    for node in tree.walk():
        if isinstance(node, exp.Subquery):
            raise AnalysisError("不支持子查询（标量 / 派生表 / IN 等），请改用 CTE")
        if isinstance(node, exp.Window):
            raise AnalysisError("不支持窗口函数（OVER ...）")
        if isinstance(node, exp.Exists):
            raise AnalysisError("不支持 EXISTS 子查询")
        if isinstance(node, exp.In) and node.args.get("query") is not None:
            raise AnalysisError("不支持 IN (SELECT ...) 子查询")
        if isinstance(node, (exp.Intersect, exp.Except)):
            raise AnalysisError("仅支持 UNION ALL，不支持 INTERSECT / EXCEPT")
        if isinstance(node, exp.Union):
            if node.args.get("distinct"):
                raise AnalysisError("仅支持 UNION ALL，不支持 UNION（去重）")
            if node.args.get("by_name"):
                raise AnalysisError("不支持 UNION ... BY NAME")
        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise AnalysisError("不支持递归 CTE（WITH RECURSIVE）")
        if isinstance(node, exp.Join):
            method = (node.args.get("method") or "").upper()
            if method == "NATURAL":
                raise AnalysisError("不支持 NATURAL JOIN，请显式写出 JOIN ... ON 条件")
            if node.args.get("using"):
                raise AnalysisError("不支持 JOIN ... USING(...)，请改用 JOIN ... ON ...")
        if isinstance(node, exp.Table) and (node.args.get("db") or node.args.get("catalog")):
            raise AnalysisError("不支持 db.table 形式的限定表名")
        if isinstance(node, exp.Column) and (
            node.args.get("db") or node.args.get("catalog")
        ):
            raise AnalysisError("不支持三段式限定列名（db.table.column）")
        if isinstance(node, (exp.Rollup, exp.Cube, exp.GroupingSets)):
            raise AnalysisError("不支持 ROLLUP / CUBE / GROUPING SETS")
        if isinstance(node, (exp.Qualify, exp.Pivot, exp.TableSample, exp.Lateral)):
            raise AnalysisError(
                f"不支持的语法：{type(node).__name__}（QUALIFY / PIVOT / TABLESAMPLE / LATERAL）"
            )


def _analyze_query(node: exp.Expression, env: dict, catalog: Catalog) -> QueryResult:
    """分析一个查询节点（Select / Union），env 为可见的 CTE 环境。"""
    node = _unwrap_paren(node)

    with_ = node.args.get("with_")
    if with_ is not None:
        env = dict(env)
        seen: set[str] = set()
        for cte in with_.expressions:
            name = cte.alias
            if not name:
                raise AnalysisError("CTE 缺少名称")
            key = name.lower()
            if key in seen:
                raise AnalysisError(f"CTE 名称重复：{name}")
            seen.add(key)
            # 非递归：CTE 体只能引用此前定义的 CTE 与目录表
            sub = _analyze_query(cte.this, env, catalog)
            cols = list(sub.columns)
            alias_arg = cte.args.get("alias")
            rename = alias_arg.args.get("columns") if alias_arg else None
            if rename:
                names = [c.name for c in rename]
                if len(names) != len(cols):
                    raise AnalysisError(
                        f"CTE {name} 的列别名数量（{len(names)}）与查询列数（{len(cols)}）不一致"
                    )
                cols = [
                    OutputColumn(n, c.expression, c.is_aggregate, c.value_lineage, c.lineage)
                    for n, c in zip(names, cols)
                ]
            # 打上“经由 CTE”标记后再发布到环境
            cols = [
                OutputColumn(
                    c.name,
                    c.expression,
                    c.is_aggregate,
                    _tag_via(c.value_lineage, VIA_CTE_PREFIX + name),
                    _tag_via(c.lineage, VIA_CTE_PREFIX + name),
                )
                for c in cols
            ]
            env[key] = {
                "display": name,
                "columns": cols,
                "row_lineage": _tag_via(sub.row_lineage, VIA_CTE_PREFIX + name),
            }

    if isinstance(node, exp.Select):
        return _analyze_select(node, env, catalog)
    if isinstance(node, exp.Union):
        left = _analyze_query(node.this, env, catalog)
        right = _analyze_query(node.expression, env, catalog)
        if len(left.columns) != len(right.columns):
            raise AnalysisError(
                f"UNION ALL 两侧列数不一致（左 {len(left.columns)} 列，右 {len(right.columns)} 列）"
            )
        merged_cols = []
        for lc, rc in zip(left.columns, right.columns):
            value = _copy_lineage(lc.value_lineage)
            _merge_into(value, rc.value_lineage)
            full = _copy_lineage(lc.lineage)
            _merge_into(full, rc.lineage)
            merged_cols.append(
                OutputColumn(
                    lc.name,
                    lc.expression,
                    lc.is_aggregate or rc.is_aggregate,
                    _tag_via(value, VIA_UNION),
                    _tag_via(full, VIA_UNION),
                )
            )
        row = _copy_lineage(left.row_lineage)
        _merge_into(row, right.row_lineage)
        return QueryResult(merged_cols, _tag_via(row, VIA_UNION))
    raise AnalysisError(f"仅支持 SELECT / UNION ALL 查询，不支持 {type(node).__name__}")


def _build_source(node: exp.Expression, env: dict, catalog: Catalog) -> Source:
    if not isinstance(node, exp.Table):
        raise AnalysisError("FROM / JOIN 中仅支持已登记的表或 CTE，不支持派生子查询或表函数")
    name = node.name
    if not name:
        raise AnalysisError("FROM / JOIN 中仅支持已登记的表或 CTE，不支持表函数")
    display = node.alias_or_name
    key = name.lower()
    if key in env:  # CTE 优先于目录表（与 SQLite 语义一致）
        cte = env[key]
        return Source(
            qualifier=display.lower(),
            display=display,
            columns=[ScopeColumn(c.name, _copy_lineage(c.lineage)) for c in cte["columns"]],
            row_lineage=_copy_lineage(cte["row_lineage"]),
        )
    cat = catalog.get(key)
    if cat is None:
        raise AnalysisError(f"未知表：{name}（未在字段目录登记，也不是已定义的 CTE）")
    cols = []
    for col_key, col in cat["columns"].items():
        lin: Lineage = {
            (key, col_key): SourceRef(cat["display"], col["display"], col["sensitive"], set())
        }
        cols.append(ScopeColumn(col["display"], lin))
    return Source(qualifier=display.lower(), display=display, columns=cols)


def _resolve(scope: list[Source], col: exp.Column) -> Lineage | None:
    """解析列引用。返回 None 表示未找到；歧义引用抛 AnalysisError。"""
    name = col.name
    qual = col.table
    if qual:
        matches = [s for s in scope if s.qualifier == qual.lower()]
        if not matches:
            return None
        if len(matches) > 1:
            raise AnalysisError(f"限定名 {qual} 对应多个来源，引用存在歧义")
        src = matches[0]
        hits = [c for c in src.columns if c.name.lower() == name.lower()]
        if not hits:
            return None
        if len(hits) > 1:
            raise AnalysisError(f"列 {qual}.{name} 在来源中存在歧义")
        return hits[0].lineage
    hits = [(s, c) for s in scope for c in s.columns if c.name.lower() == name.lower()]
    if not hits:
        return None
    if len(hits) > 1:
        where = "、".join(sorted({s.display for s, _ in hits}))
        raise AnalysisError(f"列引用存在歧义：{name}（同时出现在 {where}）")
    return hits[0][1].lineage


def _describe_col(col: exp.Column) -> str:
    return col.sql(dialect=DIALECT)


def _columns_lineage(node: exp.Expression, scope: list[Source]) -> Lineage:
    """收集表达式中全部列引用的血缘（不追加本层角色标记）。
    表达式中的子查询在此拒绝。"""
    for _ in node.find_all(exp.Subquery):
        raise AnalysisError("不支持表达式中的子查询，请改用 CTE")
    lin: Lineage = {}
    for col in node.find_all(exp.Column):
        if isinstance(col.this, exp.Star):
            continue
        resolved = _resolve(scope, col)
        if resolved is None:
            raise AnalysisError(f"未知列：{_describe_col(col)}")
        _merge_into(lin, resolved)
    return lin


def _analyze_select(select: exp.Select, env: dict, catalog: Catalog) -> QueryResult:
    scope: list[Source] = []
    influences: list[tuple[str | None, Lineage]] = []

    from_ = select.args.get("from_")
    if from_ is not None and from_.this is not None:
        scope.append(_build_source(from_.this, env, catalog))

    for join in select.args.get("joins") or []:
        scope.append(_build_source(join.this, env, catalog))
        on = join.args.get("on")
        if on is not None:
            influences.append((VIA_JOIN, _columns_lineage(on, scope)))

    # CTE 来源的行集血缘影响本查询全部输出（如 COUNT(*) 不引用任何列时尤为关键）
    for s in scope:
        if s.row_lineage:
            influences.append((None, s.row_lineage))

    # ---- 输出列（先展开星号、算直接取值来源）----
    outputs: list[OutputColumn] = []

    def add_output(name: str, expr_sql: str, is_agg: bool, value: Lineage) -> None:
        outputs.append(OutputColumn(name, expr_sql, is_agg, value, {}))

    for idx, proj in enumerate(select.expressions):
        if isinstance(proj, exp.Star):
            if not scope:
                raise AnalysisError("SELECT * 缺少 FROM 来源")
            for s in scope:
                for c in s.columns:
                    add_output(c.name, "*", False, _copy_lineage(c.lineage))
            continue
        if isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star):
            qual = proj.table
            matches = [s for s in scope if s.qualifier == (qual or "").lower()]
            if not matches:
                raise AnalysisError(f"未知表或别名：{qual}")
            if len(matches) > 1:
                raise AnalysisError(f"限定名 {qual} 对应多个来源，{qual}.* 存在歧义")
            for c in matches[0].columns:
                add_output(c.name, f"{qual}.*", False, _copy_lineage(c.lineage))
            continue
        if isinstance(proj, exp.Alias):
            name, inner = proj.alias, proj.this
            if not name:
                raise AnalysisError("存在缺少别名的表达式")
        elif isinstance(proj, exp.Column):
            name, inner = proj.name, proj
        else:
            name, inner = f"expr_{idx + 1}", proj
        value = _columns_lineage(inner, scope)
        is_agg = any(True for _ in inner.find_all(exp.AggFunc))
        add_output(name, inner.sql(dialect=DIALECT), is_agg, value)

    # ---- 筛选 / 连接 / 分组影响 ----
    where = select.args.get("where")
    if where is not None:
        influences.append((VIA_WHERE, _columns_lineage(where.this, scope)))

    group_lin: Lineage = {}
    group = select.args.get("group")
    if group is not None:
        for g in group.expressions:
            _merge_into(group_lin, _group_lineage(g, scope, outputs))
        influences.append((VIA_GROUP, group_lin))

    having = select.args.get("having")
    if having is not None:
        influences.append((VIA_HAVING, _having_lineage(having.this, scope, outputs, group_lin)))

    # ---- 合成最终血缘与行集血缘 ----
    row_lineage: Lineage = {}
    for via, lin in influences:
        _merge_into(row_lineage, lin, via)

    final: list[OutputColumn] = []
    for out in outputs:
        full = _copy_lineage(out.value_lineage)
        # “直接来源”只标记本层新引入（尚无途径标记）的来源；
        # 经 CTE 等携带来的影响来源保留其原始途径，不重复标记。
        for ref in full.values():
            if not ref.via:
                ref.via.add(VIA_DIRECT)
        for via, lin in influences:
            _merge_into(full, lin, via)
        final.append(
            OutputColumn(out.name, out.expression, out.is_aggregate, out.value_lineage, full)
        )
    return QueryResult(final, row_lineage)


def _group_lineage(
    g: exp.Expression, scope: list[Source], outputs: list[OutputColumn]
) -> Lineage:
    """GROUP BY 项：支持列引用、输出别名、序号（1 起）与常量。"""
    if isinstance(g, exp.Literal):
        if g.args.get("is_string"):
            return {}  # 字符串常量：不影响分组来源
        try:
            pos = int(g.this)
        except (TypeError, ValueError):
            return {}
        if 1 <= pos <= len(outputs):
            return _copy_lineage(outputs[pos - 1].value_lineage)
        raise AnalysisError(f"GROUP BY 序号越界：{pos}（共 {len(outputs)} 个输出列）")
    if isinstance(g, exp.Column) and not isinstance(g.this, exp.Star):
        resolved = _resolve(scope, g)
        if resolved is not None:
            return _copy_lineage(resolved)
        alias_hits = [o for o in outputs if o.name.lower() == g.name.lower()]
        if len(alias_hits) == 1:
            return _copy_lineage(alias_hits[0].value_lineage)
        if len(alias_hits) > 1:
            raise AnalysisError(f"GROUP BY 引用的输出别名存在歧义：{g.name}")
        raise AnalysisError(f"未知列：{_describe_col(g)}（GROUP BY 中）")
    return _columns_lineage(g, scope)


def _having_lineage(
    node: exp.Expression,
    scope: list[Source],
    outputs: list[OutputColumn],
    group_lin: Lineage,
) -> Lineage:
    for _ in node.find_all(exp.Subquery):
        raise AnalysisError("不支持 HAVING 中的子查询")
    lin: Lineage = {}
    for col in node.find_all(exp.Column):
        if isinstance(col.this, exp.Star):
            continue
        resolved = _resolve(scope, col)
        if resolved is None:
            alias_hits = [o for o in outputs if o.name.lower() == col.name.lower()]
            if len(alias_hits) == 1 and not col.table:
                # HAVING 引用输出别名：其取值来源 + 分组来源共同构成影响
                _merge_into(lin, alias_hits[0].value_lineage)
                _merge_into(lin, group_lin)
                continue
            if len(alias_hits) > 1:
                raise AnalysisError(f"HAVING 引用的输出别名存在歧义：{col.name}")
            raise AnalysisError(f"未知列：{_describe_col(col)}（HAVING 中）")
        _merge_into(lin, resolved)
    return lin


def _to_response(result: QueryResult) -> dict:
    columns = []
    sensitive_names = []
    for out in result.columns:
        refs = sorted(
            out.lineage.values(),
            key=lambda r: (not r.sensitive, r.table.lower(), r.column.lower()),
        )
        sources = [
            {
                "table": r.table,
                "column": r.column,
                "sensitive": r.sensitive,
                "via": sorted(r.via, key=_via_rank),
            }
            for r in refs
        ]
        sensitive = any(r.sensitive for r in refs)
        if sensitive:
            sensitive_names.append(out.name)
        columns.append(
            {
                "name": out.name,
                "expression": out.expression,
                "is_aggregate": out.is_aggregate,
                "sensitive": sensitive,
                "sources": sources,
            }
        )
    return {
        "status": "blocked" if sensitive_names else "pass",
        "columns": columns,
        "errors": [],
        "summary": {
            "output_columns": len(columns),
            "sensitive_columns": len(sensitive_names),
            "sensitive_column_names": sensitive_names,
        },
    }
