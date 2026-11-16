# 遥测 SQL 涉敏血缘核查

数仓工程师向合作方交付遥测取数 SQL 前的自查工具：录入字段目录与涉敏标记，
编辑一条 SELECT，系统逐输出列给出**原始来源**与**影响路径**（经筛选、连接、
分组传来）；任一来源涉敏即阻止下载 SQL，**聚合不视为脱敏**。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000
```

本地开发（Python 3.11+）：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
# 测试
pytest tests/ -q
```

## 功能与规则

- **字段目录**：至多 4 张表，逐列勾选涉敏标记；名称匹配不区分大小写。
- **SQL 支持范围**：单条 SELECT；非递归 CTE（含列重命名、多 CTE 链）、
  表/列别名、星号（`*`、`t.*`）、JOIN … ON（INNER/LEFT/RIGHT/FULL/CROSS、
  逗号连接）、WHERE、GROUP BY（含序号与输出别名）、HAVING、聚合函数、
  一般标量表达式/函数、UNION ALL、DISTINCT、ORDER BY / LIMIT。
- **明确拒绝**：未知表/列、歧义引用（含自连接未限定）、递归 CTE、
  子查询（标量/派生表/IN）、窗口函数、UNION（去重）/INTERSECT/EXCEPT、
  NATURAL JOIN、JOIN USING、三段式限定名、多条语句、非 SELECT 语句等。
- **影响路径**：输出列的血缘来源标注其到达途径——直接来源、JOIN 连接、
  WHERE 筛选、GROUP BY 分组、HAVING 筛选、UNION ALL 合并、经由 CTE。
  `COUNT(*)` 等不引用列的输出也会继承行集血缘（如 CTE 内部的涉敏筛选）。
- **判定**：任一输出列的任一来源（含影响路径）涉敏 → 整体 blocked，禁止下载。

## 结论与输入绑定（防串窗）

- 服务端保存唯一一份“当前输入”（目录 + SQL），任何改动使版本号单调递增。
- 核查结论绑定版本号；输入一变，旧通过立即失效；核查失败不会沿用旧结论。
- 两个窗口交错操作时，`GET /api/download` 只放行“当前版本核查通过”的 SQL，
  其余一律 409。前端每 3 秒轮询，发现其他窗口变更会提示并将旧结论置灰。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 当前版本、目录、SQL、绑定当前版本的结论 |
| PUT | `/api/input` | 保存目录与 SQL（有改动则版本递增） |
| POST | `/api/analyze` | 核查当前输入，结论绑定当前版本 |
| GET | `/api/download` | 仅当前版本结论为 pass 时返回 SQL 附件，否则 409 |

## 目录结构

```
app/
  analyzer.py        # sqlglot 血缘/影响路径分析器（核心）
  db.py              # SQLite 存储：版本、目录、SQL、按版本存放的结论
  main.py            # FastAPI 接口与目录校验
  static/index.html  # 操作页面（原生 JS）
tests/               # pytest：分析器单测 + API 集成测试（73 项）
Dockerfile / docker-compose.yml
```
