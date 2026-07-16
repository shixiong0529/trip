"""orchestrator LLM 参数接线测试(不触网)"""
import asyncio
from contextlib import suppress
import json
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


def test_default_temperature_prioritizes_structured_report_stability(monkeypatch):
    import config
    monkeypatch.delenv("LLM_TEMPERATURE", raising=False)

    assert config.LLMConfig().temperature == 0.2


def test_cancelled_generation_cancels_data_collection_and_route_tasks(monkeypatch):
    started = [asyncio.Event(), asyncio.Event()]
    cancelled = [asyncio.Event(), asyncio.Event()]

    async def fake_collect(*args, **kwargs):
        started[0].set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled[0].set()

    async def fake_plan(*args, **kwargs):
        started[1].set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled[1].set()

    monkeypatch.setattr("services.data_collector.collect_travel_data", fake_collect)
    monkeypatch.setattr("services.route_planner.plan_route", fake_plan)

    async def scenario():
        orchestrator = TravelGuideOrchestrator("https://api.example.com/v1", "k", "m")
        stream = orchestrator.generate("测试取消")
        first = await anext(stream)
        assert first["type"] == "progress"

        pending_event = asyncio.create_task(anext(stream))
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 1)
        pending_event.cancel()
        with suppress(asyncio.CancelledError):
            await pending_event
        await stream.aclose()
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in cancelled)), 1)

    asyncio.run(scenario())


def test_chat_json_uses_structured_non_streaming_request(monkeypatch):
    captured = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [{
                    "message": {"content": json.dumps({"origin": "长沙"})},
                    "finish_reason": "stop",
                }]
            }

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            captured.append(json)
            return FakeResponse()

    monkeypatch.setattr("orchestrator.httpx.AsyncClient", FakeClient)
    client = LLMClient("https://api.example.com/v1", "k", "fast")

    result = asyncio.run(client.chat_json([{"role": "user", "content": "extract"}]))

    assert json.loads(result) == {"origin": "长沙"}
    assert captured == [{
        "model": "fast",
        "messages": [{"role": "user", "content": "extract"}],
        "stream": False,
        "max_tokens": 16384,
        "temperature": 0.7,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
    }]


def test_chat_json_falls_back_when_gateway_rejects_response_format(monkeypatch):
    payloads = []

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code

        def json(self):
            return {
                "choices": [{
                    "message": {"content": '{"origin":"长沙"}'},
                    "finish_reason": "stop",
                }]
            }

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            payloads.append(json)
            return FakeResponse(400 if len(payloads) == 1 else 200)

    monkeypatch.setattr("orchestrator.httpx.AsyncClient", FakeClient)
    client = LLMClient("https://api.example.com/v1", "k", "fast")

    result = asyncio.run(client.chat_json([{"role": "user", "content": "extract"}]))

    assert json.loads(result) == {"origin": "长沙"}
    assert "response_format" in payloads[0]
    assert "response_format" not in payloads[1]
    assert all(payload["thinking"] == {"type": "disabled"} for payload in payloads)


def test_chat_json_does_not_treat_reasoning_as_final_json(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [{
                    "message": {
                        "content": "",
                        "reasoning_content": '{"origin":"错误的思考过程"}',
                    },
                    "finish_reason": "stop",
                }]
            }

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            return FakeResponse()

    monkeypatch.setattr("orchestrator.httpx.AsyncClient", FakeClient)
    client = LLMClient("https://api.example.com/v1", "k", "fast")

    with pytest.raises(LLMClientError, match="返回空内容"):
        asyncio.run(client.chat_json([{"role": "user", "content": "extract"}]))


def test_locked_day_plan_never_regenerates_entire_report(monkeypatch):
    """结构校验失败也必须继续做 HTML，不能再次调用主模型生成整篇报告。"""
    calls = 0

    class FakeReportLLM:
        last_finish_reason = None

        async def chat_stream(self, messages):
            nonlocal calls
            calls += 1
            self.last_finish_reason = "stop"
            # 骨架要求两天，故这份完整草稿会触发不可确定性修复的天数校验错误。
            yield "### Day 1 · 第一天 · 长沙 → 张家界 · 320km\n\n| 时段 | 安排 |\n|---|---|\n| 上午 | 出发 |\n"

    route = {"markdown": "locked route"}
    day_plan = {
        "overview": "长沙 → 张家界 → 长沙",
        "scaffold_md": "locked days",
        "days": [
            {"day": 1, "kind": "transfer", "from": "长沙", "to": "张家界", "km": 320},
            {"day": 2, "kind": "transfer", "from": "张家界", "to": "长沙", "km": 320},
        ],
    }

    async def fake_collect(*args, **kwargs):
        return {}

    async def fake_plan(*args, **kwargs):
        return route, "ok"

    async def fake_build(*args, **kwargs):
        return day_plan

    monkeypatch.setattr("services.data_collector.collect_travel_data", fake_collect)
    monkeypatch.setattr("services.route_planner.plan_route", fake_plan)
    monkeypatch.setattr("services.route_planner.build_day_plan", fake_build)

    orchestrator = TravelGuideOrchestrator("https://api.example.com/v1", "k", "m")
    orchestrator.llm = FakeReportLLM()

    async def consume():
        return [event async for event in orchestrator.generate("测试锁定骨架")]

    events = asyncio.run(consume())

    assert calls == 1
    assert any(
        event["type"] == "progress" and "保留完整草稿" in event["data"]
        for event in events
    )
    assert events[-1] == {"type": "progress", "data": "正在生成精美文档..."}
