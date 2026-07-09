"""
services/data_collector.py 覆盖测试：目的地/出发地提取的正则，以及
collect_travel_data 的并行采集聚合逻辑。携程问道客户端与 12306 查询
全部通过 monkeypatch 替换为假实现，不发起任何真实网络请求。
"""

import asyncio

import pytest

from services.data_collector import (
    _extract_destination,
    _extract_origin,
    collect_travel_data,
)


# ---------- _extract_destination ----------

@pytest.mark.parametrize(
    "query,expected",
    [
        ("北京3日游", "北京"),
        ("云南亲子游8天", "云南"),
        ("日本赏樱7天", "日本"),
        ("成都美食之旅4天", "成都"),
        ("武汉出发自驾西藏15天", "西藏"),
        ("我想去西藏玩18天", "西藏"),
    ],
)
def test_extract_destination(query, expected):
    assert _extract_destination(query) == expected


# ---------- _extract_origin ----------

@pytest.mark.parametrize(
    "query,expected",
    [
        ("武汉出发自驾西藏15天", "武汉"),
        ("从上海到北京", "上海"),
    ],
)
def test_extract_origin(query, expected):
    assert _extract_origin(query) == expected


def test_extract_origin_no_match_returns_none():
    assert _extract_origin("随便写点内容测试") is None


# ---------- collect_travel_data ----------

class _FakeCtripClient:
    """假携程问道客户端：按查询数量返回等量的假 Markdown 结果，不触网"""

    async def query_many(self, questions: list[str]) -> list[str]:
        return [f"[假数据] {q}" for q in questions]


def test_collect_travel_data_keys(monkeypatch):
    monkeypatch.setattr(
        "services.data_collector.get_ctrip_client", lambda: _FakeCtripClient()
    )
    # 无法提取出发地时不会触发 12306 查询
    data = asyncio.run(collect_travel_data("我想去西藏玩18天"))
    assert set(data.keys()) == {"transport", "hotels", "attractions", "tips", "train", "amap"}
    assert data["transport"]
    assert data["hotels"]
    assert data["attractions"]
    assert data["tips"]
    assert data["train"] == ""
    assert data["amap"] == ""


def test_collect_travel_data_injects_amap_when_destination_present(monkeypatch):
    monkeypatch.setattr(
        "services.data_collector.get_ctrip_client", lambda: _FakeCtripClient()
    )

    async def fake_query_amap_reference(dest, org):
        return f"高德位置数据: {org or '未知'} -> {dest}"

    monkeypatch.setattr("services.data_collector._query_amap_reference", fake_query_amap_reference)

    data = asyncio.run(collect_travel_data("上海出发成都3日游"))
    assert data["amap"] == "高德位置数据: 上海 -> 成都"


def test_collect_travel_data_injects_train_when_org_and_dest_present(monkeypatch):
    monkeypatch.setattr(
        "services.data_collector.get_ctrip_client", lambda: _FakeCtripClient()
    )

    def fake_query_tickets(from_station, to_station, date):
        return {"tickets": ["G1234"]}

    def fake_format_ticket_result(result):
        return "G1234 二等座 有票"

    monkeypatch.setattr("services.train_service.query_tickets", fake_query_tickets)
    monkeypatch.setattr("services.train_service.format_ticket_result", fake_format_ticket_result)

    data = asyncio.run(collect_travel_data("武汉出发自驾西藏15天"))
    assert "G1234 二等座 有票" in data["train"]
    assert "12306余票参考" in data["train"]


def test_collect_travel_data_train_empty_when_query_fails(monkeypatch):
    monkeypatch.setattr(
        "services.data_collector.get_ctrip_client", lambda: _FakeCtripClient()
    )

    def fake_query_tickets_error(from_station, to_station, date):
        return {"error": "查询失败"}

    monkeypatch.setattr("services.train_service.query_tickets", fake_query_tickets_error)

    data = asyncio.run(collect_travel_data("武汉出发自驾西藏15天"))
    assert data["train"] == ""
