"""专业版隐藏初稿、审核与条件修复流水线测试。

这些测试直接消费 orchestrator 事件，使用 fake LLM，既不访问网络，也不
触碰真实数据库。重点约束浏览器可观察到的事件边界：初稿只能留在服务端，
审核完成后仅通过 ``final_content`` 交给 app.py 持久化。
"""

import asyncio
import json
import time

from orchestrator import LLMClientError, TravelGuideOrchestrator


def _complete_report(marker: str) -> str:
    return f"""# 成都 1 日专业旅行攻略

**路线总览：** 成都

## 🌤️ 天气与穿搭

出发前复核天气，携带雨具。

## 🚄 城际交通建议

市内优先乘坐地铁与步行。

## 🏨 住宿推荐

如需住宿，选择市中心交通便利区域。

## 📅 分日行程

### Day 1 · 成都城市漫游

| 时段 | 安排 | 耗时 | 提示 |
|---|---|---|---|
| 09:00 | 博物馆与老街游览 {marker} | 4h | 提前预约 |

## 💰 总预算拆解

人均约 ¥500。

## 🚨 必做预约 & 证件清单

携带身份证并提前预约热门场馆。

## ⚠️ 避坑提示

以官方实时开放信息为准。

## 🎒 行前物品清单

身份证、雨具、常用药和充电宝。

## 🌳 行程知识图谱

```
[成都]
└── Day 1 · 城市漫游
```

> **免责声明**：请以官方实时信息为准。
"""


def _report_with_program_major(marker: str) -> str:
    """构造缺少行前物品清单的报告，程序审核应判定为 major。"""
    report = _complete_report(marker)
    return report.replace(
        "## 🎒 行前物品清单\n\n"
        "身份证、雨具、常用药和充电宝。\n\n",
        "",
    )


class _SequencedReportLLM:
    """每次正文调用返回预先配置的一份完整报告。"""

    last_finish_reason = "stop"

    def __init__(self, *reports: str):
        self.reports = list(reports)
        self.calls = 0

    async def chat_stream(self, messages):
        index = self.calls
        self.calls += 1
        self.last_finish_reason = "stop"
        if index >= len(self.reports):
            raise AssertionError("专业版自动修复调用次数超过预期")
        yield self.reports[index]


class _ReviewLLM:
    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.calls = 0

    async def chat_json(self, messages):
        index = self.calls
        self.calls += 1
        if index >= len(self.responses):
            raise AssertionError("专业版审核调用次数超过预期")
        return self.responses[index]


class _RepairRuntimeErrorLLM:
    """初稿正常返回，进入隐藏修复时抛出普通运行时异常。"""

    last_finish_reason = "stop"

    def __init__(self, draft: str):
        self.draft = draft
        self.calls = 0

    async def chat_stream(self, messages):
        self.calls += 1
        self.last_finish_reason = "stop"
        if self.calls == 1:
            yield self.draft
            return
        raise RuntimeError("simulated repair runtime failure")


def _review_response(*, severity: str, evidence: str) -> str:
    return json.dumps(
        {
            "summary": "发现一项需要关注的问题",
            "issues": [
                {
                    "id": "L001",
                    "severity": severity,
                    "category": "internal_contradiction",
                    "locations": ["Day 1"],
                    "evidence": evidence,
                    "violated_constraint": "报告内部信息必须一致",
                    "diagnosis": "该标记代表测试中的前后矛盾",
                    "repair_instruction": "移除矛盾并输出完整终稿",
                    "confidence": 0.95,
                    "suggested_action": "rewrite",
                }
            ],
        },
        ensure_ascii=False,
    )


def _orchestrator(report_llm, review_llm) -> TravelGuideOrchestrator:
    orchestrator = TravelGuideOrchestrator(
        "https://api.example.com/v1",
        "test-key",
        "test-model",
        mode="professional",
    )
    orchestrator.llm = report_llm
    orchestrator.review_llm = review_llm
    orchestrator.review_mode = "repair"
    orchestrator.rewrite_max_attempts = 1
    return orchestrator


