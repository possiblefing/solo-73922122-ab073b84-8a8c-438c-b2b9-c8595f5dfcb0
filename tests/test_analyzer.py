"""分析器单元测试：血缘、涉敏判定、影响路径、明确拒绝。"""
import pytest

from app.analyzer import (
    VIA_DIRECT,
    VIA_GROUP,
    VIA_HAVING,
    VIA_JOIN,
    VIA_UNION,
    VIA_WHERE,
    analyze,
)

CATALOG = [
    {
        "name": "events",
        "columns": [
            {"name": "event_id", "sensitive": False},
            {"name": "device_id", "sensitive": False},
            {"name": "ts", "sensitive": False},
            {"name": "event_type", "sensitive": False},
            {"name": "precise_lat", "sensitive": True},
            {"name": "precise_lng", "sensitive": True},
            {"name": "speed", "sensitive": False},
        ],
    },
    {
        "name": "devices",
        "columns": [
            {"name": "device_id", "sensitive": False},
            {"name": "model", "sensitive": False},
            {"name": "owner_id", "sensitive": False},
            {"name": "home_geohash", "sensitive": True},
        ],
    },
    {
        "name": "users",
        "columns": [
            {"name": "owner_id", "sensitive": False},
            {"name": "name", "sensitive": False},
            {"name": "phone", "sensitive": True},
        ],
    },
]


def run(sql, catalog=None):
    return analyze(sql, catalog if catalog is not None else CATALOG)


def col(result, name):
    for c in result["columns"]:
        if c["name"] == name:
            return c
    raise AssertionError(f"输出列 {name} 不存在：{[c['name'] for c in result['columns']]}")


def src_keys(c):
    return {(s["table"], s["column"]) for s in c["sources"]}


def src(c, table, column):
    for s in c["sources"]:
        if (s["table"], s["column"]) == (table, column):
            return s
    raise AssertionError(f"来源 {table}.{column} 不存在于 {c['sources']}")


# ---------- 基本通过场景 ----------

def test_simple_pass():
    r = run("SELECT device_id, COUNT(*) AS cnt FROM events GROUP BY device_id")
    assert r["status"] == "pass"
    c = col(r, "cnt")
    assert c["is_aggregate"] is True
    # COUNT(*) 无直接来源，但 GROUP BY device_id 是分组影响
    assert src(c, "events", "device_id")["via"] == [VIA_GROUP]


def test_select_without_from():
    r = run("SELECT 1 AS one, 'x' AS label")
    assert r["status"] == "pass"
    assert col(r, "one")["sources"] == []


def test_alias_and_expression():
    r = run("SELECT device_id AS did, speed * 2 AS dbl FROM events")
    assert r["status"] == "pass"
    assert col(r, "did")["sources"][0]["via"] == [VIA_DIRECT]
    assert src(col(r, "dbl"), "events", "speed")


def test_case_insensitive():
    r = run("SELECT DEVICE_ID FROM EVENTS")
    assert r["status"] == "pass"
    assert src(col(r, "DEVICE_ID"), "events", "device_id")


# ---------- 涉敏判定 ----------

def test_direct_sensitive_blocked():
    r = run("SELECT device_id, precise_lat FROM events")
    assert r["status"] == "blocked"
    assert col(r, "device_id")["sensitive"] is False
    c = col(r, "precise_lat")
    assert c["sensitive"] is True
    assert src(c, "events", "precise_lat")["via"] == [VIA_DIRECT]


def test_where_sensitive_blocked():
    r = run("SELECT device_id FROM events WHERE precise_lat > 30")
    assert r["status"] == "blocked"
    c = col(r, "device_id")
    assert src(c, "events", "precise_lat")["via"] == [VIA_WHERE]


def test_join_on_sensitive_blocked():
    r = run(
        "SELECT e.device_id FROM events e "
        "JOIN devices d ON d.home_geohash = e.event_type"
    )
    assert r["status"] == "blocked"
    assert src(col(r, "device_id"), "devices", "home_geohash")["via"] == [VIA_JOIN]


