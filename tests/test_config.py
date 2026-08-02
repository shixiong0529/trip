"""环境配置解析测试。"""

import pytest

from config import AppConfig


_PRO_REVIEW_ENV = (
    "PRO_REVIEW_MODE",
    "PRO_REVIEW_TIMEOUT",
    "PRO_REVIEW_MAX_TOKENS",
    "PRO_REVIEW_TOTAL_TIMEOUT",
    "PRO_REWRITE_MAX_ATTEMPTS",
)


def _clear_pro_review_env(monkeypatch):
    for name in _PRO_REVIEW_ENV:
        monkeypatch.delenv(name, raising=False)


def test_professional_review_defaults_to_safe_repair_mode(monkeypatch):
    _clear_pro_review_env(monkeypatch)

    config = AppConfig()

    assert config.pro_review_mode == "repair"
    assert config.pro_review_timeout == 60
    assert config.pro_review_max_tokens == 2500
    assert config.pro_review_total_timeout == 420
    assert config.pro_rewrite_max_attempts == 1


@pytest.mark.parametrize("mode", ["off", "shadow", "audit", "repair"])
def test_professional_review_accepts_all_rollout_modes(monkeypatch, mode):
    monkeypatch.setenv("PRO_REVIEW_MODE", f" {mode.upper()} ")

    assert AppConfig().pro_review_mode == mode


def test_professional_review_invalid_mode_falls_back_to_repair(monkeypatch):
    monkeypatch.setenv("PRO_REVIEW_MODE", "unexpected")

    assert AppConfig().pro_review_mode == "repair"


def test_professional_review_limits_prevent_runaway_rewrites(monkeypatch):
    monkeypatch.setenv("PRO_REVIEW_TIMEOUT", "5")
    monkeypatch.setenv("PRO_REVIEW_MAX_TOKENS", "100")
    monkeypatch.setenv("PRO_REVIEW_TOTAL_TIMEOUT", "8")
    monkeypatch.setenv("PRO_REWRITE_MAX_ATTEMPTS", "9")

    config = AppConfig()

    assert config.pro_review_timeout == 10
    assert config.pro_review_max_tokens == 256
    assert config.pro_review_total_timeout == 10
    assert config.pro_rewrite_max_attempts == 1


def test_professional_review_can_disable_rewrite_attempt(monkeypatch):
    monkeypatch.setenv("PRO_REWRITE_MAX_ATTEMPTS", "-2")

    assert AppConfig().pro_rewrite_max_attempts == 0
