"""
services/trip_store.py 覆盖测试：行程 CRUD、攻略缓存、携程问道查询缓存、
行程字段解析。每个测试都通过 conftest.py 的 isolated_db autouse fixture
拿到一个全新的临时 SQLite 库，不触碰真实的 travel_data.db。
"""

import time

import pytest


# ---------- 行程 CRUD ----------

def test_save_get_delete_trip(isolated_db):
    trip_store = isolated_db
    trip_id = trip_store.save_trip(
        destination="北京",
        markdown="# 🗺️ 北京3日游",
        days=3,
        travelers=2,
        budget=4000,
    )
    assert trip_id

    trip = trip_store.get_trip(trip_id)
    assert trip is not None
    assert trip["destination"] == "北京"
    assert trip["days"] == 3
    assert trip["travelers"] == 2
    assert trip["budget"] == 4000

    assert trip_store.delete_trip(trip_id) is True
    assert trip_store.get_trip(trip_id) is None
    # 删除不存在的行程返回 False
    assert trip_store.delete_trip(trip_id) is False


def test_list_trips_orders_by_updated_at_desc(isolated_db):
    trip_store = isolated_db
    id1 = trip_store.save_trip(destination="A")
    id2 = trip_store.save_trip(destination="B")
    trips = trip_store.list_trips()
    ids = [t["id"] for t in trips]
    assert id1 in ids and id2 in ids
    # 最近保存的排在前面（B 比 A 晚保存）
    assert ids.index(id2) < ids.index(id1)


# ---------- 攻略缓存 ----------

def test_save_and_get_guide(isolated_db):
    trip_store = isolated_db
    trip_store.save_guide("g1", "<html>hi</html>", "# hi")
    guide = trip_store.get_guide("g1")
    assert guide is not None
    assert guide["html"] == "<html>hi</html>"
    assert guide["markdown"] == "# hi"


def test_get_guide_missing_returns_none(isolated_db):
    trip_store = isolated_db
    assert trip_store.get_guide("does-not-exist") is None


def test_clean_expired_guides_removes_stale_entries(isolated_db):
    trip_store = isolated_db
    trip_store.save_guide("fresh", "<html>fresh</html>", "# fresh")
    trip_store.save_guide("stale", "<html>stale</html>", "# stale")

    # 手动把 stale 记录的 created_at 拨回 2 小时前，模拟过期
    conn = trip_store._get_db()
    conn.execute(
        "UPDATE guides SET created_at = ? WHERE id = ?",
        (time.time() - 7200, "stale"),
    )
    conn.commit()
    conn.close()

    trip_store.clean_expired_guides(ttl_seconds=3600)

    assert trip_store.get_guide("stale") is None
    assert trip_store.get_guide("fresh") is not None


# ---------- 携程问道查询缓存 ----------

def test_wendao_cache_write_and_read(isolated_db):
    trip_store = isolated_db
    trip_store.save_wendao_cache("hash1", "北京酒店推荐", "结果内容")
    cached = trip_store.get_cached_wendao("hash1")
    assert cached is not None
    assert cached["query"] == "北京酒店推荐"
    assert cached["result"] == "结果内容"


def test_wendao_cache_missing_returns_none(isolated_db):
    trip_store = isolated_db
    assert trip_store.get_cached_wendao("no-such-hash") is None


def test_clean_expired_wendao_cache(isolated_db):
    trip_store = isolated_db
    trip_store.save_wendao_cache("fresh-hash", "q1", "r1")
    trip_store.save_wendao_cache("stale-hash", "q2", "r2")

    conn = trip_store._get_db()
    conn.execute(
        "UPDATE wendao_cache SET created_at = ? WHERE query_hash = ?",
        (time.time() - 50000, "stale-hash"),
    )
    conn.commit()
    conn.close()

    trip_store.clean_expired_wendao_cache(ttl_seconds=43200)  # 12 小时

    assert trip_store.get_cached_wendao("stale-hash") is None
    assert trip_store.get_cached_wendao("fresh-hash") is not None


# ---------- 行程字段解析 ----------

@pytest.mark.parametrize(
    "raw_text,expected_dest,expected_days,expected_travelers,expected_budget",
    [
        ("北京3日游，2人，预算4000元", "北京", 3, 2, 4000.0),
        ("2026年7月9日出发去三亚玩5天，预算1.5万", "三亚", 5, None, 15000.0),
        ("预算2千", None, None, None, 2000.0),
    ],
)
def test_parse_trip_fields_cases(
    isolated_db, raw_text, expected_dest, expected_days, expected_travelers, expected_budget
):
    trip_store = isolated_db
    result = trip_store.parse_trip_fields(raw_text, "")
    if expected_dest is not None:
        assert result["destination"] == expected_dest
    assert result["days"] == expected_days
    assert result["travelers"] == expected_travelers
    assert result["budget"] == expected_budget


def test_parse_trip_fields_empty_input(isolated_db):
    trip_store = isolated_db
    result = trip_store.parse_trip_fields("", "")
    assert result == {"destination": None, "days": None, "travelers": None, "budget": None}
