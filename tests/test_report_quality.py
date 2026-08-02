"""专业版报告审核核心测试（不调用网络或真实模型）。"""

import json

import pytest

from services.report_quality import (
    ReviewIssue,
    ReviewResult,
    audit_report,
    build_repair_messages,
    build_review_messages,
    decide_review_action,
    merge_review_results,
    needs_rewrite,
    parse_review_result,
    requires_route_replan,
)


DAY_PLAN = {
    "overview": "乌鲁木齐 → 和静县巴音布鲁克草原",
    "days": [
        {
            "day": 1,
            "kind": "transfer",
            "from": "乌鲁木齐",
            "to": "和静县巴音布鲁克草原",
            "via": [],
            "km": 500,
            "hours": 8,
        },
        {
            "day": 2,
            "kind": "stay",
            "at": "和静县巴音布鲁克草原",
        },
    ],
}

ROUTE = {
    "seq_names": ["乌鲁木齐", "和静县巴音布鲁克草原"],
    "legs": [{
        "from": "乌鲁木齐",
        "to": "和静县巴音布鲁克草原",
        "km": 500,
        "hours": 8,
    }],
    "min_stay_days": {"和静县巴音布鲁克草原": 1},
    "visit_requirements": {
        "和静县巴音布鲁克草原": "必须进入景区实质游览并在镇上住宿",
    },
}


def complete_report(day_two_text: str = "进入景区实质游览，傍晚入住镇上酒店。") -> str:
    return f"""# 🗺️ 巴音布鲁克 2 日自驾行程

总天数 2 天，总里程 500km。
乌鲁木齐 → 和静县巴音布鲁克草原

## 🌤️ 天气与穿搭

昼夜温差较大，需携带冲锋衣。

## 🚄 城际交通建议

自驾前往，出发前复核路况。

## 🏨 住宿推荐

推荐入住巴音布鲁克镇。

## 📅 分日行程

### Day 1 · 前往草原 · 乌鲁木齐 → 和静县巴音布鲁克草原 · 500km · 约8h

| 时段 | 安排 | 耗时 | 提示 |
|---|---|---|---|
| 08:00 | 出发 | 8h | 每 2 小时休息 |

### Day 2 · 草原深度游 · 和静县巴音布鲁克草原 深度游/休整 · 0km

| 时段 | 安排 | 耗时 | 提示 |
|---|---|---|---|
| 09:00 | {day_two_text} | 8h | 提前预约 |

## 💰 总预算拆解

人均约 ¥1000。

## 🚨 必做预约 & 证件清单

提前预约门票，携带身份证。

## ⚠️ 避坑提示

出发前核实天气和路况。

## 🎒 行前物品清单

身份证、冲锋衣、常用药、充电宝。

## 🌳 行程知识图谱

```
[乌鲁木齐]
├── Day 1 · 前往草原
└── Day 2 · 巴音布鲁克深度游
```

> **免责声明**：请以官方实时信息为准。
"""


def valid_raw_issue(**overrides) -> dict:
    value = {
        "id": "I1",
        "severity": "critical",
        "category": "route_order",
        "locations": ["Day 9-11"],
        "evidence": "喀什 → 库车 → 天池",
        "violated_constraint": "路线必须单向推进",
        "diagnosis": "形成明显跨区折返",
        "repair_instruction": "按锁定骨架重新排列相关 Day",
        "confidence": 0.96,
        "suggested_action": "rewrite",
    }
    value.update(overrides)
    return value


def test_parse_review_result_accepts_fenced_json_and_derives_verdict():
    draft = "路线总览：喀什 → 库车 → 天池"
    raw = "```json\n" + json.dumps({
        "verdict": "pass",
        "highest_severity": "none",
        "summary": "存在折返",
        "issues": [valid_raw_issue()],
    }, ensure_ascii=False) + "\n```"

    result = parse_review_result(raw, draft)

    assert result.valid is True
    assert result.verdict == "repair"
    assert result.highest_severity == "critical"
    assert result.needs_rewrite is True
    assert result.issues[0].source == "llm"


def test_parse_review_result_filters_unknown_low_confidence_and_fake_evidence():
    draft = "路线总览：喀什 → 库车 → 天池"
    raw = {
        "summary": "混合结果",
        "issues": [
            valid_raw_issue(id="good"),
            valid_raw_issue(id="unknown", category="style_problem"),
            valid_raw_issue(id="low", confidence=0.79),
            valid_raw_issue(id="fake", evidence="报告中根本不存在的路线"),
            valid_raw_issue(id="none", suggested_action="none"),
        ],
    }

    result = parse_review_result(raw, draft)

    assert [issue.id for issue in result.issues] == ["good"]
    assert result.rejected_count == 4
    assert result.raw_issue_count == 5