def test_group_by_sensitive_blocked():
    r = run("SELECT precise_lat, COUNT(*) AS cnt FROM events GROUP BY precise_lat")
    assert r["status"] == "blocked"
    c = col(r, "cnt")
    assert src(c, "events", "precise_lat")["via"] == [VIA_GROUP]


def test_having_sensitive_blocked():
    r = run(
        "SELECT device_id, MAX(speed) AS m FROM events "
        "GROUP BY device_id HAVING MAX(precise_lng) < 120"
    )
    assert r["status"] == "blocked"
    assert src(col(r, "m"), "events", "precise_lng")["via"] == [VIA_HAVING]


def test_aggregate_is_not_desensitization():
    r = run("SELECT AVG(precise_lat) AS a, SUM(precise_lng) AS s FROM events")
    assert r["status"] == "blocked"
    assert col(r, "a")["is_aggregate"] and col(r, "a")["sensitive"]
    assert col(r, "s")["is_aggregate"] and col(r, "s")["sensitive"]


def test_count_over_sensitive_where_blocked():
    # 典型泄露：COUNT + 涉敏 WHERE，虽只输出计数仍拦截
    r = run("SELECT COUNT(*) AS c FROM events WHERE precise_lat = 30.1")
    assert r["status"] == "blocked"
    assert col(r, "c")["sensitive"] is True


# ---------- 星号 ----------

def test_star_expands_and_marks_sensitive():
    r = run("SELECT * FROM devices")
    assert r["status"] == "blocked"
    names = [c["name"] for c in r["columns"]]
    assert names == ["device_id", "model", "owner_id", "home_geohash"]
    assert col(r, "home_geohash")["sensitive"] is True


def test_qualified_star():
    r = run("SELECT e.* FROM events e JOIN devices d ON e.device_id = d.device_id")
    assert r["status"] == "blocked"  # events 含 precise_lat/lng
    names = {c["name"] for c in r["columns"]}
    assert "precise_lat" in names and "model" not in names


def test_star_join_expands_both():
    r = run("SELECT * FROM devices d JOIN users u ON d.owner_id = u.owner_id")
    assert r["status"] == "blocked"  # home_geohash + phone
    names = [c["name"] for c in r["columns"]]
    assert names.count("owner_id") == 2  # 两侧同名列都展开


def test_unknown_qualified_star():
    r = run("SELECT x.* FROM events e")
    assert r["status"] == "error"
    assert "未知表或别名" in r["errors"][0]


# ---------- CTE ----------

def test_cte_passthrough():
    r = run(
        "WITH c AS (SELECT device_id, ts FROM events) "
        "SELECT device_id FROM c"
    )
    assert r["status"] == "pass"
    s = src(col(r, "device_id"), "events", "device_id")
    assert VIA_DIRECT in s["via"] and "经由 CTE c" in s["via"]


def test_cte_carries_where_influence():
    r = run(
        "WITH c AS (SELECT device_id FROM events WHERE precise_lat > 1) "
        "SELECT * FROM c"
    )
    assert r["status"] == "blocked"
    s = src(col(r, "device_id"), "events", "precise_lat")
    assert VIA_WHERE in s["via"] and "经由 CTE c" in s["via"]


def test_cte_chain_and_rename():
    r = run(
        "WITH a(x, y) AS (SELECT device_id, speed FROM events), "
        "b AS (SELECT x, y * 2 AS y2 FROM a) "
        "SELECT x, y2 FROM b"
    )
    assert r["status"] == "pass"
    c = col(r, "y2")
    assert src(c, "events", "speed")
    assert "经由 CTE a" in src(c, "events", "speed")["via"]
    assert "经由 CTE b" in src(c, "events", "speed")["via"]


def test_cte_rename_count_mismatch():
    r = run("WITH c(a, b) AS (SELECT device_id FROM events) SELECT * FROM c")
    assert r["status"] == "error"
    assert "列别名数量" in r["errors"][0]


def test_cte_shadows_base_table():
    r = run(
        "WITH devices AS (SELECT device_id FROM events) "
        "SELECT device_id FROM devices"
    )
    assert r["status"] == "pass"  # 引用的是 CTE 而非目录表 devices