def _consume_review_pipeline(orchestrator: TravelGuideOrchestrator) -> list[dict]:
    async def consume():
        return [
            event
            async for event in orchestrator._generate_reviewed_professional(
                query="成都1日城市游",
                messages=[{"role": "user", "content": "生成专业版攻略"}],
                travel_data={},
                route=None,
                day_plan=None,
                generation_started=time.monotonic(),
            )
        ]

    return asyncio.run(consume())


def _consume_review_pipeline_capturing_error(
    orchestrator: TravelGuideOrchestrator,
) -> tuple[list[dict], Exception | None]:
    async def consume():
        events = []
        try:
            async for event in orchestrator._generate_reviewed_professional(
                query="成都1日城市游",
                messages=[{"role": "user", "content": "生成专业版攻略"}],
                travel_data={},
                route=None,
                day_plan=None,
                generation_started=time.monotonic(),
            ):
                events.append(event)
        except Exception as exc:  # 测试需要同时断言异常与此前的事件边界
            return events, exc
        return events, None

    return asyncio.run(consume())


def _published(events: list[dict]) -> list[dict]:
    return [event for event in events if event["type"] == "final_content"]


def test_professional_draft_never_leaks_as_content_and_only_final_is_published():
    draft = _complete_report("DRAFT_ONLY_MARKER")
    report_llm = _SequencedReportLLM(draft)
    review_llm = _ReviewLLM('{"summary":"审核通过","issues":[]}')
    orchestrator = _orchestrator(report_llm, review_llm)

    events = _consume_review_pipeline(orchestrator)

    assert not [event for event in events if event["type"] == "content"]
    assert _published(events) == [{"type": "final_content", "data": draft}]
    assert orchestrator.last_review_trace["draft_markdown"] == draft
    assert orchestrator.last_review_trace["final_markdown"] == draft
    assert report_llm.calls == 1
    assert review_llm.calls == 1


def test_major_review_issue_triggers_at_most_one_hidden_repair():
    draft = _complete_report("MAJOR_DRAFT_MARKER")
    repaired = _complete_report("FINAL_REPAIRED_MARKER")
    report_llm = _SequencedReportLLM(draft, repaired)
    review_llm = _ReviewLLM(
        _review_response(severity="major", evidence="MAJOR_DRAFT_MARKER")
    )
    orchestrator = _orchestrator(report_llm, review_llm)

    events = _consume_review_pipeline(orchestrator)

    assert report_llm.calls == 2  # 一次初稿 + 最多一次完整修复
    assert review_llm.calls == 1
    assert not [event for event in events if event["type"] == "content"]
    assert _published(events) == [{"type": "final_content", "data": repaired}]
    assert "MAJOR_DRAFT_MARKER" not in _published(events)[0]["data"]
    assert orchestrator.last_review_trace["rewritten"] is True
    assert orchestrator.last_review_trace["final_markdown"] == repaired


def test_minor_review_issue_does_not_rewrite_professional_report():
    draft = _complete_report("MINOR_DRAFT_MARKER")
    report_llm = _SequencedReportLLM(draft)
    review_llm = _ReviewLLM(
        _review_response(severity="minor", evidence="MINOR_DRAFT_MARKER")
    )
    orchestrator = _orchestrator(report_llm, review_llm)

    events = _consume_review_pipeline(orchestrator)

    assert report_llm.calls == 1
    assert review_llm.calls == 1
    assert _published(events) == [{"type": "final_content", "data": draft}]
    assert orchestrator.last_review_trace["rewritten"] is False
    assert orchestrator.last_review_trace["review"]["highest_severity"] == "minor"


def test_invalid_review_json_retries_once_then_safely_publishes_hard_checked_draft():
    draft = _complete_report("SAFE_DRAFT_MARKER")
    report_llm = _SequencedReportLLM(draft)
    review_llm = _ReviewLLM("not-json", '{"summary":"missing issues"}')
    orchestrator = _orchestrator(report_llm, review_llm)

    events = _consume_review_pipeline(orchestrator)

    assert review_llm.calls == 2
    assert report_llm.calls == 1
    assert _published(events) == [{"type": "final_content", "data": draft}]
    assert orchestrator.last_review_trace["fallback_reason"] == "review_unavailable"
    assert orchestrator.last_review_trace["review"]["valid"] is False
    progress = "\n".join(
        event["data"] for event in events if event["type"] == "progress"
    )
    assert "正在自动重试一次" in progress
    assert "安全降级发布" in progress


