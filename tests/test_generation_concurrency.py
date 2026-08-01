"""报告生成并发与请求隔离测试，不触发真实外部 API。"""

import asyncio
import json
import re

import pytest

from starlette.requests import Request

import app as app_module


def _request_for(
    query: str,
    config: dict | None = None,
    mode: str | None = None,
) -> Request:
    payload = {"query": query}
    if config is not None:
        payload["config"] = config
    if mode is not None:
        payload["mode"] = mode
    body = json.dumps(payload, ensure_ascii=False).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/generate",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
    )


async def _consume_generation(
    query: str,
    config: dict | None = None,
    mode: str | None = None,
) -> str:
    response = await app_module.generate_guide(_request_for(query, config, mode))
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return "".join(chunks)


def _result_id(stream: str) -> str:
    match = re.search(r"event: result\ndata: (.+)\n\n", stream)
    assert match, stream
    result = json.loads(match.group(1))
    assert set(result) == {"guide_id"}  # HTML 通过下载接口加载，不在 SSE 中重复传输
    return result["guide_id"]


def test_concurrent_generations_are_limited_and_isolated(monkeypatch, isolated_db):
    counters = {"active": 0, "peak": 0}
    constructor_args = []

    class FakeOrchestrator:
        def __init__(self, base_url: str, api_key: str, model: str, mode: str):
            constructor_args.append((base_url, api_key, model, mode))

        async def generate(self, query: str):
            counters["active"] += 1
            counters["peak"] = max(counters["peak"], counters["active"])
            try:
                yield {"type": "progress", "data": f"开始 {query}"}
                await asyncio.sleep(0.02)
                yield {"type": "content", "data": f"# {query}\n\n仅属于 {query}"}
                await asyncio.sleep(0.02)
            finally:
                counters["active"] -= 1

    monkeypatch.setattr("orchestrator.TravelGuideOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(
        "generator.TravelGuideGenerator.to_html",
        lambda self, markdown, guide_id: f"<html><body>{guide_id}|{markdown}</body></html>",
    )
    monkeypatch.setattr(app_module, "_generation_gate", app_module._ConcurrencyGate(2))

    queries = [f"并发行程-{index}" for index in range(6)]
    streams = asyncio.run(_run_all(queries))

    assert counters["peak"] == 2
    assert any("已进入队列等待" in stream for stream in streams)

    guide_ids = [_result_id(stream) for stream in streams]
    assert len(set(guide_ids)) == len(queries)
    assert all(len(guide_id) == 32 for guide_id in guide_ids)

    for query, guide_id in zip(queries, guide_ids):
        guide = isolated_db.get_guide(guide_id)
        assert guide is not None
        assert guide["markdown"].startswith(f"# {query}\n\n仅属于 {query}")
        assert re.search(r"本报告生成耗时\d+分\d+秒", guide["markdown"])
        assert app_module._REPORT_DURATION_TOKEN not in guide["markdown"]
        assert f"{guide_id}|# {query}" in guide["html"]
        assert "本报告生成耗时" in guide["html"]
        assert app_module._REPORT_DURATION_TOKEN not in guide["html"]

    # 即使请求伪造模型地址/密钥，构造器也只能收到服务器配置。
    assert constructor_args
    assert all(
        args
        == (
            app_module.llm_config.base_url,
            app_module.llm_config.api_key,
            app_module.llm_config.model,
            "standard",
        )
        for args in constructor_args
    )


def test_generation_request_passes_professional_mode(monkeypatch, isolated_db):
    modes = []

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            modes.append(kwargs["mode"])

        async def generate(self, query: str):
            yield {"type": "content", "data": "# 专业版测试"}

    monkeypatch.setattr("orchestrator.TravelGuideOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(
        "generator.TravelGuideGenerator.to_html",
        lambda self, markdown, guide_id: f"<html>{markdown}</html>",
    )

    stream = asyncio.run(_consume_generation("专业版请求", mode="professional"))

    guide_id = _result_id(stream)
    assert modes == ["professional"]
    guide = isolated_db.get_guide(guide_id)
    assert guide is not None
    assert re.search(r"本报告生成耗时\d+分\d+秒", guide["markdown"])
    assert "本报告生成耗时" in guide["html"]


def test_standard_generation_persists_required_tail_sections(monkeypatch, isolated_db):
    class FakeOrchestrator:
        def __init__(self, **kwargs):
            assert kwargs["mode"] == "standard"

        async def generate(self, query: str):
            yield {
                "type": "content",
                "data": (
                    "# 成都2日游 · 为2人定制\n\n"
                    "## 分日行程\n"
                    "### Day 1 · 春熙路\n安排\n\n"
                    "### Day 2 · 熊猫基地\n安排\n\n"
                    "## 总预算\n人均约¥1000\n"
                ),
            }

    monkeypatch.setattr("orchestrator.TravelGuideOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(
        "generator.TravelGuideGenerator.to_html",
        lambda self, markdown, guide_id: f"<html>{markdown}</html>",
    )

    stream = asyncio.run(_consume_generation("成都2日游", mode="standard"))
    guide = isolated_db.get_guide(_result_id(stream))

    assert guide is not None
    assert "## 🎒 行前物品清单" in guide["markdown"]
    assert "身份证×2" in guide["markdown"]
    assert "## 🌳 行程知识图谱" in guide["markdown"]
    assert "└── Day 2 · 熊猫基地" in guide["markdown"]


@pytest.mark.parametrize(
    ("elapsed_seconds", "expected"),
    [
        (0, "本报告生成耗时0分0秒"),
        (65.2, "本报告生成耗时1分5秒"),
        (125.8, "本报告生成耗时2分6秒"),
    ],
)
def test_report_duration_uses_minutes_and_seconds(elapsed_seconds, expected):
    assert app_module._format_report_duration(elapsed_seconds) == expected


def test_report_duration_token_is_inserted_before_disclaimer():
    markdown = (
        "# 成都4日游\n\n正文\n\n"
        "> **免责声明**：请以官方信息为准。\n\n"
        "- 是否需要调整？\n"
    )

    prepared = app_module._append_report_duration_token(markdown)

    assert prepared.index(app_module._REPORT_DURATION_TOKEN) < prepared.index("免责声明")


def test_unexpected_html_failure_does_not_leak_internal_details(monkeypatch):
    class FakeOrchestrator:
        def __init__(self, **kwargs):
            pass

        async def generate(self, query: str):
            yield {"type": "content", "data": "# 测试报告"}

    monkeypatch.setattr("orchestrator.TravelGuideOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(
        "generator.TravelGuideGenerator.to_html",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("/private/database/path secret-upstream-body")
        ),
    )

    stream = asyncio.run(_consume_generation("触发 HTML 错误"))

    assert "event: error\ndata: 报告生成失败，请稍后重试" in stream
    assert "secret-upstream-body" not in stream
    assert "/private/database/path" not in stream


async def _run_all(queries: list[str]) -> list[str]:
    tasks = []
    for index, query in enumerate(queries):
        malicious_config = None
        if index == 0:
            malicious_config = {
                "base_url": "https://attacker.invalid/v1",
                "api_key": "attacker-key",
                "model": "attacker-model",
            }
        tasks.append(_consume_generation(query, malicious_config))
    return await asyncio.gather(*tasks)