def test_parse_review_result_marks_bad_response_invalid():
    assert parse_review_result("not json", "draft").valid is False
    result = parse_review_result('{"summary":"x"}', "draft")
    assert result.valid is False
    assert "issues" in result.parse_error


def test_model_report_divergence_cannot_force_route_replan():
    draft = "Day 9 安排二郎剑景区并住宿刚察县"
    raw = {
        "summary": "正文偏离锁定地点",
        "issues": [valid_raw_issue(
            category="route_consistency",
            evidence="安排二郎剑景区并住宿刚察县",
            diagnosis=(
                "锁定路线 Day 9 为卓尔山到海晏县青海湖，但报告实际安排二郎剑景区，"
                "偏离锁定目的地"
            ),
            suggested_action="route_replan",
        )],
    }

    result = parse_review_result(raw, draft)

    assert result.issues[0].suggested_action == "rewrite"
    assert decide_review_action(result) == "rewrite"


def test_model_can_still_request_replan_for_true_route_order_problem():
    draft = "路线为喀什 → 库车 → 天池，形成明显折返"
    raw = {
        "summary": "底层路线折返",
        "issues": [valid_raw_issue(
            category="route_order",
            evidence="喀什 → 库车 → 天池",
            diagnosis="锁定 day_plan 本身先向西到喀什，再向东折返库车",
            suggested_action="route_replan",
        )],
    }

    result = parse_review_result(raw, draft)

    assert result.issues[0].suggested_action == "route_replan"
    assert decide_review_action(result) == "route_replan"


def test_program_audit_accepts_complete_report_with_locked_plan():
    issues = audit_report(
        complete_report(),
        query="乌鲁木齐出发，巴音布鲁克自驾2天",
        day_plan=DAY_PLAN,
        route=ROUTE,
    )

    assert issues == []


def test_program_audit_finds_missing_day_and_knowledge_coverage():
    draft = complete_report().replace(
        "### Day 2 · 草原深度游 · 和静县巴音布鲁克草原 深度游/休整 · 0km",
        "#### 草原深度游",
    ).replace("└── Day 2 · 巴音布鲁克深度游\n", "")

    issues = audit_report(draft, query="巴音布鲁克自驾2天", day_plan=DAY_PLAN)

    assert any(issue.category == "missing_day" and "Day 2" in issue.diagnosis for issue in issues)
    assert any(issue.category == "knowledge_graph" and "Day 2" in issue.diagnosis for issue in issues)
    assert any(issue.category == "route_consistency" for issue in issues)


def test_program_audit_rejects_missing_required_sections():
    draft = complete_report().replace(
        "## 🎒 行前物品清单\n\n身份证、冲锋衣、常用药、充电宝。\n\n",
        "",
    )

    issues = audit_report(draft, query="巴音布鲁克自驾2天")

    packing = [issue for issue in issues if issue.locations == ("行前物品清单",)]
    assert len(packing) == 1
    assert packing[0].category == "missing_section"
    assert packing[0].severity == "major"


def test_program_audit_rejects_must_visit_downgraded_to_pass_through():
    draft = complete_report("到观景台远眺即可，不进入景区，随后返回酒店。")

    issues = audit_report(draft, day_plan=DAY_PLAN, route=ROUTE)

    issue = next(issue for issue in issues if issue.category == "must_visit")
    assert issue.severity == "critical"
    assert "远眺" in issue.evidence
    assert issue.suggested_action == "rewrite"


@pytest.mark.parametrize("downgrade", [
    "仅在外围远眺即可，不进入景区，随后离开。",
    "当天只途经巴音布鲁克，短暂停留后离开。",
    "禁止入园，只能在外围远眺即可。",
])
def test_program_audit_rejects_actual_must_visit_downgrade_variants(downgrade):
    issues = audit_report(complete_report(downgrade), day_plan=DAY_PLAN, route=ROUTE)

    assert any(
        issue.category == "must_visit" and issue.severity == "critical"
        for issue in issues
    )


@pytest.mark.parametrize("reminder", [
    "必须进入景区实质游览并入住镇上酒店，禁止只路过、远眺或只作短暂休息。",
    "乘景交进入景区游览并入住镇上酒店，不要只路过，应入园参观。",
    "进入景区参观后入住镇上酒店，严禁仅作短暂停留。",
])
def test_program_audit_accepts_positive_anti_downgrade_reminder(reminder):
    issues = audit_report(complete_report(reminder), day_plan=DAY_PLAN, route=ROUTE)

    assert not any(issue.category == "must_visit" for issue in issues)