def test_recursive_cte_rejected():
    r = run("WITH RECURSIVE c AS (SELECT 1 AS x) SELECT x FROM c")
    assert r["status"] == "error"
    assert "递归" in r["errors"][0]


def test_cte_forward_reference_rejected():
    r = run(
        "WITH a AS (SELECT x FROM b), b AS (SELECT device_id AS x FROM events) "
        "SELECT x FROM a"
    )
    assert r["status"] == "error"
    assert "未知表" in r["errors"][0]


# ---------- UNION ALL ----------

def test_union_all_merges_lineage():
    r = run(
        "SELECT device_id AS id FROM events "
        "UNION ALL SELECT owner_id AS id FROM users"
    )
    assert r["status"] == "pass"
    c = col(r, "id")
    assert src_keys(c) == {("events", "device_id"), ("users", "owner_id")}
    assert VIA_UNION in src(c, "events", "device_id")["via"]


def test_union_all_sensitive_branch_blocked():
    r = run("SELECT device_id AS id FROM events UNION ALL SELECT phone AS id FROM users")
    assert r["status"] == "blocked"
    assert col(r, "id")["sensitive"] is True


def test_union_distinct_rejected():
    r = run("SELECT device_id FROM events UNION SELECT device_id FROM events")
    assert r["status"] == "error"
    assert "UNION ALL" in r["errors"][0]


def test_intersect_rejected():
    r = run("SELECT device_id FROM events INTERSECT SELECT device_id FROM events")
    assert r["status"] == "error"


def test_union_column_count_mismatch():
    r = run("SELECT device_id, ts FROM events UNION ALL SELECT device_id FROM events")
    assert r["status"] == "error"
    assert "列数不一致" in r["errors"][0]


def test_union_with_cte_and_parens():
    r = run(
        "WITH c AS (SELECT device_id FROM events) "
        "(SELECT device_id FROM c) UNION ALL (SELECT owner_id FROM users)"
    )
    assert r["status"] == "pass"


# ---------- 明确拒绝 ----------

def test_unknown_column():
    r = run("SELECT nope FROM events")
    assert r["status"] == "error"
    assert "未知列" in r["errors"][0]


def test_unknown_qualified_column():
    r = run("SELECT e.nope FROM events e")
    assert r["status"] == "error"
    assert "未知列" in r["errors"][0]


def test_unknown_table():
    r = run("SELECT a FROM missing_table")
    assert r["status"] == "error"
    assert "未知表" in r["errors"][0]


def test_ambiguous_unqualified():
    r = run("SELECT device_id FROM events e JOIN devices d ON e.event_id = d.owner_id")
    assert r["status"] == "error"
    assert "歧义" in r["errors"][0]


def test_ambiguous_self_join():
    r = run("SELECT device_id FROM events a JOIN events b ON a.event_id = b.event_id")
    assert r["status"] == "error"
    assert "歧义" in r["errors"][0]


def test_qualified_self_join_ok():
    r = run(
        "SELECT a.device_id FROM events a JOIN events b ON a.event_id = b.event_id"
    )
    assert r["status"] == "pass"


def test_derived_table_rejected():
    r = run("SELECT x.device_id FROM (SELECT device_id FROM events) x")
    assert r["status"] == "error"


def test_scalar_subquery_rejected():
    r = run("SELECT (SELECT MAX(speed) FROM events) AS m FROM devices")
    assert r["status"] == "error"


def test_in_subquery_rejected():
    r = run("SELECT device_id FROM events WHERE device_id IN (SELECT device_id FROM devices)")
    assert r["status"] == "error"


def test_window_function_rejected():
    r = run("SELECT ROW_NUMBER() OVER (ORDER BY ts) AS rn FROM events")
    assert r["status"] == "error"
    assert "窗口" in r["errors"][0]


def test_natural_join_rejected():
    r = run("SELECT * FROM events NATURAL JOIN devices")
    assert r["status"] == "error"
    assert "NATURAL" in r["errors"][0]


