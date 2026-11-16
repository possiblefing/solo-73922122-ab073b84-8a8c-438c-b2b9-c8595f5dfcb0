"""API 集成测试：保存-核查-下载状态机、版本失效、双窗口交错。"""
import pytest
from fastapi.testclient import TestClient

from app.concurrency import fingerprint
from app.main import create_app

CATALOG = {
    "tables": [
        {
            "name": "events",
            "columns": [
                {"name": "event_id", "sensitive": False},
                {"name": "device_id", "sensitive": False},
                {"name": "precise_lat", "sensitive": True},
                {"name": "speed", "sensitive": False},
            ],
        },
        {
            "name": "devices",
            "columns": [
                {"name": "device_id", "sensitive": False},
                {"name": "home_geohash", "sensitive": True},
            ],
        },
    ]
}

SQL_PASS = "SELECT device_id, COUNT(*) AS c FROM events GROUP BY device_id"
SQL_BLOCKED = "SELECT AVG(precise_lat) AS a FROM events"
SQL_ERROR = "SELECT nope FROM events"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def put(client, sql=SQL_PASS, catalog=None):
    return client.put(
        "/api/input", json={"catalog": catalog or CATALOG, "sql": sql}
    )


def put_analyze(client, sql=SQL_PASS, catalog=None):
    r = put(client, sql, catalog)
    assert r.status_code == 200, r.text
    return client.post("/api/analyze")


# ---------- 基本流程 ----------

def test_full_flow_pass_and_download(client):
    r = put_analyze(client, SQL_PASS)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pass"
    assert body["version"] == 1
    d = client.get("/api/download")
    assert d.status_code == 200
    assert d.text == SQL_PASS
    assert "attachment" in d.headers["content-disposition"]


def test_blocked_no_download(client):
    r = put_analyze(client, SQL_BLOCKED)
    assert r.json()["status"] == "blocked"
    d = client.get("/api/download")
    assert d.status_code == 409
    assert "blocked" in d.json()["detail"]


def test_error_no_download(client):
    r = put_analyze(client, SQL_ERROR)
    assert r.json()["status"] == "error"
    assert "未知列" in r.json()["result"]["errors"][0]
    assert client.get("/api/download").status_code == 409


def test_download_before_analyze(client):
    put(client, SQL_PASS)
    r = client.get("/api/download")
    assert r.status_code == 409
    assert "核查" in r.json()["detail"]


def test_state_reflects_analysis(client):
    put_analyze(client, SQL_PASS)
    st = client.get("/api/state").json()
    assert st["version"] == 1
    assert st["analysis"]["status"] == "pass"
    assert st["analysis"]["result"]["summary"]["output_columns"] == 2


# ---------- 版本与失效 ----------

def test_change_invalidates_old_pass(client):
    put_analyze(client, SQL_PASS)
    # 修改 SQL → 版本递增 → 旧通过立即失效
    r = put(client, "SELECT device_id FROM events")
    assert r.json()["changed"] is True
    assert r.json()["version"] == 2
    st = client.get("/api/state").json()
    assert st["analysis"] is None  # 旧结论不再绑定当前版本
    assert client.get("/api/download").status_code == 409
    # 重新核查通过后可下载
    r = client.post("/api/analyze")
    assert r.json()["status"] == "pass"
    d = client.get("/api/download")
    assert d.status_code == 200
    assert d.text == "SELECT device_id FROM events"


def test_save_same_content_keeps_version(client):
    put_analyze(client, SQL_PASS)
    r = put(client, SQL_PASS)
    assert r.json()["changed"] is False
    assert r.json()["version"] == 1
    assert client.get("/api/download").status_code == 200


def test_catalog_change_invalidates(client):
    put_analyze(client, SQL_PASS)
    catalog = {
        "tables": [
            {
                "name": "events",
                "columns": [
                    {"name": "event_id", "sensitive": False},
                    {"name": "device_id", "sensitive": True},  # 改为涉敏
                    {"name": "precise_lat", "sensitive": True},
                    {"name": "speed", "sensitive": False},
                ],
            }
        ]
    }
    r = put(client, SQL_PASS, catalog)
    assert r.json()["changed"] is True
    assert client.get("/api/download").status_code == 409
    # 新目录下同一 SQL 核查被拦
    r = client.post("/api/analyze")
    assert r.json()["status"] == "blocked"


def test_failed_analysis_does_not_reuse_old_pass(client):
    # v1 通过 → 改成涉敏（v2）核查被拦 → 下载仍 409（不得沿用 v1 结论）
    put_analyze(client, SQL_PASS)
    assert client.get("/api/download").status_code == 200
    put(client, SQL_BLOCKED)
    r = client.post("/api/analyze")
    assert r.json()["status"] == "blocked"
    assert client.get("/api/download").status_code == 409
    # 改回通过的 SQL（v3）但未重新核查 → 仍 409（改动后旧通过已失效）
    put(client, SQL_PASS)
    assert client.get("/api/download").status_code == 409
    # 重新核查通过 → 200
    client.post("/api/analyze")
    assert client.get("/api/download").status_code == 200


