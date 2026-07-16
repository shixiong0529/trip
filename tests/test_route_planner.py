"""路线骨架标题校验与确定性修复测试。"""

import asyncio

from services.route_planner import (
    _match_day,
    _normalize_query_for_cache,
    _dayplan_cache,
    check_day_sequence_prefix,
    build_day_plan,
    repair_day_headings,
    validate_day_sequence,
)


def test_merged_stop_title_is_not_rejected():
    day = {
        "day": 7,
        "kind": "stay",
        "at": "溆浦县山背梯田·花瑶古寨",
    }
    title = "梯田守望者 · 溆浦县山背梯田·花瑶古寨 深度游/休整 · 0km"

    assert _match_day(title, day) == (True, "")


def test_merged_stop_in_transfer_title_survives_middle_dot_parsing():
    day = {
        "day": 8,
        "kind": "transfer",
        "from": "溆浦县思蒙湿地公园",
        "to": "溆浦县山背梯田·花瑶",
    }
    title = (
        "云端梯田与花瑶风情 · "
        "溆浦县思蒙湿地公园 → 溆浦县山背梯田·花瑶 · 约65km · 约1.5h"
    )

    assert _match_day(title, day) == (True, "")


def test_county_prefix_may_be_omitted_but_wrong_place_is_rejected():
    day = {"day": 3, "kind": "stay", "at": "龙山县八面山"}

    assert _match_day("八面山深度游 · 0km", day) == (True, "")
    assert _match_day("张家界国家森林公园深度游 · 0km", day)[0] is False


def test_heading_drift_is_repaired_without_rewriting_body():
    markdown = (
        "### Day 9 · 八面山慢游 · 八面山休整\n\n"
        "| 时段 | 安排 |\n|---|---|\n| 08:00 | 看日出 |\n"
    )
    day_plan = {
        "days": [{"day": 3, "kind": "stay", "at": "龙山县八面山"}]
    }

    repaired, count = repair_day_headings(markdown, day_plan)

    assert count == 1
    assert "### Day 3 · 八面山慢游 · 龙山县八面山 深度游/休整 · 0km" in repaired
    assert "| 08:00 | 看日出 |" in repaired
    assert validate_day_sequence(repaired, day_plan) == (True, "")


def test_streaming_prefix_does_not_reject_repairable_title_text():
    markdown = (
        "### Day 9 · 简称 · 八面山休整\n"
        "| 时段 | 安排 |\n|---|---|\n| 08:00 | 看日出 |\n"
    )
    day_plan = {
        "days": [{"day": 3, "kind": "stay", "at": "龙山县八面山"}]
    }

    assert check_day_sequence_prefix(markdown, day_plan) == (True, "")


def test_stop_extraction_prefers_non_streaming_json_method():
    from services.route_planner import _extract_stops_inner

    class FakeLLM:
        async def chat_json(self, messages):
            return (
                '{"origin":"长沙","origin_inferred":false,'
                '"stops":["龙山县八面山","古丈县坐龙峡"],'
                '"user_fixed_order":false,"round_trip":true,"days":7}'
            )

        async def chat_stream(self, messages):
            raise AssertionError("短 JSON 任务不应走流式接口")
            yield

    result = asyncio.run(_extract_stops_inner("测试", FakeLLM()))

    assert result["origin"] == "长沙"
    assert result["days"] == 7


def test_route_cache_query_normalization_ignores_spacing_and_common_punctuation():
    assert _normalize_query_for_cache(" 长沙 → 龙山，7 天！ ") == _normalize_query_for_cache(
        "长沙→龙山 7天"
    )


def test_day_plan_is_restored_from_sqlite_after_memory_cache_clear(isolated_db):
    route = {
        "seq_names": ["长沙", "龙山县八面山", "长沙"],
        "legs": [
            {"from": "长沙", "to": "龙山县八面山", "km": 400, "hours": 5, "measured": True},
            {"from": "龙山县八面山", "to": "长沙", "km": 400, "hours": 5, "measured": True},
        ],
        "round_trip": True,
        "days_budget": 2,
        "markdown": "locked-route",
    }

    first = asyncio.run(build_day_plan("长沙到龙山，两天", route, object()))
    _dayplan_cache.clear()
    second = asyncio.run(build_day_plan("长沙到龙山，两天", route, object()))

    assert second == first


def test_fast_extraction_failure_retries_with_pro_model(monkeypatch, isolated_db):
    from services import route_planner

    class Model:
        def __init__(self, name):
            self.model = name

    fast = Model("deepseek-v4-flash")
    pro = Model("deepseek-v4-pro")
    calls = []

    async def fake_extract(query, llm, timeout_seconds):
        calls.append((llm.model, timeout_seconds))
        if llm is fast:
            return None
        return {
            "origin": "长沙",
            "stops": ["龙山县八面山", "古丈县坐龙峡"],
            "round_trip": True,
        }

    planned_route = {
        "seq_names": ["长沙", "龙山县八面山", "古丈县坐龙峡", "长沙"],
        "legs": [],
        "markdown": "route",
    }

    async def fake_geo(query, extracted, notify):
        return planned_route

    route_planner._route_cache.clear()
    monkeypatch.setenv("AMAP_WEB_SERVICE_KEY", "test-key")
    monkeypatch.setattr(route_planner, "_extract_stops", fake_extract)
    monkeypatch.setattr(route_planner, "_plan_geo", fake_geo)

    route, status = asyncio.run(
        route_planner.plan_route("独一无二的回退测试需求", fast, fallback_llm=pro)
    )

    assert status == "ok"
    assert route == planned_route
    assert calls == [
        ("deepseek-v4-flash", route_planner._FAST_EXTRACT_TIMEOUT),
        ("deepseek-v4-pro", route_planner._FALLBACK_EXTRACT_TIMEOUT),
    ]
