"""路线骨架标题校验与确定性修复测试。"""

import asyncio

from services.route_planner import (
    _apply_ordered_chains,
    _collapse_near_stops,
    _expand_unsafe_legs,
    _insert_gateway_excursions,
    _match_day,
    _normalize_query_for_cache,
    _poi_core_name,
    _poi_match_score,
    _plan_geo,
    _required_drive_days,
    _sample_path,
    _dayplan_cache,
    check_day_sequence_prefix,
    build_day_plan,
    repair_day_headings,
    validate_day_sequence,
)
from services.route_rules import canonicalize_corridor_stops, resolve_route_rules


def test_unsafe_4000km_leg_requires_multiple_real_days():
    leg = {"km": 4035, "hours": 43.2}

    assert _required_drive_days(leg) == 6


def test_unsafe_leg_expands_to_concrete_overnight_stops_with_safe_caps():
    legs = [{
        "from": "西安",
        "to": "塔什库尔干",
        "km": 4035,
        "hours": 43.2,
        "measured": True,
        "is_return": False,
        "split_points": ["兰州", "酒泉", "哈密", "库尔勒", "喀什"],
    }]

    expanded = _expand_unsafe_legs(legs)

    assert len(expanded) == 6
    assert [(leg["from"], leg["to"]) for leg in expanded] == [
        ("西安", "兰州"),
        ("兰州", "酒泉"),
        ("酒泉", "哈密"),
        ("哈密", "库尔勒"),
        ("库尔勒", "喀什"),
        ("喀什", "塔什库尔干"),
    ]
    assert all(leg["km"] <= 800 for leg in expanded)
    assert all(leg["hours"] <= 10 for leg in expanded)
    assert all(leg["is_safety_stop"] for leg in expanded[:-1])
    assert expanded[-1]["is_safety_stop"] is False


def test_path_sampling_returns_one_ordered_point_per_overnight_stop():
    points = [(0.0, 0.0), (2.0, 0.0), (5.0, 0.0)]

    sampled = _sample_path(points, 4)

    assert len(sampled) == 4
    assert [point[0] for point in sampled] == sorted(point[0] for point in sampled)


def test_day_plan_uses_split_days_instead_of_locking_4000km_into_day_one(isolated_db):
    route = {
        "seq_names": ["西安", "塔什库尔干", "喀什"],
        "legs": [
            {
                "from": "西安", "to": "塔什库尔干", "km": 4035,
                "hours": 43.2, "measured": True,
                "split_points": ["兰州", "酒泉", "哈密", "库尔勒", "喀什"],
            },
            {
                "from": "塔什库尔干", "to": "喀什", "km": 300,
                "hours": 5, "measured": True,
            },
        ],
        "round_trip": False,
        "days_budget": 7,
        "markdown": "unique-safe-long-leg-route",
    }

    plan = asyncio.run(build_day_plan("西安到新疆7天安全自驾", route, object()))

    assert plan is not None
    assert len(plan["days"]) == 7
    assert plan["days"][0]["from"] == "西安"
    assert plan["days"][0]["to"] == "兰州"
    assert plan["days"][5]["to"] == "塔什库尔干"
    assert "西安 → 兰州 → 酒泉 → 哈密 → 库尔勒 → 喀什 → 塔什库尔干" in plan["overview"]
    assert "必须在 兰州 住宿" in plan["scaffold_md"]
    assert "约 4035km" not in plan["scaffold_md"]


