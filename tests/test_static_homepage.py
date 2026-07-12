from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_homepage_uses_clear_background_and_note_footer():
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    background_js = (ROOT / "static" / "background.js").read_text(encoding="utf-8")

    assert "AI AGENT · 实时数据 · 三格式导出" not in index
    assert "AI AGENT · 实时数据" in index
    assert "告诉我目的地、天数和旅行偏好，AI 结合实时数据，帮你规划一份可以直接照着走的行程" in index
    # 页脚免责说明（NOTA BENE 标语已在后续改版中移除）
    assert "本工具基于人工智能与互联网检索生成参考方案" in index
    assert "数据来源" in index
    assert "隐私政策" in index
    assert "服务条款" in index
    assert "联系我们" in index
    assert 'href="/info#data-source"' in index
    assert 'href="/info#privacy"' in index
    assert 'href="/info#terms"' in index
    assert 'href="/info#contact"' in index
    assert "bg-veil" not in index
    assert ".bg-veil" not in css
    assert "url('/static/pcbg6-desktop.jpg')" in css
    assert "url('/static/macbook-bg-desktop.jpg')" in css
    assert "url('/static/mbbg1-mobile.jpg')" in css
    assert "Math.random() < 0.5" in background_js
    assert "bg-pc-macbook" in background_js
    assert "backdrop-filter:" not in css
    assert "-webkit-backdrop-filter:" not in css
    bg_scene_rule = re.search(r"\.bg-scene\s*\{(?P<body>.*?)\}", css, re.S)
    assert bg_scene_rule is not None
    assert "position: fixed" in bg_scene_rule.group("body")
    assert "no-repeat fixed" in bg_scene_rule.group("body")
    footer_rule = re.search(r"\.app-footer\s*\{(?P<body>.*?)\}", css, re.S)
    assert footer_rule is not None
    assert "url(" not in footer_rule.group("body")


def test_homepage_script_tolerates_removed_status_bar():
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="status-bar"' not in index
    assert "if (els.statusDot && els.statusText)" in app_js
    assert "if (els.statusBar)" in app_js


def test_generation_frontend_isolates_cancelled_requests():
    app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "generationRunId" in app_js
    assert "if (state.mode === 'generating') return" in app_js
    assert "const body = { query: query }" in app_js
    assert "state.config" not in app_js


def test_homepage_uses_logo_image_as_nav_icon_only():
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    assert 'class="homepage-logo"' not in index
    assert 'src="/static/logo_good.png"' in index
    assert "&#x2708;" not in index
    logo_rule = re.search(r"\.logo-icon\s*\{(?P<body>.*?)\}", css, re.S)
    assert logo_rule is not None
    assert "width: auto" in logo_rule.group("body")
    assert "height: 1.8em" in logo_rule.group("body")


def test_info_page_contains_four_link_targets_and_contact_email():
    info = (ROOT / "static" / "info.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    assert 'id="data-source"' in info
    assert 'id="privacy"' in info
    assert 'id="terms"' in info
    assert 'id="contact"' in info
    assert "76106737@qq.com" in info
    assert 'href="/info#data-source"' in info
    assert 'class="info-page"' in info
    assert ".info-page" in css


def test_guide_template_allows_cjk_wrap_in_tables():
    """回归:模板表格曾用 word-break: keep-all,中文长明细不折行,
    把预算表的人均/合计列挤出可视区,看起来像没有数据。
    """
    guide = (ROOT / "templates" / "guide.html").read_text(encoding="utf-8")
    assert "word-break: keep-all" not in guide
    assert "overflow-wrap: break-word" in guide


def test_guide_template_keeps_pc_table_structure_on_mobile():
    guide = (ROOT / "templates" / "guide.html").read_text(encoding="utf-8")

    mobile = guide.split("@media (max-width: 640px)", 1)[1]
    assert "-webkit-overflow-scrolling: touch" in mobile
    assert ".table-wrapper table { min-width: 620px; font-size: .78rem; }" in mobile
    assert ".kv-table-wrapper table { min-width: 0; }" in mobile
    assert ".table-wrapper tbody { display: block" not in mobile
    assert "content: attr(data-label)" not in mobile
    assert ".budget-table-wrapper { overflow-x: visible; }" in mobile
    assert ".budget-table-wrapper table" in mobile
    assert "table-layout: fixed" in mobile


def test_guide_template_has_no_hero_tag_styles():
    guide = (ROOT / "templates" / "guide.html").read_text(encoding="utf-8")

    assert ".hero .tags" not in guide
    assert ".hero .tag" not in guide
    assert ".stats-grid" not in guide
    assert ".stat-card" not in guide


def test_mobile_result_title_and_guide_id_use_separate_rows():
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    mobile = css.split("@media (max-width: 640px)", 1)[1]

    assert "flex-direction: column" in mobile
    assert ".result-badge { white-space: nowrap; }" in mobile
    assert "overflow-wrap: anywhere" in mobile
    assert "mobile-result-header" in index