def test_program_major_cannot_fallback_when_repaired_report_still_fails_final_audit():
    draft = _report_with_program_major("PROGRAM_MAJOR_DRAFT")
    still_invalid = _report_with_program_major("STILL_INVALID_REPAIR")
    report_llm = _SequencedReportLLM(draft, still_invalid)
    review_llm = _ReviewLLM('{"summary":"程序问题待修复","issues":[]}')
    orchestrator = _orchestrator(report_llm, review_llm)

    events, error = _consume_review_pipeline_capturing_error(orchestrator)

    assert isinstance(error, LLMClientError)
    assert not _published(events)
    assert report_llm.calls == 2
    assert orchestrator.last_review_trace["final_markdown"] == ""


def test_program_major_cannot_fallback_when_repair_raises_runtime_error():
    draft = _report_with_program_major("PROGRAM_MAJOR_DRAFT")
    report_llm = _RepairRuntimeErrorLLM(draft)
    review_llm = _ReviewLLM('{"summary":"程序问题待修复","issues":[]}')
    orchestrator = _orchestrator(report_llm, review_llm)

    events, error = _consume_review_pipeline_capturing_error(orchestrator)

    assert isinstance(error, LLMClientError)
    assert not _published(events)
    assert report_llm.calls == 2
    assert orchestrator.last_review_trace["final_markdown"] == ""


def test_model_only_major_may_fallback_when_repaired_report_fails_hard_audit():
    draft = _complete_report("MODEL_MAJOR_DRAFT")
    invalid_repair = _report_with_program_major("INVALID_REPAIR")
    report_llm = _SequencedReportLLM(draft, invalid_repair)
    review_llm = _ReviewLLM(
        _review_response(severity="major", evidence="MODEL_MAJOR_DRAFT")
    )
    orchestrator = _orchestrator(report_llm, review_llm)

    events = _consume_review_pipeline(orchestrator)

    assert _published(events) == [{"type": "final_content", "data": draft}]
    assert report_llm.calls == 2
    assert orchestrator.last_review_trace["fallback_reason"] == (
        "repair_final_validation_failed"
    )
    assert orchestrator.last_review_trace["repair_attempted"] is True
    assert orchestrator.last_review_trace["repair_applied"] is False
    assert orchestrator.last_review_trace["rewritten"] is False
    assert orchestrator.last_review_trace["final_markdown"] == draft


def test_model_route_consistency_replan_suggestion_is_rewritten_not_blocked():
    evidence = "报告实际安排二郎剑，偏离锁定目的地"
    draft = _complete_report(evidence)
    repaired = _complete_report("最终稿已恢复锁定目的地")
    report_llm = _SequencedReportLLM(draft, repaired)
    review_llm = _ReviewLLM(json.dumps(
        {
            "summary": "正文安排偏离锁定目的地",
            "issues": [
                {
                    "id": "L-ROUTE-DRIFT",
                    "severity": "major",
                    "category": "route_consistency",
                    "locations": ["Day 1"],
                    "evidence": evidence,
                    "violated_constraint": "报告正文必须遵循程序锁定路线",
                    "diagnosis": evidence,
                    "repair_instruction": "按锁定目的地修正文案，不重建路线骨架",
                    "confidence": 0.96,
                    # 审核模型把正文偏差误标成 route_replan；流水线必须收窄为 rewrite。
                    "suggested_action": "route_replan",
                }
            ],
        },
        ensure_ascii=False,
    ))
    orchestrator = _orchestrator(report_llm, review_llm)

    events = _consume_review_pipeline(orchestrator)

    assert report_llm.calls == 2
    assert review_llm.calls == 1
    assert _published(events) == [{"type": "final_content", "data": repaired}]
    assert orchestrator.last_review_trace["repair_attempted"] is True
    assert orchestrator.last_review_trace["repair_applied"] is True
    assert orchestrator.last_review_trace["rewritten"] is True
    assert orchestrator.last_review_trace["final_markdown"] == repaired