def test_join_using_rejected():
    r = run("SELECT * FROM events JOIN devices USING (device_id)")
    assert r["status"] == "error"
    assert "USING" in r["errors"][0]


def test_multi_statement_rejected():
    r = run("SELECT 1; SELECT 2")
    assert r["status"] == "error"
    assert "单条" in r["errors"][0]


def test_non_select_rejected():
    r = run("DELETE FROM events")
    assert r["status"] == "error"
    assert "SELECT" in r["errors"][0]


def test_empty_sql_rejected():
    r = run("   ")
    assert r["status"] == "error"


def test_parse_error():
    r = run("SELECT FROM WHERE")
    assert r["status"] == "error"
    assert "解析失败" in r["errors"][0]


def test_db_qualified_table_rejected():
    r = run("SELECT device_id FROM main.events")
    assert r["status"] == "error"


# ---------- GROUP BY 细节 ----------

def test_group_by_ordinal():
    r = run("SELECT device_id, COUNT(*) AS c FROM events GROUP BY 1")
    assert r["status"] == "pass"
    assert src(col(r, "c"), "events", "device_id")["via"] == [VIA_GROUP]


def test_group_by_ordinal_out_of_range():
    r = run("SELECT device_id FROM events GROUP BY 7")
    assert r["status"] == "error"
    assert "越界" in r["errors"][0]


def test_group_by_alias():
    r = run("SELECT speed * 2 AS sp FROM events GROUP BY sp")
    assert r["status"] == "pass"
    assert VIA_GROUP in src(col(r, "sp"), "events", "speed")["via"]


def test_group_by_expression():
    r = run(
        "SELECT strftime('%H', ts) AS hr, COUNT(*) AS c FROM events "
        "GROUP BY strftime('%H', ts)"
    )
    assert r["status"] == "pass"
    assert VIA_GROUP in src(col(r, "c"), "events", "ts")["via"]


def test_having_alias_reference():
    r = run(
        "SELECT device_id, COUNT(*) AS c FROM events "
        "GROUP BY device_id HAVING c > 5"
    )
    assert r["status"] == "pass"
    # HAVING 引用输出别名 c → 其来源（device_id 分组）也进入影响路径
    assert VIA_HAVING in src(col(r, "c"), "events", "device_id")["via"]


# ---------- 组合场景 ----------

def test_full_demo_query_pass():
    sql = """
    WITH hourly AS (
      SELECT device_id, strftime('%Y-%m-%d %H:00', ts) AS hour, COUNT(*) AS cnt
      FROM events
      WHERE event_type = 'upload'
      GROUP BY device_id, hour
    )
    SELECT h.device_id, d.model, h.hour, h.cnt
    FROM hourly h
    JOIN devices d ON d.device_id = h.device_id
    ORDER BY h.device_id
    """
    r = run(sql)
    assert r["status"] == "pass"
    c = col(r, "cnt")
    # cnt 的影响：event_type（WHERE，经 CTE）、device_id（JOIN + GROUP）
    keys = src_keys(c)
    assert ("events", "event_type") in keys
    assert ("events", "device_id") in keys
    assert ("devices", "device_id") in keys


def test_distinct_allowed():
    r = run("SELECT DISTINCT device_id FROM events")
    assert r["status"] == "pass"


def test_order_limit_allowed():
    r = run("SELECT device_id FROM events ORDER BY ts DESC LIMIT 10")
    assert r["status"] == "pass"


def test_case_function_expression():
    r = run(
        "SELECT device_id, CASE WHEN speed > 100 THEN 'fast' ELSE 'slow' END AS lvl "
        "FROM events"
    )
    assert r["status"] == "pass"
    assert src(col(r, "lvl"), "events", "speed")


def test_sensitive_through_two_cte_layers():
    r = run(
        "WITH a AS (SELECT device_id FROM events WHERE precise_lat > 1), "
        "b AS (SELECT device_id FROM a GROUP BY device_id) "
        "SELECT COUNT(*) AS c FROM b"
    )
    assert r["status"] == "blocked"
    s = src(col(r, "c"), "events", "precise_lat")
    assert "经由 CTE a" in s["via"] and "经由 CTE b" in s["via"]