def test_program_audit_accepts_official_closure_fallback_after_real_visit_plan():
    text = (
        "上午乘景交进入景区实质游览，傍晚入住镇上酒店。"
        "仅如遇官方临时闭园或极端天气管制，备用方案改为外围安全点远眺即可，不进入封闭区域。"
    )

    issues = audit_report(complete_report(text), day_plan=DAY_PLAN, route=ROUTE)

    assert not any(issue.category == "must_visit" for issue in issues)


def test_program_audit_rejects_time_saving_downgrade_even_after_visit_plan():
    text = (
        "上午进入景区游览并预订镇上酒店；若时间不足，也可不进入景区，"
        "只在外围远眺即可。"
    )

    issues = audit_report(complete_report(text), day_plan=DAY_PLAN, route=ROUTE)

    assert any(
        issue.category == "must_visit" and issue.severity == "critical"
        for issue in issues
    )


def test_must_visit_uses_stay_day_instead_of_arrival_day_remote_view():
    draft = complete_report().replace(
        "| 08:00 | 出发 | 8h | 每 2 小时休息 |",
        "| 08:00 | 抵达镇区后仅在外围远眺即可，不进入景区 | 8h | 次日深度游览 |",
    )

    issues = audit_report(draft, day_plan=DAY_PLAN, route=ROUTE)

    assert not any(issue.category == "must_visit" for issue in issues)


def test_must_visit_ignores_remote_view_of_another_landscape_after_real_visit():
    text = (
        "乘景交进入景区实质游览，傍晚入住镇上酒店；"
        "之后在镇外观景台远眺天山雪峰即可。"
    )

    issues = audit_report(complete_report(text), day_plan=DAY_PLAN, route=ROUTE)

    assert not any(issue.category == "must_visit" for issue in issues)


def test_must_visit_does_not_consume_following_top_level_sections():
    draft = complete_report().replace(
        "出发前核实天气和路况。",
        "其他景点如遇关闭，可在外围远眺即可，不进入封闭区域。",
    )

    issues = audit_report(draft, day_plan=DAY_PLAN, route=ROUTE)

    assert not any(issue.category == "must_visit" for issue in issues)


def test_requirement_reminder_alone_is_not_real_visit_or_overnight_evidence():
    reminder = (
        "必游硬约束：必须进入巴音布鲁克景区安排实质游览并在镇上住宿，"
        "禁止只路过、远眺或只作短暂休息。"
    )

    issues = audit_report(complete_report(reminder), day_plan=DAY_PLAN, route=ROUTE)

    issue = next(issue for issue in issues if issue.category == "must_visit")
    assert issue.severity == "major"
    assert "实质游览和住宿安排" in issue.diagnosis


def test_program_audit_does_not_treat_deep_visit_heading_as_body_evidence():
    draft = complete_report("全天在车内休息，晚上返回市区。")

    issues = audit_report(draft, day_plan=DAY_PLAN, route=ROUTE)

    issue = next(issue for issue in issues if issue.category == "must_visit")
    assert issue.severity == "major"
    assert "实质游览" in issue.diagnosis


def test_program_audit_rejects_changed_locked_distance_or_duration():
    draft = complete_report().replace("500km · 约8h", "390km · 约6h")

    issues = audit_report(draft, day_plan=DAY_PLAN, route=ROUTE)

    issue = next(
        issue for issue in issues
        if issue.category == "route_consistency" and issue.evidence.startswith("前往草原")
    )
    assert issue.severity == "critical"
    assert "里程或驾驶时长" in issue.diagnosis


def test_day_plan_overview_wins_when_it_contains_inserted_safety_stop():
    route = dict(ROUTE, seq_names=["乌鲁木齐", "巴音布鲁克"])
    plan = dict(DAY_PLAN, overview="乌鲁木齐 → 和静县巴音布鲁克草原")

    issues = audit_report(complete_report(), day_plan=plan, route=route)

    assert not any(issue.category == "route_order" for issue in issues)


def test_program_audit_blocks_unsafe_locked_drive_day():
    unsafe_plan = {
        "overview": DAY_PLAN["overview"],
        "days": [dict(DAY_PLAN["days"][0], km=900, hours=11)],
    }
    draft = complete_report().replace("500km · 约8h", "900km · 约11h")

    issues = audit_report(draft, day_plan=unsafe_plan)

    assert any(issue.category == "driving_feasibility" for issue in issues)
    assert decide_review_action(issues) == "route_replan"


