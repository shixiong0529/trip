from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_homepage_uses_clear_background_and_note_footer():
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    assert "AI AGENT · 实时数据 · 三格式导出" not in index
    assert "AI AGENT · 实时数据" in index
    assert "告诉我目的地、天数和旅行偏好，AI 结合实时数据，帮你规划一份可以直接照着走的行程" in index
    assert "NOTA BENE" in index
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
    assert css.count("url('/static/bg.jpg')") == 1
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


def test_homepage_uses_logo_image_as_nav_icon_only():
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    assert 'class="homepage-logo"' not in index
    assert 'src="/static/logo-nav.png"' in index
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
