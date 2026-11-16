"""API 集成测试：保存-核查-下载状态机、版本失效、双窗口交错。"""
import pytest
from fastapi.testclient import TestClient

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