def test_program_audit_requests_route_replan_when_required_stay_missing():
    no_stay_plan = {
        "overview": DAY_PLAN["overview"],
        "days": [DAY_PLAN["days"][0]],
    }
    one_day = complete_report().replace(
        "### Day 2 · 草原深度游 · 和静县巴音布鲁克草原 深度游/休整 · 0km\n\n"
        "| 时段 | 安排 | 耗时 | 提示 |\n|---|---|---|---|\n"
        "| 09:00 | 进入景区实质游览，傍晚入住镇上酒店。 | 8h | 提前预约 |\n\n",
        "",
    )

    issues = audit_report(one_day, day_plan=no_stay_plan, route=ROUTE)

    assert requires_route_replan(issues) is True
    assert decide_review_action(issues) == "route_replan"


def test_program_audit_requires_locked_corridor_and_via():
    route = dict(ROUTE)
    route["legs"] = [{
        "from": "乌鲁木齐",
        "to": "和静县巴音布鲁克草原",
        "corridor": "G217独库公路南段",
        "via": ["库车大小龙池"],
    }]

    issues = audit_report(complete_report(), day_plan=DAY_PLAN, route=route)

    assert any("G217独库公路南段" in issue.diagnosis for issue in issues)
    assert any("库车大小龙池" in issue.diagnosis for issue in issues)


def test_decision_helpers_keep_route_replan_separate_from_rewrite():
    rewrite = ReviewIssue(
        id="x", severity="major", category="missing_section",
        diagnosis="missing", repair_instruction="fix", suggested_action="rewrite",
    )
    replan = ReviewIssue(
        id="y", severity="critical", category="route_order",
        diagnosis="bad route", repair_instruction="replan", suggested_action="route_replan",
    )
    minor = ReviewIssue(
        id="z", severity="minor", category="internal_contradiction",
        diagnosis="wording", repair_instruction="polish", suggested_action="rewrite",
    )

    assert needs_rewrite([rewrite]) is True
    assert needs_rewrite([replan]) is False
    assert needs_rewrite([minor]) is False
    assert decide_review_action([rewrite, replan]) == "route_replan"
    assert decide_review_action([minor]) == "publish"


def test_merge_review_results_recomputes_highest_severity():
    program = ReviewIssue(
        id="P001", severity="critical", category="missing_day",
        locations=("Day 2",), diagnosis="missing", repair_instruction="fix",
    )
    model = parse_review_result({
        "summary": "另有折返",
        "issues": [valid_raw_issue(severity="major")],
    }, "喀什 → 库车 → 天池")

    merged = merge_review_results([program], model)

    assert merged.highest_severity == "critical"
    assert merged.verdict == "repair"
    assert len(merged.issues) == 2


def test_message_builders_keep_audit_and_repair_as_separate_tasks():
    issue = ReviewIssue(
        id="P001", severity="critical", category="must_visit",
        locations=("巴音布鲁克",), diagnosis="仅远眺",
        repair_instruction="进入景区实质游览", suggested_action="rewrite",
    )

    review = build_review_messages(
        "巴音布鲁克2天",
        complete_report(),
        {"tips": "以官方实时公告为准"},
        DAY_PLAN,
        [issue],
    )
    repair = build_repair_messages(
        "巴音布鲁克2天",
        complete_report(),
        {"tips": "以官方实时公告为准"},
        DAY_PLAN,
        [issue],
    )

    assert [message["role"] for message in review] == ["system", "user"]
    assert "只输出一个 JSON 对象" in review[0]["content"]
    assert "evidence 必须是报告原文" in review[0]["content"]
    assert "Day 标题中出现的途经点已经算明确落实" in review[0]["content"]
    assert "景点安排 2-3 小时本身不构成 major" in review[0]["content"]
    assert "程序预检为空" in review[0]["content"]
    assert "应报告 route_consistency 并使用 rewrite" in review[0]["content"]
    assert '"id": "P001"' in review[1]["content"]
    assert "输出修复后的完整终稿" in repair[1]["content"]
    assert "禁止擅自修改" in repair[0]["content"]


def test_message_builder_limits_oversized_realtime_context():
    messages = build_review_messages("两天行程", "草稿", {"raw": "x" * 30_000})

    assert "实时数据已截断" in messages[1]["content"]
    assert len(messages[1]["content"]) < 20_000


def test_empty_model_issue_list_is_valid_pass():
    result = parse_review_result(
        {"verdict": "repair", "highest_severity": "critical", "issues": []},
        "完整草稿",
    )

    assert result == ReviewResult(raw_issue_count=0)
