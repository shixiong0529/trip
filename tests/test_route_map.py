import base64

from services import route_map


def test_extract_route_nodes_from_day_tables_keeps_order_and_dedupes():
    md = (
        "# 成都3日游\n\n"
        "### Day 1 · 慢城初见\n"
        "| 时段 | 安排 | 耗时 | 提示 |\n"
        "|------|------|------|------|\n"
        "| 09:00 | 游览**宽窄巷子**，随后去**人民公园**喝茶 | 3h | 慢走 |\n"
        "| 19:00 | 夜游**宽窄巷子** | 1h | 不重复标点 |\n\n"
        "### Day 2 · 城市漫步\n"
        "| 时段 | 安排 | 耗时 | 提示 |\n"
        "|------|------|------|------|\n"
        "| 09:00 | 前往**成都大熊猫繁育研究基地** | 3h | 早到 |\n"
        "| 15:00 | 逛**东郊记忆** | 2h | 拍照 |\n"
    )

    assert route_map.extract_route_nodes(md) == [
        "宽窄巷子",
        "人民公园",
        "成都大熊猫繁育研究基地",
        "东郊记忆",
    ]


def test_build_route_map_html_embeds_image_without_exposing_key(monkeypatch):
    monkeypatch.setenv("AMAP_WEB_SERVICE_KEY", "secret-key")
    monkeypatch.setattr(
        route_map,
        "_resolve_nodes",
        lambda key, destination, names: [
            {"name": "宽窄巷子", "location": "104.052,30.668"},
            {"name": "人民公园", "location": "104.058,30.660"},
        ],
    )
    monkeypatch.setattr(
        route_map,
        "_resolve_route_points",
        lambda key, resolved: ["104.052,30.668", "104.055,30.664", "104.058,30.660"],
    )
    monkeypatch.setattr(route_map, "_fetch_static_map", lambda key, resolved, points: b"fake-png")

    html = route_map.build_route_map_html(
        "# 成都3日游\n\n"
        "### Day 1\n"
        "| 时段 | 安排 |\n"
        "|------|------|\n"
        "| 09:00 | **宽窄巷子**到**人民公园** |\n"
    )

    assert "全程路线图" in html
    assert "data:image/png;base64," + base64.b64encode(b"fake-png").decode("ascii") in html
    assert "secret-key" not in html
    assert "A 宽窄巷子" in html
    assert "B 人民公园" in html