def test_eighteen_day_loop_spends_real_days_on_both_extreme_legs(isolated_db):
    middle_names = ["喀什", "库车", "巴音布鲁克", "伊宁", "乌鲁木齐", "克拉玛依", "喀纳斯", "可可托海"]
    names = ["塔什库尔干", *middle_names]
    legs = [{
        "from": "西安", "to": "塔什库尔干", "km": 4035,
        "hours": 43.2, "measured": True,
        "split_points": ["中卫", "酒泉", "哈密", "焉耆", "柯坪"],
    }]
    for start, end in zip(names, middle_names):
        legs.append({
            "from": start, "to": end, "km": 300,
            "hours": 5, "measured": True,
        })
    legs.append({
        "from": "可可托海", "to": "西安", "km": 2788,
        "hours": 31.5, "measured": True,
        "split_points": ["哈密", "酒泉", "兰州"],
    })
    route = {
        "seq_names": ["西安", *names, "西安"],
        "legs": legs,
        "round_trip": True,
        "days_budget": 18,
        "markdown": "exact-html-regression-safe-long-legs",
    }

    plan = asyncio.run(build_day_plan("西安出发新疆18天环线自驾", route, object()))

    assert plan is not None
    assert len(plan["days"]) == 18
    transfers = [day for day in plan["days"] if day["kind"] == "transfer"]
    assert all(day["km"] <= 800 for day in transfers)
    assert all(day["hours"] <= 10 for day in transfers)
    assert transfers[0]["to"] == "中卫"
    assert transfers[5]["to"] == "塔什库尔干"
    assert transfers[-4]["from"] == "可可托海"
    assert transfers[-1]["to"] == "西安"


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
        route_planner.plan_route("独一无二的自驾回退测试需求", fast, fallback_llm=pro)
    )

    assert status == "ok"
    assert route == planned_route
    assert calls == [
        ("deepseek-v4-flash", route_planner._FAST_EXTRACT_TIMEOUT),
        ("deepseek-v4-pro", route_planner._FALLBACK_EXTRACT_TIMEOUT),
    ]


def test_route_planner_skips_outbound_before_calling_llm(monkeypatch):
    from services import route_planner

    async def should_not_extract(*args, **kwargs):
        raise AssertionError("出境请求不应进入自驾途经点抽取")

    monkeypatch.setenv("AMAP_WEB_SERVICE_KEY", "test-key")
    monkeypatch.setattr(route_planner, "_extract_stops", should_not_extract)

    route, status = asyncio.run(
        route_planner.plan_route("日本北海道租车自驾7天", object())
    )

    assert route is None
    assert status == "not_applicable"


def test_poi_candidate_scoring_rejects_wrong_business_and_tianchi_road():
    wrong_bayan = {"name": "携程度假农庄(那拉提河谷草原店)"}
    right_bayan = {"name": "巴音布鲁克大草原"}
    wrong_tianchi = {"name": "天池路"}
    right_tianchi = {"name": "天山天池风景区"}

    assert _poi_core_name("新源县巴音布鲁克草原") == "巴音布鲁克草原"
    assert _poi_match_score("新源县巴音布鲁克草原", right_bayan) > _poi_match_score(
        "新源县巴音布鲁克草原", wrong_bayan
    )
    assert _poi_match_score("乌鲁木齐市天山天池", right_tianchi) > _poi_match_score(
        "乌鲁木齐市天山天池", wrong_tianchi
    )


def test_distinct_nearby_pois_are_not_collapsed_by_distance_alone():
    names = ["乌鲁木齐", "新源县那拉提旅游风景区", "和静县巴音布鲁克草原"]
    distance = [
        [0, 400, 410],
        [400, 0, 8],
        [410, 8, 0],
    ]

    seq, merged = _collapse_near_stops(
        [0, 1, 2, 0], names, distance, poi_ids=[None, "nalati", "bayan"]
    )

    assert seq == [0, 1, 2, 0]
    assert merged[1] != merged[2]


def test_duku_corridor_is_an_ordered_block_and_tashkurgan_uses_kashgar_gateway():
    names = [
        "乌鲁木齐", "库车市天山神秘大峡谷", "喀什市喀什古城",
        "和静县巴音布鲁克草原", "塔什库尔干县帕米尔旅游区",
        "新源县那拉提旅游风景区", "赛里木湖",
    ]
    context = resolve_route_rules(
        "8月新疆自然景观自驾14天", names,
    )
    # 故意给一条错误顺序，通用约束器须先修成连续走廊。
    distance = [[0 if i == j else 100 for j in range(len(names))] for i in range(len(names))]
    order = _apply_ordered_chains(
        distance,
        [6, 1, 2, 3, 5],
        context["ordered_chains"],
        True,
    )
    order = _insert_gateway_excursions(order, context["gateway_excursions"])

    nalati, bayan, kuqa = 5, 3, 1
    start = order.index(nalati)
    assert order[start:start + 3] == [nalati, bayan, kuqa]
    kashgar = order.index(2)
    assert order[kashgar:kashgar + 3] == [2, 4, 2]
    assert context["leg_overrides"][(bayan, kuqa)]["corridor"] == "G217独库公路南段"


