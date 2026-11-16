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

## 结论与输入绑定（防串窗 / 防竞态）

核心是“**版本号 + 内容指纹（SHA-256）**”双重乐观并发绑定，前后端对同一规范
序列化算同一指纹（见 `app/concurrency.py` 与 `app/static/app/core.js`）：

- 服务端只保存唯一一份“当前输入”（目录 + SQL），任何改动使版本号单调递增，
  并持久化该内容的指纹；核查结论按版本号存放。
- 保存（`PUT /api/input`）携带 `base_version`，存储层在**同一把锁内原子**完成
  “版本校验 + 写入”：已被另一窗口更新取代时返回 **409 且状态不变**，
  杜绝先检查后写入的 TOCTOU 竞态（并发保存只有一个能成功）。
- 核查（`POST /api/analyze`）与下载（`GET /api/download`）都要求携带
  `version` 与 `fingerprint`：版本或指纹对不上当前已保存输入即 **409 明确拒绝**，
  不能核查或下载另一份查询；下载响应还带 `X-Content-Fingerprint` 头，
  前端落盘前做“请求指纹 = 响应头指纹 = 按下载内容重算指纹”三方核验，
  不放行任何不匹配文件。
- 前端（`app/static/app/app.js` + `core.js`）：
  - 区分“已保存版本”与“本地草稿”；**任何本地未保存改动立即令旧结论失效**；
  - 每个保存/核查/下载/刷新动作分配单调递增的**请求世代号**，迟到响应只作
    过期处理，绝不清除新草稿、不恢复过期通过；等待响应期间仍可继续编辑；
  - 轮询发现他窗更新只提示并阻断操作，**不自动覆盖本地草稿**；用户可点
    “刷新为当前输入”显式对齐（有草稿时二次确认），之后可重新保存、核查、下载，
    涉密判定仍由分析器给出，保持不变。
- 输入一变，旧通过立即失效；核查失败不会沿用旧结论；只有“当前版本 +
  指纹一致 + 结论为 pass”三者同时成立才允许下载。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 当前版本、内容指纹、目录、SQL、绑定当前版本的结论 |
| PUT | `/api/input` | 保存目录与 SQL；带 `base_version` 做原子乐观并发校验，冲突返回 409 |
| POST | `/api/analyze` | body 带 `version`/`fingerprint`，仅绑定当前已保存输入时核查，否则 409 |
| GET | `/api/download` | 带 `version`/`fingerprint`，三者一致且结论 pass 才返回 SQL 附件（带指纹头），否则 409 |

## 目录结构

```
app/
  analyzer.py        # sqlglot 血缘/影响路径分析器（核心）
  concurrency.py     # 规范序列化与 SHA-256 内容指纹（与前端对齐）
  db.py              # SQLite：版本、指纹、目录、SQL、按版本存放的结论；原子乐观锁与旧库迁移
  main.py            # FastAPI 接口：版本/指纹绑定、409 冲突、静态资源
  static/
    index.html       # 操作页面结构
    app/core.js      # 纯逻辑：指纹、请求世代、结论有效性、操作门禁（可单测）
    app/app.js       # UI 接线：草稿、迟到响应、冲突提示、下载三方核验
tests/
  test_analyzer.py   # 分析器单测
  test_api.py        # API 集成测试（状态机、失效、双窗口、原子并发、迁移）
  web/core.test.js   # 前端纯逻辑单测（node --test）
  web/app.smoke.test.js  # 前端 DOM 桩冒烟测试（迟到响应/草稿保留/冲突提示）
Dockerfile / docker-compose.yml
```

## 测试

```bash
# 后端
pytest tests/ -q
# 前端（Node 18+ 内置测试运行器，无需依赖）
node --test tests/web/*.test.js
```
