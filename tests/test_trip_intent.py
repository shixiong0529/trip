import pytest

from services.trip_intent import classify_trip_intent, should_use_drive_planner


@pytest.mark.parametrize(
    "query",
    [
        "武汉出发去日本7天，喜欢樱花和美食",
        "上海去法国巴黎和意大利罗马10天",
        "北京出发泰国清迈旅行",
        "香港澳门5日游",
    ],
)
def test_outbound_trips_never_use_mainland_drive_planner(query):
    intent = classify_trip_intent(query)

    assert intent.is_outbound is True
    assert intent.use_drive_planner is False


def test_outbound_self_drive_still_skips_mainland_drive_planner():
    intent = classify_trip_intent("日本北海道租车自驾7天")

    assert intent.is_outbound is True
    assert intent.is_self_drive is True
    assert intent.use_drive_planner is False


def test_domestic_explicit_self_drive_uses_drive_planner():
    assert should_use_drive_planner("武汉出发自驾西藏15天") is True


def test_domestic_public_transport_trip_does_not_force_drive_planner():
    assert should_use_drive_planner("武汉坐高铁去成都玩4天") is False