def test_interleaved_two_windows(client):
    """模拟两个窗口交错操作：只能下载当前输入核查通过的 SQL。"""
    # 窗口 A：保存安全 SQL 并核查通过
    put_analyze(client, SQL_PASS)
    assert client.get("/api/download").status_code == 200
    # 窗口 B：改成涉敏 SQL（版本失效），B 自己核查 → 被拦
    put(client, SQL_BLOCKED)
    r = client.post("/api/analyze")
    assert r.json()["status"] == "blocked"
    # 窗口 A 拿着旧页面尝试下载 → 必须 409
    assert client.get("/api/download").status_code == 409
    # 窗口 B 改回安全 SQL（又一次变更）→ A 的旧结论依旧不可用
    put(client, SQL_PASS)
    assert client.get("/api/download").status_code == 409
    # 任一窗口对当前输入重新核查通过 → 可下载
    r = client.post("/api/analyze")
    assert r.json()["status"] == "pass"
    assert client.get("/api/download").status_code == 200


def test_version_monotonic(client):
    put(client, SQL_PASS)
    put(client, "SELECT device_id FROM events")
    put(client, SQL_PASS)  # 内容回到最初，但版本必须继续递增
    st = client.get("/api/state").json()
    assert st["version"] == 3


# ---------- 目录校验 ----------

def test_catalog_too_many_tables(client):
    catalog = {
        "tables": [
            {"name": f"t{i}", "columns": [{"name": "a", "sensitive": False}]}
            for i in range(5)
        ]
    }
    r = client.put("/api/input", json={"catalog": catalog, "sql": "SELECT 1"})
    assert r.status_code == 422  # pydantic max_length=4


def test_catalog_duplicate_table(client):
    catalog = {
        "tables": [
            {"name": "t1", "columns": [{"name": "a", "sensitive": False}]},
            {"name": "T1", "columns": [{"name": "a", "sensitive": False}]},
        ]
    }
    r = client.put("/api/input", json={"catalog": catalog, "sql": "SELECT 1"})
    assert r.status_code == 400
    assert "重复" in str(r.json()["detail"])


def test_catalog_duplicate_column(client):
    catalog = {
        "tables": [
            {
                "name": "t1",
                "columns": [
                    {"name": "a", "sensitive": False},
                    {"name": "A", "sensitive": True},
                ],
            }
        ]
    }
    r = client.put("/api/input", json={"catalog": catalog, "sql": "SELECT 1"})
    assert r.status_code == 400
    assert "重复" in str(r.json()["detail"])


def test_catalog_bad_identifier(client):
    catalog = {
        "tables": [
            {"name": "1bad", "columns": [{"name": "a", "sensitive": False}]}
        ]
    }
    r = client.put("/api/input", json={"catalog": catalog, "sql": "SELECT 1"})
    assert r.status_code == 400
    assert "非法" in str(r.json()["detail"])


def test_catalog_empty_columns_rejected(client):
    catalog = {"tables": [{"name": "t1", "columns": []}]}
    r = client.put("/api/input", json={"catalog": catalog, "sql": "SELECT 1"})
    assert r.status_code == 422


def test_catalog_change_failed_validation_keeps_state(client):
    put_analyze(client, SQL_PASS)
    catalog = {
        "tables": [
            {"name": "t1", "columns": [{"name": "a", "sensitive": False}]},
            {"name": "t1", "columns": [{"name": "a", "sensitive": False}]},
        ]
    }
    r = client.put("/api/input", json={"catalog": catalog, "sql": "X"})
    assert r.status_code == 400
    # 校验失败不改变已保存状态：旧版本结论仍有效
    assert client.get("/api/download").status_code == 200


# ---------- 页面 ----------

def test_index_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "涉敏血缘核查" in r.text
    assert "下载 SQL" in r.text


def test_static_assets_served(client):
    # 模块化前端资源必须可访问
    r = client.get("/static/app/app.js")
    assert r.status_code == 200
    assert "createGeneration" in r.text
    r = client.get("/static/app/core.js")
    assert r.status_code == 200
    assert "canonicalFingerprint" in r.text


# ---------- 内容指纹 ----------

def test_state_carries_fingerprint(client):
    r = put(client, SQL_PASS)
    st = client.get("/api/state").json()
    assert st["fingerprint"] == fingerprint(
        st["catalog"]["tables"], st["sql"]
    )
    assert r.json()["fingerprint"] == st["fingerprint"]