def test_corridor_stop_canonicalization_fixes_wrong_admin_prefixes():
    stops = canonicalize_corridor_stops(
        "新疆自驾14天",
        ["伊宁市那拉提草原", "新源县巴音布鲁克草原", "库车大峡谷"],
    )

    assert stops == [
        "新源县那拉提旅游风景区",
        "和静县巴音布鲁克草原",
        "库车市天山神秘大峡谷",
    ]


def _bayan_required_route(days_budget):
    requirement = "必须进入巴音布鲁克景区，禁止只路过、远眺或只作短暂休息"
    return {
        "seq_names": ["乌鲁木齐", "新源县那拉提旅游风景区", "和静县巴音布鲁克草原", "乌鲁木齐"],
        "legs": [
            {"from": "乌鲁木齐", "to": "新源县那拉提旅游风景区", "km": 450, "hours": 6, "measured": True},
            {
                "from": "新源县那拉提旅游风景区", "to": "和静县巴音布鲁克草原",
                "km": 225, "hours": 5.5, "measured": True,
                "visit_requirement": requirement, "no_merge": True,
            },
            {"from": "和静县巴音布鲁克草原", "to": "乌鲁木齐", "km": 500, "hours": 7, "measured": True},
        ],
        "round_trip": True,
        "days_budget": days_budget,
        "min_stay_days": {"和静县巴音布鲁克草原": 1},
        "markdown": f"bayan-required-{days_budget}",
    }


def test_bayanbulak_minimum_stay_cannot_be_reduced_to_pass_through(isolated_db):
    plan = asyncio.run(build_day_plan("新疆自驾5天", _bayan_required_route(5), object()))

    assert plan is not None and not plan.get("infeasible")
    assert any(
        day.get("kind") == "stay" and day.get("at") == "和静县巴音布鲁克草原"
        for day in plan["days"]
    )
    assert "禁止只路过、远眺" in plan["scaffold_md"]


def test_required_visit_over_day_budget_is_rejected_instead_of_dropped(isolated_db):
    plan = asyncio.run(build_day_plan("新疆自驾3天", _bayan_required_route(3), object()))

    assert plan["infeasible"] is True
    assert "不会把必游景点降级成路过远眺" in plan["reason"]


def test_geo_pipeline_applies_corridor_gateway_and_visit_metadata(monkeypatch):
    from services import route_planner

    async def fake_geocode(client, key, names):
        return [
            {
                "coord": (float(index), float(index)),
                "poi_name": name,
                "poi_id": f"poi-{index}",
            }
            for index, name in enumerate(names)
        ]

    async def fake_matrix(client, key, coords):
        n = len(coords)
        distance = [[0.0 if i == j else 100.0 for j in range(n)] for i in range(n)]
        duration = [[0.0 if i == j else 2.0 for j in range(n)] for i in range(n)]
        measured = [[True for _ in range(n)] for _ in range(n)]
        # 模拟高德当前时段把巴音→库车算成绕行；规则仍须用景观走廊参考覆盖。
        distance[2][3], duration[2][3] = 751.0, 10.1
        return distance, duration, measured

    monkeypatch.setenv("AMAP_WEB_SERVICE_KEY", "test-key")
    monkeypatch.setattr(route_planner, "_geocode_all", fake_geocode)
    monkeypatch.setattr(route_planner, "_driving_matrix", fake_matrix)
    route = asyncio.run(_plan_geo(
        "8月新疆自然景观自驾14天",
        {
            "origin": "乌鲁木齐",
            "stops": [
                "新源县那拉提草原", "和静县巴音布鲁克草原", "库车大峡谷",
                "喀什古城", "塔什库尔干帕米尔旅游区",
            ],
            "round_trip": True,
            "days": 14,
        },
    ))

    sequence = route["seq_names"]
    nalati = sequence.index("新源县那拉提旅游风景区")
    assert sequence[nalati:nalati + 3] == [
        "新源县那拉提旅游风景区",
        "和静县巴音布鲁克草原",
        "库车市天山神秘大峡谷",
    ]
    kashgar = sequence.index("喀什古城")
    assert sequence[kashgar:kashgar + 3] == [
        "喀什古城", "塔什库尔干帕米尔旅游区", "喀什古城",
    ]
    corridor = next(leg for leg in route["legs"] if leg.get("corridor"))
    assert corridor["km"] == 280.0
    assert corridor["via"] == ["G217独库公路南段", "库车大小龙池"]
    assert route["min_stay_days"]["和静县巴音布鲁克草原"] == 1
