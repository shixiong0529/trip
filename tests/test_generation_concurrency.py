"""报告生成并发与请求隔离测试，不触发真实外部 API。"""

import asyncio
import json
import re

from starlette.requests import Request

import app as app_module


def _request_for(query: str, config: dict | None = None) -> Request:
    payload = {"query": query}
    if config is not None:
        payload["config"] = config
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


async def _consume_generation(query: str, config: dict | None = None) -> str:
    response = await app_module.generate_guide(_request_for(query, config))
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
        def __init__(self, base_url: str, api_key: str, model: str):
            constructor_args.append((base_url, api_key, model))

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
        assert guide["markdown"] == f"# {query}\n\n仅属于 {query}"
        assert f"{guide_id}|# {query}" in guide["html"]

    # 即使请求伪造模型地址/密钥，构造器也只能收到服务器配置。
    assert constructor_args
    assert all(
        args
        == (
            app_module.llm_config.base_url,
            app_module.llm_config.api_key,
            app_module.llm_config.model,
        )
        for args in constructor_args
    )


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
