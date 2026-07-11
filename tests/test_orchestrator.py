"""orchestrator LLM 参数接线测试(不触网)"""
import pytest

from orchestrator import LLMClient, TravelGuideOrchestrator, LLMClientError


def test_build_payload_uses_instance_limits():
    client = LLMClient("https://api.example.com/v1", "k", "m", max_tokens=16384, temperature=0.3)
    payload = client._build_payload([{"role": "user", "content": "hi"}])
    assert payload["max_tokens"] == 16384
    assert payload["temperature"] == 0.3
    assert payload["stream"] is True


def test_orchestrator_wires_config_max_tokens(monkeypatch):
    """回归:LLM_MAX_TOKENS 配置曾被硬编码 8192 覆盖,长行程输出被截断触发续写导致耗时翻倍"""
    import config
    monkeypatch.setattr(config.llm_config, "max_tokens", 12345)
    monkeypatch.setattr(config.llm_config, "temperature", 0.55)
    orch = TravelGuideOrchestrator("https://api.example.com/v1", "k", "m")
    payload = orch.llm._build_payload([{"role": "user", "content": "hi"}])
    assert payload["max_tokens"] == 12345
    assert payload["temperature"] == 0.55


def test_orchestrator_rejects_empty_key():
    with pytest.raises(LLMClientError):
        TravelGuideOrchestrator("https://api.example.com/v1", "", "m")
