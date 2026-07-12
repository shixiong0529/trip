import pytest

from services import route_map


@pytest.fixture(autouse=True)
def _clear_route_map_cache():
    """每个用例前后清空模块内缓存，避免跨用例串扰"""
    route_map._route_map_cache.clear()
    yield
    route_map._route_map_cache.clear()


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


def test_extract_route_nodes_skips_activity_and_hint_text():
    """活动/提示类加粗文本不是地名，不应被当作路线节点（回归：曾把"生态廊道骑行"配到河北）"""
    md = (
        "# 云南8日游\n\n"
        "### Day 3 · 大理\n"
        "| 时段 | 安排 |\n"
        "|------|------|\n"
        "| 09:00 | **生态廊道骑行**，下午**大理古城**漫步 |\n"
        "| 14:00 | **仅预约故宫无法进入广场**（提示） |\n"
        "| 16:00 | **自由活动**后**双廊古镇** |\n"
    )
    assert route_map.extract_route_nodes(md) == ["大理古城", "双廊古镇"]


def test_extract_route_nodes_from_route_overview_and_quoted_places():
    md = (
        "# 云南亲子自然风光 · 4日精华行程\n\n"
        "**路线总览**：武汉 ✈️ → 昆明（中转）🚄→ 大理（深度游）"
        "→ 苍山洱海环湖精华段 → 喜洲古镇 → 沙溪古镇 → 大理 🚄→ 昆明 ✈️ → 武汉，当地包车约160km轻松游。\n\n"
        "### Day 1 · 抵达大理\n"
        "| 时段 | 安排 | 耗时 | 提示 |\n"
        "|------|------|------|------|\n"
        "| 15:30 | 🏛️ 大理古城亲子漫步：人民路 → 洋人街 → 五华楼 | 2h | 轻松 |\n"
        "| 18:00 | 🌇 「大理古城南门」城楼看苍山日落 | 1h | 免费 |\n"
        "| 19:30 | 🍲 晚餐 · 「段公子·天龙八部主题餐厅」 | 1h | 不应进入路线图 |\n"
    )

    assert route_map.extract_route_nodes(md) == [
        "武汉",
        "昆明",
        "大理",
        "喜洲古镇",
        "沙溪古镇",
        "大理古城南门",
    ]


def test_extract_route_items_only_uses_route_overview_and_skips_road_nodes():
    md = (
        "# 青甘大环线12日\n\n"
        "**路线总览：** 西安 → G30连霍高速 → 兰州 → 青海湖 → G6京藏高速 → 茶卡镇 → 青海湖 → 西安。\n\n"
        "### Day 1 · 出发\n"
        "| 时段 | 安排 |\n"
        "|------|------|\n"
        "| 09:00 | **兵马俑**出发，抵达**兰州老街** |\n"
    )

    assert route_map.extract_route_items(md) == [
        {"name": "西安", "day": "第1站"},
        {"name": "兰州", "day": "第2站"},
        {"name": "青海湖", "day": "第3站"},
        {"name": "茶卡镇", "day": "第4站"},
        {"name": "青海湖", "day": "第5站"},
        {"name": "西安", "day": "第6站"},
    ]


def test_extract_route_items_accepts_line_overview_alias():
    md = "**线路纵览：** 上海 → 成都 → 上海。"

    assert route_map.extract_route_items(md) == [
        {"name": "上海", "day": "第1站"},
        {"name": "成都", "day": "第2站"},
        {"name": "上海", "day": "第3站"},
    ]


def test_normalize_route_overview_removes_road_names_and_distance_copy():
    content = "**路线总览**：武汉 → G42沪蓉高速 → 成都 → G318川藏南线 → 拉萨，全程约2,300km。"

    assert route_map.normalize_route_overview(content) == "**路线总览：** 武汉 → 成都 → 拉萨"


def test_build_route_map_html_builds_schematic_from_route_overview_without_static_map(monkeypatch):
    monkeypatch.setenv("AMAP_WEB_SERVICE_KEY", "secret-key")
    monkeypatch.setattr(
        route_map,
        "_resolve_item_coordinates",
        lambda items: [(104.06, 30.67), (103.57, 30.51)],
    )

    html = route_map.build_route_map_html(
        "# 成都3日游\n\n"
        "**路线总览：** 宽窄巷子 → 人民公园\n"
    )

    assert "全程路线图" in html
    assert '<svg class="route-map-svg"' in html
    assert "data:image/png;base64" not in html
    assert "基于高德地图真实底图" not in html
    assert "secret-key" not in html
    assert "1</strong> 宽窄巷子" in html
    assert "2</strong> 人民公园" in html


def test_geographic_points_follow_longitude_and_latitude_direction():
    points = route_map._geographic_points(
        [(114.30, 30.59), (104.06, 30.67), (91.13, 29.65)], 1200, 560
    )

    assert points is not None
    assert points[0][0] > points[1][0] > points[2][0]  # 武汉 -> 成都 -> 拉萨，向西
    assert points[1][1] < points[2][1]  # 成都在拉萨以北，SVG y 值更小
