"""
app.py 路由覆盖测试。用 FastAPI TestClient 直接调用 ASGI app，不起真实
HTTP 服务、不触网。isolated_db（conftest.py 中 autouse）保证行程/攻略
相关数据落在临时库。12306/机票相关外部调用一律 monkeypatch 掉，防止
测试环境中被误触发真实调用。
"""

import pytest
from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


@pytest.fixture(autouse=True)
def _no_real_network_calls(monkeypatch):
    """防御性 monkeypatch：即便未来代码变更导致提前调用，也不会触网"""
    monkeypatch.setattr(
        "services.train_service.query_tickets",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应在测试中调用真实 12306 查询")),
    )
    monkeypatch.setattr(
        "services.flight_search.search_flights",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应在测试中调用真实机票查询")),
    )


# ---------- 健康检查 ----------

def test_health_check():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert "llm_configured" in data
    assert "pdf_ready" in data


# ---------- 生成攻略 ----------

def test_generate_empty_query_returns_400():
    resp = client.post("/api/generate", json={"query": "  "})
    assert resp.status_code == 400


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        ("{not-json", "application/json"),
        ("[]", "application/json"),
        ('{"query": 123}', "application/json"),
    ],
)
def test_generate_rejects_invalid_request_bodies(body, content_type):
    resp = client.post(
        "/api/generate", content=body, headers={"Content-Type": content_type}
    )
    assert resp.status_code == 400


def test_generate_rejects_query_over_2000_characters():
    resp = client.post("/api/generate", json={"query": "旅" * 2001})
    assert resp.status_code == 400


# ---------- 下载 ----------

def test_download_missing_guide_returns_404():
    resp = client.get("/api/download/does-not-exist")
    assert resp.status_code == 404


def test_download_rejects_unknown_format(isolated_db):
    isolated_db.save_guide("format-test", "<html>ok</html>", "# ok")
    resp = client.get("/api/download/format-test?format=zip")
    assert resp.status_code == 400


def test_download_html_recalculates_cached_daily_budget(isolated_db):
    markdown = (
        "# 测试\n\n### Day 1 · 抵达\n"
        "💰 **本日预算：** 3人合计约¥2,458"
        "（高铁¥2,208 + 打车¥80 + 晚餐¥300 + 酒店¥250）\n"
    )
    isolated_db.save_guide("budget-fix", "<html>旧合计¥2,458</html>", markdown)

    response = client.get("/api/download/budget-fix?format=html")

    assert response.status_code == 200
    assert "3人合计约¥2,838" in response.text
    assert "旧合计¥2,458" not in response.text


def test_download_html_normalizes_cached_one_line_knowledge_graph(isolated_db):
    markdown = (
        "# 测试\n\n## 🌳 行程知识图谱\n```\n"
        "[北京] ├── Day 1 · 抵达 → 故宫 → ¥100/人 "
        "└── Day 2 · 返程 → 北京站 → ¥80/人\n```\n"
    )
    isolated_db.save_guide("graph-fix", "<html>旧单行图谱</html>", markdown)

    response = client.get("/api/download/graph-fix?format=html")

    assert response.status_code == 200
    assert "[北京]\n├── Day 1" in response.text
    assert "\n└── Day 2" in response.text
    assert "旧单行图谱" not in response.text


# ---------- 行程管理 ----------

def test_save_trip_auto_parses_fields():
    resp = client.post(
        "/api/trips",
        json={
            "destination": "北京3日游，2人，预算4000元",
            "markdown": "",
        },
    )
    assert resp.status_code == 200
    trip_id = resp.json()["trip_id"]

    detail = client.get(f"/api/trips/{trip_id}")
    assert detail.status_code == 200
    trip = detail.json()
    assert trip["days"] == 3
    assert trip["travelers"] == 2
    assert trip["budget"] == 4000


def test_save_trip_persists_corrected_daily_budget():
    markdown = (
        "# 成都3日游\n\n"
        "💰 **本日预算：** 3人合计约¥2,458"
        "（高铁¥2,208 + 打车¥80 + 晚餐¥300 + 酒店¥250）"
    )
    response = client.post(
        "/api/trips", json={"destination": "成都", "markdown": markdown}
    )

    saved = client.get(f"/api/trips/{response.json()['trip_id']}").json()

    assert "3人合计约¥2,838" in saved["markdown"]


def test_view_trip_200_and_404():
    saved = client.post(
        "/api/trips",
        json={"destination": "上海", "markdown": "# 🗺️ 上海2日游\n\n简单说明文字"},
    )
    trip_id = saved.json()["trip_id"]

    view = client.get(f"/api/trips/{trip_id}/view")
    assert view.status_code == 200
    assert "text/html" in view.headers["content-type"]

    missing = client.get("/api/trips/does-not-exist/view")
    assert missing.status_code == 404


def test_view_trip_preserves_compact_overview_and_mobile_table_labels():
    markdown = (
        "# 🗺️ 成都3日游\n\n"
        "3天2人，人均预算 ¥2,000，核心景点 6 个\n"
        "**线路纵览：** 上海 → 成都 → 上海。\n"
        "**重要提示：** 午后注意避暑。\n\n"
        "## 🚄 城际交通建议\n"
        "| 方向 | 推荐方式 | 提示 |\n"
        "|------|---------|------|\n"
        "| 去程 | 高铁 | 提前购票 |\n"
    )
    saved = client.post(
        "/api/trips", json={"destination": "成都", "markdown": markdown}
    )

    view = client.get(f"/api/trips/{saved.json()['trip_id']}/view")

    assert view.status_code == 200
    assert 'class="route-overview-card"' in view.text
    assert 'class="important-note-card"' in view.text
    assert "上海 → 成都 → 上海" in view.text
    assert "午后注意避暑" in view.text
    assert '<td data-label="提示">提前购票</td>' in view.text


def test_delete_trip_404_for_missing():
    resp = client.delete("/api/trips/does-not-exist")
    assert resp.status_code == 404


def test_delete_trip_success():
    saved = client.post("/api/trips", json={"destination": "广州", "markdown": ""})
    trip_id = saved.json()["trip_id"]
    resp = client.delete(f"/api/trips/{trip_id}")
    assert resp.status_code == 200
    assert client.get(f"/api/trips/{trip_id}").status_code == 404


# ---------- 12306 火车票 ----------

def test_train_tickets_missing_params_400():
    resp = client.get("/api/train/tickets")
    assert resp.status_code == 400


def test_train_tickets_invalid_date_400():
    resp = client.get(
        "/api/train/tickets",
        params={"from_station": "北京", "to_station": "上海", "date": "2026-13-99"},
    )
    assert resp.status_code == 400


# ---------- 国际机票 ----------

def test_flights_search_missing_params_400():
    resp = client.get("/api/flights/search")
    assert resp.status_code == 400
