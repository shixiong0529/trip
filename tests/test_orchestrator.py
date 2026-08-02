"""orchestrator LLM 参数接线测试(不触网)"""
import asyncio
from contextlib import suppress
import json
import time
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


def test_standard_mode_caps_report_tokens_and_professional_keeps_config(monkeypatch):
    import config
    monkeypatch.setattr(config.llm_config, "max_tokens", 16384)

    standard = TravelGuideOrchestrator(
        "https://api.example.com/v1", "k", "m", mode="standard"
    )
    professional = TravelGuideOrchestrator(
        "https://api.example.com/v1", "k", "m", mode="professional"
    )

    assert standard.llm.max_tokens == 10000
    assert professional.llm.max_tokens == 16384
    assert standard.llm.first_token_timeout == 45
    assert standard.llm.stream_timeout == 75
    assert professional.llm.first_token_timeout == 75
    assert professional.llm.stream_timeout == 180


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
        stream = orchestrator.generate("测试自驾取消")
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


def test_chat_stream_stops_when_first_token_deadline_expires(monkeypatch):
    class SlowStreamResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_lines(self):
            await asyncio.sleep(1)
            yield 'data: {"choices":[{"delta":{"content":"太迟"}}]}'

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            return SlowStreamResponse()

    monkeypatch.setattr("orchestrator.httpx.AsyncClient", FakeClient)
    client = LLMClient(
        "https://api.example.com/v1",
        "k",
        "slow",
        first_token_timeout=0.02,
        stream_timeout=0.1,
    )

    async def consume():
        return [chunk async for chunk in client.chat_stream([{"role": "user", "content": "报告"}])]

    started = time.monotonic()
    chunks = asyncio.run(consume())

    assert chunks == []
    assert client.last_finish_reason == "timeout"
    assert time.monotonic() - started < 0.3


def test_chat_stream_disables_thinking_mode(monkeypatch):
    captured = []

    class DoneResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_lines(self):
            yield "data: [DONE]"

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, json, headers):
            captured.append(json)
            return DoneResponse()

    monkeypatch.setattr("orchestrator.httpx.AsyncClient", FakeClient)
    client = LLMClient("https://api.example.com/v1", "k", "report")

    async def consume():
        return [chunk async for chunk in client.chat_stream([{"role": "user", "content": "报告"}])]

    assert asyncio.run(consume()) == []
    assert captured[0]["thinking"] == {"type": "disabled"}


def test_standard_locked_day_plan_never_regenerates_entire_report(monkeypatch):
    """标准版仍保持单次正文生成，不进入专业版审核重写流程。"""
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

    orchestrator = TravelGuideOrchestrator(
        "https://api.example.com/v1", "k", "m", mode="standard"
    )
    orchestrator.llm = FakeReportLLM()

    async def consume():
        return [event async for event in orchestrator.generate("测试自驾锁定骨架")]

    events = asyncio.run(consume())

    assert calls == 1
    assert any(
        event["type"] == "progress" and "保留完整草稿" in event["data"]
        for event in events
    )
    assert events[-1] == {"type": "progress", "data": "正在生成精美文档..."}


def test_infeasible_route_is_stopped_before_report_generation(monkeypatch):
    class ReportMustNotRun:
        async def chat_stream(self, messages):
            raise AssertionError("不可行路线不能进入正文模型")
            yield

    async def fake_collect(*args, **kwargs):
        return {}

    async def fake_plan(*args, **kwargs):
        return {"markdown": "unsafe route"}, "ok"

    async def fake_build(*args, **kwargs):
        return {
            "infeasible": True,
            "reason": "14天无法容纳门户往返与必游停留",
        }

    monkeypatch.setattr("services.data_collector.collect_travel_data", fake_collect)
    monkeypatch.setattr("services.route_planner.plan_route", fake_plan)
    monkeypatch.setattr("services.route_planner.build_day_plan", fake_build)

    orchestrator = TravelGuideOrchestrator("https://api.example.com/v1", "k", "m")
    orchestrator.llm = ReportMustNotRun()

    async def consume():
        return [event async for event in orchestrator.generate("新疆自驾14天")]

    events = asyncio.run(consume())

    assert events[-1]["type"] == "error"
    assert "路线可行性检查未通过" in events[-1]["data"]


