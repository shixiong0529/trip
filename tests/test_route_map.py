import base64

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
        "苍山洱海环湖精华段",
        "喜洲古镇",
        "沙溪古镇",
        "大理古城南门",
    ]


def test_drop_outliers_removes_cross_province_mismatch():
    """混入一个被错配到 1900km 外的点时应被剔除，其余保留"""
    resolved = [
        {"name": "翠湖公园", "location": "102.703,25.048"},      # 昆明
        {"name": "大理古城", "location": "100.164,25.694"},      # 大理
        {"name": "束河古镇", "location": "100.205,26.919"},      # 丽江
        {"name": "廊大街", "location": "116.634,38.700"},        # 河北廊坊（错配）
    ]
    kept = route_map._drop_outliers(resolved)
    assert [n["name"] for n in kept] == ["翠湖公园", "大理古城", "束河古镇"]


def test_short_city_names_prefer_geocode_before_poi(monkeypatch):
    calls = []

    class DummyClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(route_map.httpx, "Client", lambda timeout: DummyClient())

    def fake_geocode(client, key, name):
        calls.append(("geo", name))
        return {"name": f"{name}市", "location": "114.305,30.593"}

    def fake_search(client, key, city, name):
        calls.append(("poi", name))
        return {"name": "武汉鸭脖", "location": "100.100,25.600"}

    monkeypatch.setattr(route_map, "_geocode", fake_geocode)
    monkeypatch.setattr(route_map, "_search_poi", fake_search)

    assert route_map._resolve_nodes("k", "云南", ["武汉"]) == [
        {"name": "武汉市", "location": "114.305,30.593"}
    ]
    assert calls == [("geo", "武汉")]


def test_drop_outliers_keeps_legit_cross_city_chain():
    """昆明→大理→丽江这种真实跨城行程不应被误杀"""
    resolved = [
        {"name": "翠湖公园", "location": "102.703,25.048"},
        {"name": "大理古城", "location": "100.164,25.694"},
        {"name": "喜洲古镇", "location": "100.131,25.852"},
        {"name": "束河古镇", "location": "100.205,26.919"},
    ]
    kept = route_map._drop_outliers(resolved)
    assert len(kept) == 4


def test_drop_outliers_keeps_intra_city_with_suburb():
    """市区密集点 + 一个 60km 郊区景点（慕田峪长城）不应被误杀"""
    resolved = [
        {"name": "天安门广场", "location": "116.397,39.903"},
        {"name": "故宫博物院", "location": "116.397,39.917"},
        {"name": "天坛公园", "location": "116.410,39.881"},
        {"name": "慕田峪长城", "location": "116.565,40.431"},
    ]
    kept = route_map._drop_outliers(resolved)
    assert len(kept) == 4


def test_build_route_map_html_embeds_image_without_exposing_key(monkeypatch):
    monkeypatch.setenv("AMAP_WEB_SERVICE_KEY", "secret-key")
    monkeypatch.setattr(route_map, "_resolve_city", lambda key, destination: "成都")
    monkeypatch.setattr(
        route_map,
        "_resolve_nodes",
        lambda key, city, names: [
            {"name": "宽窄巷子", "location": "104.052,30.668"},
            {"name": "人民公园", "location": "104.058,30.660"},
        ],
    )
    monkeypatch.setattr(
        route_map,
        "_resolve_route_points",
        lambda key, resolved: ["104.052,30.668", "104.055,30.664", "104.058,30.660"],
    )
    monkeypatch.setattr(
        route_map, "_fetch_static_map",
        lambda key, resolved, points, labels=None: b"fake-png",
    )

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


def _mk_nodes(locs):
    return [{"name": f"点{i}", "location": loc} for i, loc in enumerate(locs)]


def test_detail_map_added_when_span_is_large(monkeypatch):
    """市区密集簇 + 远郊点（跨度>60km）时应追加细节图，且沿用原字母"""
    calls = []

    def fake_fetch(key, resolved, points, labels=None):
        calls.append({"n": len(resolved), "labels": labels})
        return b"png"

    monkeypatch.setattr(route_map, "_fetch_static_map", fake_fetch)
    # 5 个北京市区点 + 1 个慕田峪（61km 外）
    resolved = _mk_nodes([
        "116.397,39.903", "116.397,39.917", "116.410,39.881",
        "116.413,39.946", "116.323,39.909", "116.565,40.431",
    ])
    detail = route_map._build_detail_map("k", resolved, ["116.40,39.90", "116.56,40.43"])
    assert detail == b"png"
    assert calls[0]["n"] == 5                      # 只含密集簇 5 点
    assert calls[0]["labels"] == ["A", "B", "C", "D", "E"]  # 原字母


def test_detail_map_skipped_when_compact(monkeypatch):
    """纯市内紧凑行程（跨度<60km）不出细节图"""
    monkeypatch.setattr(
        route_map, "_fetch_static_map",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用")),
    )
    resolved = _mk_nodes([
        "116.397,39.903", "116.397,39.917", "116.410,39.881", "116.413,39.946",
    ])
    assert route_map._build_detail_map("k", resolved, []) == b""