def test_legacy_db_migrates_and_backfills_fingerprint(tmp_path):
    """无 fingerprint 列的历史库打开时自动加列并按内容回填指纹。"""
    import sqlite3

    from app.concurrency import fingerprint as fp_of
    from app.db import Store

    p = tmp_path / "legacy.db"
    con = sqlite3.connect(p)
    con.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE state(id INTEGER PRIMARY KEY CHECK(id=1),
            sql TEXT NOT NULL DEFAULT '');
        CREATE TABLE catalog_table(id INTEGER PRIMARY KEY,
            name TEXT NOT NULL, pos INTEGER NOT NULL);
        CREATE TABLE catalog_column(id INTEGER PRIMARY KEY, table_id INTEGER NOT NULL,
            name TEXT NOT NULL, sensitive INTEGER NOT NULL DEFAULT 0, pos INTEGER NOT NULL);
        CREATE TABLE analysis(version INTEGER PRIMARY KEY, status TEXT NOT NULL,
            result_json TEXT NOT NULL, created_at TEXT NOT NULL);
        INSERT INTO meta(key,value) VALUES('version','1');
        INSERT INTO state(id,sql) VALUES(1,'SELECT 1');
        INSERT INTO catalog_table(id,name,pos) VALUES(1,'t',0);
        INSERT INTO catalog_column(id,table_id,name,sensitive,pos)
            VALUES(1,1,'a',0,0);
        """
    )
    con.commit()
    con.close()

    store = Store(str(p))
    st = store.get_state()
    assert len(st["fingerprint"]) == 64
    assert st["fingerprint"] == fp_of(st["catalog"]["tables"], st["sql"])
    assert st["version"] == 1
    # 再次打开（已是新结构）保持稳定
    assert Store(str(p)).get_state()["fingerprint"] == st["fingerprint"]


def test_fingerprint_changes_on_edit(client):
    r1 = put(client, SQL_PASS).json()
    r2 = put(client, SQL_BLOCKED).json()
    assert r1["fingerprint"] != r2["fingerprint"]


# ---------- 乐观并发：保存冲突 ----------

def test_save_with_stale_base_version_rejected(client):
    # 窗口 A 保存 v1
    a = put(client, SQL_PASS).json()
    assert a["version"] == 1
    # 窗口 B 保存推进到 v2
    b = put(client, SQL_BLOCKED).json()
    assert b["version"] == 2
    # 窗口 A 仍以 base_version=1 保存自己的草稿 → 409，服务端状态不变
    r = client.put(
        "/api/input",
        json={"catalog": CATALOG, "sql": "SELECT 1", "base_version": 1},
    )
    assert r.status_code == 409
    assert "其他窗口" in r.json()["detail"]
    # 服务端仍是 B 的 v2 内容，A 的草稿没有覆盖它
    st = client.get("/api/state").json()
    assert st["version"] == 2
    assert st["sql"] == SQL_BLOCKED


def test_save_with_current_base_version_accepted(client):
    put(client, SQL_PASS)
    r = client.put(
        "/api/input",
        json={"catalog": CATALOG, "sql": SQL_BLOCKED, "base_version": 1},
    )
    assert r.status_code == 200
    assert r.json()["version"] == 2


def test_concurrent_saves_only_one_wins(client):
    """12 个线程基于同一 base_version 并发保存：原子乐观锁保证仅 1 个成功，
    其余全部 409，版本只递增一次（杜绝检查-写入 TOCTOU）。"""
    import threading

    base = client.get("/api/state").json()["version"]
    outcomes = []

    def worker(i):
        r = client.put(
            "/api/input",
            json={
                "catalog": CATALOG,
                "sql": f"SELECT device_id AS c{i} FROM events",
                "base_version": base,
            },
        )
        outcomes.append(r.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count(200) == 1
    assert outcomes.count(409) == 11
    assert client.get("/api/state").json()["version"] == base + 1


def test_conflicting_save_preserves_analysis(client):
    # v1 已核查通过；冲突保存被拒后，v1 的结论与可下载性不受影响
    put_analyze(client, SQL_PASS)
    assert client.get("/api/download").status_code == 200
    put(client, SQL_BLOCKED)  # v2
    r = client.put(
        "/api/input",
        json={"catalog": CATALOG, "sql": "SELECT 1", "base_version": 1},
    )
    assert r.status_code == 409
    # 当前是 v2 且无结论 → 下载仍 409（没有凭空产生通过）
    assert client.get("/api/download").status_code == 409


# ---------- 核查绑定版本/指纹 ----------

def test_analyze_stale_version_rejected(client):
    put(client, SQL_PASS)  # v1
    put(client, SQL_BLOCKED)  # v2（他窗）
    # 窗口 A 要求核查 v1 → 拒绝，不能去核查当前的另一份查询
    r = client.post("/api/analyze", json={"version": 1})
    assert r.status_code == 409
    assert "其他窗口" in r.json()["detail"]
    # 被拒后 v2 不应产生任何结论
    assert client.get("/api/state").json()["analysis"] is None


def test_analyze_fingerprint_mismatch_rejected(client):
    st = put(client, SQL_PASS).json()["state"]
    r = client.post(
        "/api/analyze",
        json={"version": st["version"], "fingerprint": "deadbeef"},
    )
    assert r.status_code == 409
    assert "指纹" in r.json()["detail"]
    assert client.get("/api/state").json()["analysis"] is None


def test_analyze_correct_binding_succeeds(client):
    st = put(client, SQL_PASS).json()["state"]
    r = client.post(
        "/api/analyze",
        json={"version": st["version"], "fingerprint": st["fingerprint"]},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "pass"
    assert r.json()["fingerprint"] == st["fingerprint"]


# ---------- 下载绑定版本/指纹，绝不放行不匹配文件 ----------

def test_download_stale_version_rejected(client):
    st = put_analyze(client, SQL_PASS).json()
    put(client, SQL_BLOCKED)  # 他窗推进 v2
    r = client.get(
        "/api/download",
        params={"version": st["version"], "fingerprint": st["fingerprint"]},
    )
    assert r.status_code == 409
    assert "其他窗口" in r.json()["detail"]


def test_download_fingerprint_mismatch_rejected(client):
    st = put_analyze(client, SQL_PASS).json()
    # 版本号对，但指纹是另一份内容 → 拒绝
    r = client.get(
        "/api/download",
        params={"version": st["version"], "fingerprint": "deadbeef"},
    )
    assert r.status_code == 409
    assert "指纹" in r.json()["detail"]


def test_download_returns_bound_sql_with_fingerprint_header(client):
    st = put_analyze(client, SQL_PASS).json()
    r = client.get(
        "/api/download",
        params={"version": st["version"], "fingerprint": st["fingerprint"]},
    )
    assert r.status_code == 200
    assert r.text == SQL_PASS  # 下载的就是绑定的那一份，不是别的查询
    assert r.headers["x-content-fingerprint"] == st["fingerprint"]


def test_cross_window_cannot_download_other_query(client):
    """复现任务核心事故：A 保存 X 待核查，B 保存并核查 Y，
    A 的旧通过/下载请求绝不能拿到 Y，也不能把 Y 当成 X 放行。"""
    # A 保存 X（安全查询），尚未核查
    x = put(client, SQL_PASS).json()
    # B 改成 Y（涉敏），保存并核查 → blocked
    y = put(client, SQL_BLOCKED).json()
    rb = client.post(
        "/api/analyze",
        json={"version": y["version"], "fingerprint": y["fingerprint"]},
    )
    assert rb.json()["status"] == "blocked"
    # A 拿着 v1 的版本/指纹尝试核查与下载 → 双双 409
    assert (
        client.post(
            "/api/analyze",
            json={"version": x["version"], "fingerprint": x["fingerprint"]},
        ).status_code
        == 409
    )
    assert (
        client.get(
            "/api/download",
            params={"version": x["version"], "fingerprint": x["fingerprint"]},
        ).status_code
        == 409
    )
    # 当前服务端内容仍是 Y，A 无法通过任何途径下载到 Y 或 X
    assert client.get("/api/state").json()["sql"] == SQL_BLOCKED
    assert client.get("/api/download").status_code == 409


def test_refresh_then_resave_reanalyze_redownload_keeps_verdict(client):
    """刷新确认当前输入后可重新保存、核查、下载；涉密判定保持由分析器给出。"""
    # v1 安全通过
    put_analyze(client, SQL_PASS)
    # 他窗换成涉敏 Y（v2）
    put(client, SQL_BLOCKED)
    # 本窗口刷新到当前输入
    st = client.get("/api/state").json()
    assert st["version"] == 2 and st["analysis"] is None
    # 重新核查（涉密判定保持 blocked）
    r = client.post(
        "/api/analyze",
        json={"version": st["version"], "fingerprint": st["fingerprint"]},
    )
    assert r.json()["status"] == "blocked"
    assert client.get("/api/download").status_code == 409
    # 改回安全 SQL（v3）重新核查通过 → 可下载且内容正确
    back = put(client, SQL_PASS).json()
    r = client.post(
        "/api/analyze",
        json={"version": back["version"], "fingerprint": back["fingerprint"]},
    )
    assert r.json()["status"] == "pass"
    d = client.get(
        "/api/download",
        params={"version": back["version"], "fingerprint": back["fingerprint"]},
    )
    assert d.status_code == 200 and d.text == SQL_PASS