def test_empty_report_stream_retries_once_without_recollecting_data():
    class EmptyThenSuccessLLM:
        last_finish_reason = None

        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages):
            self.calls += 1
            if self.calls == 1:
                self.last_finish_reason = "length"
                return
            self.last_finish_reason = "stop"
            yield "# 重试成功\n\n## 总预算\n预算\n\n> 免责声明：请核实"

    orchestrator = TravelGuideOrchestrator("https://api.example.com/v1", "k", "m", mode="standard")
    orchestrator.llm = EmptyThenSuccessLLM()
    sink = {}

    async def consume():
        return [
            event
            async for event in orchestrator._stream_events(
                [{"role": "user", "content": "生成报告"}], sink
            )
        ]

    events = asyncio.run(consume())

    assert orchestrator.llm.calls == 2
    assert sink["content"] == "# 重试成功\n\n## 总预算\n预算\n\n> 免责声明：请核实"
    assert any("正在自动重试" in event["data"] for event in events)
    assert any("# 重试成功" in event.get("data", "") for event in events)


def test_two_empty_report_streams_raise_clear_error():
    class AlwaysEmptyLLM:
        last_finish_reason = "length"

        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages):
            self.calls += 1
            self.last_finish_reason = "length"
            if False:
                yield ""

    orchestrator = TravelGuideOrchestrator("https://api.example.com/v1", "k", "m", mode="standard")
    orchestrator.llm = AlwaysEmptyLLM()

    async def consume():
        return [
            event
            async for event in orchestrator._stream_events(
                [{"role": "user", "content": "生成报告"}], {}
            )
        ]

    with pytest.raises(LLMClientError, match="连续两次未返回报告正文"):
        asyncio.run(consume())
    assert orchestrator.llm.calls == 2


def test_truncated_standard_report_continues_and_requires_all_days():
    class PartialThenCompleteLLM:
        last_finish_reason = None

        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages):
            self.calls += 1
            if self.calls == 1:
                self.last_finish_reason = "length"
                yield "# 成都2日游\n\n## 分日行程\n### Day 1 · 市区漫游\n安排\n"
                return
            self.last_finish_reason = "stop"
            yield (
                "\n### Day 2 · 熊猫基地\n安排\n\n"
                "## 总预算\n预算\n\n> 免责声明：请以官方信息为准。"
            )

    orchestrator = TravelGuideOrchestrator("https://api.example.com/v1", "k", "m", mode="standard")
    orchestrator.llm = PartialThenCompleteLLM()
    sink = {}

    async def consume():
        return [
            event
            async for event in orchestrator._stream_events(
                [{"role": "user", "content": "成都旅行"}],
                sink,
                validate_completion=True,
            )
        ]

    events = asyncio.run(consume())

    assert orchestrator.llm.calls == 2
    assert "### Day 1" in sink["content"]
    assert "### Day 2" in sink["content"]
    assert "## 总预算" in sink["content"]
    assert any("自动补全" in event.get("data", "") for event in events)


def test_expected_days_can_be_recovered_from_report_heading():
    content = "# 成都 12 日行程 · 为2人定制\n\n## 分日行程\n### Day 1 · 出发"

    assert TravelGuideOrchestrator._expected_days_from_report(content) == 12
    assert TravelGuideOrchestrator._report_completion_issue(content, 12).startswith("缺少 Day 2")
    assert TravelGuideOrchestrator._report_completion_issue("# 成都游\n\n## 总预算\n预算") == "缺少分日行程"


def test_incomplete_continuation_is_rejected_instead_of_saved():
    class StillIncompleteLLM:
        last_finish_reason = None

        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages):
            self.calls += 1
            self.last_finish_reason = "length" if self.calls == 1 else "stop"
            yield "# 成都2日游\n\n## 分日行程\n### Day 1 · 市区漫游\n安排"

    orchestrator = TravelGuideOrchestrator("https://api.example.com/v1", "k", "m", mode="standard")
    orchestrator.llm = StillIncompleteLLM()

    async def consume():
        return [
            event
            async for event in orchestrator._stream_events(
                [{"role": "user", "content": "成都2日游"}], {}, expected_days=2
            )
        ]

    with pytest.raises(LLMClientError, match="仍不完整：缺少 Day 2"):
        asyncio.run(consume())
    assert orchestrator.llm.calls == 2
