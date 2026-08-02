"""专业版旅行报告的确定性校验、LLM 审核解析与修复提示词。

本模块刻意不负责发起模型请求。编排层可以用与正文相同的 ``LLM_MODEL``
调用 :func:`build_review_messages`，再把返回值交给
:func:`parse_review_result`。这样审核策略可以独立测试，也不会让模型的
``verdict`` 绕过程序硬校验。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence


ALLOWED_SEVERITIES = frozenset({"critical", "major", "minor"})
ALLOWED_CATEGORIES = frozenset({
    "incomplete_report",
    "missing_day",
    "missing_section",
    "knowledge_graph",
    "route_order",
    "route_consistency",
    "location_mismatch",
    "driving_feasibility",
    "must_visit",
    "requirement_mismatch",
    "timing_feasibility",
    "budget_consistency",
    "accommodation_consistency",
    "internal_contradiction",
    "factual_risk",
})
ALLOWED_ACTIONS = frozenset({
    "rewrite", "route_replan", "program_fix", "manual_verify", "none",
})
MIN_LLM_ISSUE_CONFIDENCE = 0.8

# LLM 只有在问题指向底层路线骨架本身时，才允许要求 route_replan。
# 报告正文没有严格照抄骨架、住宿点表述不同等问题都能通过重写修复，不能
# 因模型误用动作而让整次请求直接失败。
_LLM_ROUTE_REPLAN_CATEGORIES = frozenset({
    "route_order",
    "location_mismatch",
    "driving_feasibility",
    "must_visit",
    "requirement_mismatch",
})
_REPORT_VS_PLAN_RE = re.compile(
    r"(?:报告|正文|时段表|住宿).{0,100}"
    r"(?:偏离锁定|与锁定.{0,30}不一致|未按锁定)",
)

_SEVERITY_RANK = {"minor": 1, "major": 2, "critical": 3}
_DAY_HEADING_RE = re.compile(
    r"^#{1,4}\s*Day\s*(\d+)\s*(?:[·:\-—]\s*)?([^\n]*)$",
    re.MULTILINE | re.IGNORECASE,
)
_QUERY_DAYS_RE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:天|日)(?!\d)")
_FORBIDDEN_PASS_THROUGH_RE = re.compile(
    r"(?:只(?:是|作|做)?|仅(?:作|做)?|不进入|不进景区|不入园|无需进入|放弃进入).{0,8}"
    r"(?:路过|途经|远眺|车览|短暂停留|打卡)|"
    r"(?:路过|途经|远眺|车览|短暂停留).{0,8}(?:即可|为主|不进入|不进景区|不入园)",
)
_ANTI_DOWNGRADE_DIRECTIVE_RE = re.compile(
    r"(?:禁止|严禁|切勿|避免|不要|不可|不得|不能|不应|杜绝|拒绝)"
)
_OFFICIAL_CLOSURE_RE = re.compile(
    r"(?:闭园|关闭|临时封闭|暂停开放|交通管制|景区管制|极端天气|恶劣天气|"
    r"官方(?:通知|公告)|不可抗力)"
)
_CONDITIONAL_FALLBACK_RE = re.compile(r"(?:如遇|若遇|若因|仅在|一旦|遇到)")
_TIME_SAVING_DOWNGRADE_RE = re.compile(r"(?:时间不足|来不及|赶时间|节省时间|行程延误)")
_VISIT_ACTIVITY_RE = re.compile(r"(?:进入|入园|游览|参观|景区内|乘坐景交)")
_OVERNIGHT_RE = re.compile(r"(?:住宿|入住|过夜|住在|酒店|民宿|客栈)")
_KM_RE = re.compile(r"(?P<value>\d[\d,]*(?:\.\d+)?)\s*(?:km|公里)", re.I)
_HOURS_RE = re.compile(r"约?\s*(?P<value>\d+(?:\.\d+)?)\s*(?:h|小时)", re.I)

_REQUIRED_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("weather", ("天气", "穿搭")),
    ("transport", ("城际交通", "交通建议")),
    ("lodging", ("住宿推荐", "酒店推荐", "住宿建议")),
    ("itinerary", ("分日行程", "详细行程", "每日行程")),
    ("budget", ("总预算", "预算拆解", "费用汇总")),
    ("booking", ("必做预约", "预约 & 证件", "预约&证件", "预约证件")),
    ("pitfalls", ("避坑提示", "风险提示")),
    ("packing", ("行前物品清单", "行李清单")),
    ("knowledge", ("行程知识图谱",)),
)


@dataclass(frozen=True)
class ReviewIssue:
    """一项已被程序接受的报告问题。"""

    id: str
    severity: str
    category: str
    locations: tuple[str, ...] = ()
    evidence: str = ""
    violated_constraint: str = ""
    diagnosis: str = ""
    repair_instruction: str = ""
    confidence: float = 1.0
    suggested_action: str = "rewrite"
    source: str = "program"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["locations"] = list(self.locations)
        return value


@dataclass
class ReviewResult:
    """审核解析结果；``valid=False`` 表示模型响应不可安全采用。"""

    verdict: str = "pass"
    highest_severity: str = "none"
    summary: str = ""
    issues: list[ReviewIssue] = field(default_factory=list)
    valid: bool = True
    parse_error: str = ""
    rejected_count: int = 0
    raw_issue_count: int = 0

    @property
    def needs_rewrite(self) -> bool:
        return needs_rewrite(self)

    @property
    def requires_route_replan(self) -> bool:
        return requires_route_replan(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "highest_severity": self.highest_severity,
            "summary": self.summary,
            "issues": [issue.to_dict() for issue in self.issues],
            "valid": self.valid,
            "parse_error": self.parse_error,
            "rejected_count": self.rejected_count,
            "raw_issue_count": self.raw_issue_count,
        }


def _highest_severity(issues: Iterable[ReviewIssue]) -> str:
    values = list(issues)
    if not values:
        return "none"
    return max(values, key=lambda item: _SEVERITY_RANK[item.severity]).severity


def _derived_verdict(issues: Iterable[ReviewIssue]) -> str:
    values = list(issues)
    if any(issue.severity in {"critical", "major"} for issue in values):
        return "repair"
    return "pass"


def _clean_text(value: Any, max_length: int = 500) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_length]


def _clean_locations(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values: Sequence[Any] = (value,)
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        values = ()
    result: list[str] = []
    for item in values[:8]:
        cleaned = _clean_text(item, 80)
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return tuple(result)


def _normalize_evidence(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.strip().strip("`'\"“”‘’")
    return re.sub(r"\s+", " ", value)


def _evidence_exists(evidence: str, draft: str) -> bool:
    """证据必须是草稿里的连续文本，最多只放宽空白差异。"""
    evidence = _normalize_evidence(evidence)
    normalized_draft = _normalize_evidence(draft)
    return len(evidence) >= 2 and evidence in normalized_draft


def _extract_json_object(raw: Any) -> Mapping[str, Any] | None:
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, Mapping) else None
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            return value
    return None


def parse_review_result(raw: Any, draft: str) -> ReviewResult:
    """解析审核 JSON，并丢弃无白名单、低置信度或无原文证据的问题。

    ``verdict`` 与 ``highest_severity`` 始终由接受的问题重新计算；模型不能
    仅靠声称 ``critical`` 或 ``repair`` 触发昂贵的整篇重写。
    """
    payload = _extract_json_object(raw)
    if payload is None:
        return ReviewResult(
            valid=False,
            parse_error="审核模型未返回有效 JSON 对象",
        )
    raw_issues = payload.get("issues")
    if not isinstance(raw_issues, list):
        return ReviewResult(
            valid=False,
            parse_error="审核 JSON 缺少 issues 数组",
        )

    accepted: list[ReviewIssue] = []
    rejected = 0
    for index, item in enumerate(raw_issues, 1):
        if not isinstance(item, Mapping):
            rejected += 1
            continue
        severity = _clean_text(item.get("severity"), 20).lower()
        category = _clean_text(item.get("category"), 50).lower()
        action = _clean_text(item.get("suggested_action"), 30).lower()
        evidence = _clean_text(item.get("evidence"), 500)
        constraint = _clean_text(item.get("violated_constraint"), 500)
        diagnosis = _clean_text(item.get("diagnosis"), 800)
        instruction = _clean_text(item.get("repair_instruction"), 800)
        confidence_raw = item.get("confidence")
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            confidence = -1.0

        # 模型偶尔会把“正文安排与锁定目的地表述不同”误判成必须重建路线。
        # 这类问题只需按既有骨架重写正文。真正的 route_replan 仅保留给路线
        # 顺序、驾驶上限、必游停留不足或用户硬约束缺失等骨架级问题。
        if action == "route_replan" and (
            category not in _LLM_ROUTE_REPLAN_CATEGORIES
            or _REPORT_VS_PLAN_RE.search(diagnosis)
        ):
            action = "rewrite"

        valid = (
            severity in ALLOWED_SEVERITIES
            and category in ALLOWED_CATEGORIES
            and action in ALLOWED_ACTIONS
            and not isinstance(confidence_raw, bool)
            and MIN_LLM_ISSUE_CONFIDENCE <= confidence <= 1.0
            and bool(constraint and diagnosis and instruction)
            and _evidence_exists(evidence, draft)
        )
        # major/critical 必须给出可执行动作，不能用 none 偷渡高严重度结论。
        if severity in {"critical", "major"} and action == "none":
            valid = False
        if not valid:
            rejected += 1
            continue
        issue_id = _clean_text(item.get("id"), 40) or f"L{index:03d}"
        accepted.append(ReviewIssue(
            id=issue_id,
            severity=severity,
            category=category,
            locations=_clean_locations(item.get("locations")),
            evidence=_normalize_evidence(evidence),
            violated_constraint=constraint,
            diagnosis=diagnosis,
            repair_instruction=instruction,
            confidence=confidence,
            suggested_action=action,
            source="llm",
        ))

    summary = _clean_text(payload.get("summary"), 1000)
    return ReviewResult(
        verdict=_derived_verdict(accepted),
        highest_severity=_highest_severity(accepted),
        summary=summary,
        issues=accepted,
        valid=True,
        rejected_count=rejected,
        raw_issue_count=len(raw_issues),
    )


def _program_issue(
    number: int,
    *,
    severity: str,
    category: str,
    diagnosis: str,
    repair_instruction: str,
    evidence: str = "",
    locations: Sequence[str] = (),
    action: str = "rewrite",
) -> ReviewIssue:
    return ReviewIssue(
        id=f"P{number:03d}",
        severity=severity,
        category=category,
        locations=tuple(locations),
        evidence=evidence,
        violated_constraint=diagnosis,
        diagnosis=diagnosis,
        repair_instruction=repair_instruction,
        confidence=1.0,
        suggested_action=action,
        source="program",
    )


def audit_route_plan(
    query: str,
    route: Mapping[str, Any] | None,
    day_plan: Mapping[str, Any] | None = None,
) -> list[ReviewIssue]:
    """在正文生成前校验路线定位元数据与单日驾驶硬上限。

    这道门禁同时服务标准版和专业版。它不判断攻略文风，只阻止已经被
    地图层证明为未解析、行政区不符或类型错误的地点进入锁定骨架。
    """
    route = route or {}
    issues: list[ReviewIssue] = []

    def add(**kwargs: Any) -> None:
        issues.append(_program_issue(len(issues) + 1, **kwargs))

    validation = route.get("location_validation_issues")
    if isinstance(validation, list):
        for item in validation:
            if not isinstance(item, Mapping):
                continue
            requested = _clean_text(item.get("requested"), 200) or "未知地点"
            reason = _clean_text(item.get("reason"), 300) or "地图定位未通过校验"
            add(
                severity="critical",
                category="location_mismatch",
                locations=(requested,),
                diagnosis=f"路线节点「{requested}」定位校验失败：{reason}",
                repair_instruction="重新定位并重建路线骨架，禁止沿用当前坐标或让正文模型猜测",
                action="route_replan",
            )

    from services.route_planner import (
        _extract_query_region_hints,
        _query_directly_mentions_location,
        _requested_admin_units,
    )

    query_scope = tuple(_extract_query_region_hints(query))
    route_scope = tuple(route.get("scope_provinces") or query_scope)
    schema_current = route.get("schema_version") == "route-semantics-v6"
    resolutions = route.get("location_resolutions")
    if schema_current and (not isinstance(resolutions, list) or not resolutions):
        add(
            severity="critical",
            category="location_mismatch",
            diagnosis="新版路线缺少地点解析元数据，无法证明锁定坐标可信",
            repair_instruction="重新执行地点定位和省域校验后再生成路线",
            action="route_replan",
        )
    if isinstance(resolutions, list):
        for item in resolutions:
            if not isinstance(item, Mapping):
                continue
            admin_invalid = item.get("admin_match") is False
            type_invalid = item.get("type_valid") is False
            role = item.get("role") or "destination"
            requested = _clean_text(item.get("requested"), 200) or "未知地点"
            # 不信任上游 user_named 布尔值，直接用本次原始需求复核；否则
            # 起点城市或“古城/瀑布”等泛词命中就可能绕过目标省域门禁。
            user_named = _query_directly_mentions_location(query, requested)
            scope_invalid = bool(
                role == "destination"
                and route_scope
                and not user_named
                and item.get("scope_match") is not True
            )
            metadata_missing = bool(
                schema_current
                and (
                    item.get("type_valid") is not True
                    or (
                        _requested_admin_units(requested)
                        and item.get("admin_match") is not True
                    )
                )
            )
            if not (admin_invalid or type_invalid or scope_invalid or metadata_missing):
                continue
            resolved = _clean_text(item.get("resolved_name"), 200) or "未知地图结果"
            resolved_area = "".join(
                _clean_text(item.get(key), 80)
                for key in ("province", "city", "district")
            )
            reasons = []
            if admin_invalid:
                reasons.append("行政区与请求名称不一致")
            if type_invalid:
                poi_type = _clean_text(item.get("poi_type"), 120)
                reasons.append(
                    "POI 类型不适合作为旅行目的地"
                    + (f"（{poi_type}）" if poi_type else "")
                )
            if scope_invalid:
                reasons.append(
                    f"模型推荐地点超出用户目标省域（{'、'.join(route_scope)}）"
                )
            if metadata_missing:
                reasons.append("地点校验元数据不完整")
            add(
                severity="critical",
                category="location_mismatch",
                locations=(requested,),
                evidence=f"{requested} -> {resolved_area}{resolved}",
                diagnosis=(
                    f"路线节点「{requested}」被解析为「{resolved_area}{resolved}」："
                    + "；".join(reasons)
                ),
                repair_instruction="淘汰错误候选，使用行政区和类型均匹配的地图结果后重建路线",
                action="route_replan",
            )

    for index, leg in enumerate(route.get("legs") or [], 1):
        if not isinstance(leg, Mapping):
            continue
        try:
            km = float(leg.get("km") or 0)
            hours = float(leg.get("hours") or 0)
        except (TypeError, ValueError):
            continue
        required_days = max(
            1,
            math.ceil(km / 800) if km > 0 else 1,
            math.ceil(hours / 10) if hours > 0 else 1,
        )
        if required_days <= 1:
            continue
        segments = leg.get("split_segments")

        def valid_segment(segment: Any) -> bool:
            if not isinstance(segment, Mapping) or segment.get("measured") is not True:
                return False
            try:
                segment_km = float(segment.get("km") or 0)
                segment_hours = float(segment.get("hours") or 0)
            except (TypeError, ValueError):
                return False
            return 0 < segment_km <= 800 and 0 < segment_hours <= 10

        segment_list = segments if isinstance(segments, list) else []
        individually_valid = bool(
            len(segment_list) == required_days
            and all(valid_segment(segment) for segment in segment_list)
        )
        chain_valid = bool(
            individually_valid
            and segment_list[0].get("from") == leg.get("from")
            and segment_list[-1].get("to") == leg.get("to")
            and all(
                left.get("to") == right.get("from")
                for left, right in zip(segment_list, segment_list[1:])
            )
        )
        segment_km_total = sum(
            float(segment.get("km") or 0) for segment in segment_list
        ) if individually_valid else 0
        segment_hours_total = sum(
            float(segment.get("hours") or 0) for segment in segment_list
        ) if individually_valid else 0
        totals_consistent = bool(
            km > 0 and hours > 0
            and 0.80 <= segment_km_total / km <= 1.25
            and 0.70 <= segment_hours_total / hours <= 1.35
        )
        valid_segments = bool(
            leg.get("split_verified") is True
            and chain_valid
            and totals_consistent
        )
        if valid_segments:
            continue
        start = _clean_text(leg.get("from"), 100) or f"第{index}段起点"
        end = _clean_text(leg.get("to"), 100) or f"第{index}段终点"
        detail = _clean_text(leg.get("split_validation_issue"), 240)
        add(
            severity="critical",
            category="driving_feasibility",
            locations=(start, end),
            diagnosis=(
                f"长途段「{start} → {end}」没有通过逐段实测校验"
                + (f"：{detail}" if detail else "")
            ),
            repair_instruction="重新获取真实驾车折线并逐段测距，禁止用机械平均里程发布",
            action="route_replan",
        )

    days = (day_plan or {}).get("days")
    if isinstance(days, list):
        for day in days:
            if not isinstance(day, Mapping) or day.get("kind") != "transfer":
                continue
            try:
                number = int(day.get("day"))
                km = float(day.get("km") or 0)
                hours = float(day.get("hours") or 0)
            except (TypeError, ValueError):
                continue
            unverified_split = bool(
                day.get("long_leg_group")
                and (
                    day.get("measured") is not True
                    or day.get("estimated_split") is True
                    or day.get("split_verified") is not True
                    or hours <= 0
                )
            )
            if km <= 800 and hours <= 10 and not unverified_split:
                continue
            add(
                severity="critical",
                category="driving_feasibility",
                locations=(f"Day {number}",),
                diagnosis=(
                    f"锁定 day_plan 的 Day {number} 长途分段未经真实地图验证"
                    if unverified_split
                    else f"锁定 day_plan 的 Day {number} 超过单日 800km/10h 驾驶上限"
                ),
                repair_instruction="先拆分并实测该长途路段，再生成报告正文",
                action="route_replan",
            )

    return issues


def _section_ranges(content: str) -> dict[str, str]:
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", content, re.MULTILINE))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        title = re.sub(r"[*_#]", "", match.group(1)).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        body = content[match.end():end].strip()
        for key, aliases in _REQUIRED_SECTIONS:
            if any(alias in title for alias in aliases):
                sections.setdefault(key, body)
                break
    return sections


def _expected_days(query: str, day_plan: Mapping[str, Any] | None) -> list[int]:
    days = (day_plan or {}).get("days")
    if isinstance(days, list) and days:
        result = []
        for index, day in enumerate(days, 1):
            try:
                result.append(int(day.get("day", index)))
            except (AttributeError, TypeError, ValueError):
                result.append(index)
        return result
    match = _QUERY_DAYS_RE.search(query or "")
    if not match:
        return []
    total = int(match.group(1))
    return list(range(1, total + 1)) if 1 <= total <= 60 else []


def _day_sections(content: str) -> dict[int, str]:
    matches = list(_DAY_HEADING_RE.finditer(content))
    result: dict[int, str] = {}
    for index, match in enumerate(matches):
        boundaries = [
            matches[index + 1].start() if index + 1 < len(matches) else len(content)
        ]
        next_top_section = re.search(r"^##\s+", content[match.end():], re.MULTILINE)
        if next_top_section:
            boundaries.append(match.end() + next_top_section.start())
        end = min(boundaries)
        try:
            number = int(match.group(1))
        except ValueError:
            continue
        result[number] = content[match.start():end].strip()
    return result


def _day_headings(content: str) -> dict[int, str]:
    result: dict[int, str] = {}
    for number, heading in _DAY_HEADING_RE.findall(content):
        try:
            result[int(number)] = heading.strip()
        except ValueError:
            continue
    return result


def _numeric_match(pattern: re.Pattern[str], text: str) -> float | None:
    match = pattern.search(text or "")
    if not match:
        return None
    try:
        return float(match.group("value").replace(",", ""))
    except (TypeError, ValueError):
        return None


def _place_matches_text(text: str, place: str) -> bool:
    try:
        from services.route_planner import _place_matches
        return _place_matches(text, place)
    except (ImportError, AttributeError, TypeError):
        compact_text = re.sub(r"[\s·/（）()，,。:：;；\-—]+", "", text or "")
        compact_place = re.sub(r"[\s·/（）()，,。:：;；\-—]+", "", place or "")
        return bool(compact_place) and compact_place in compact_text


def _excerpt(text: str, match: re.Match[str] | None = None, radius: int = 80) -> str:
    if match is None:
        return text.strip()[: radius * 2]
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _clause_prefix(text: str, position: int, max_chars: int = 40) -> str:
    """取匹配前同一短句，避免把上一分句的“禁止”错误扩展到下一分句。"""
    start = max(0, position - max_chars)
    fragment = text[start:position]
    boundary = max(fragment.rfind(mark) for mark in "，,。；;！？!?：:\n|")
    return fragment[boundary + 1:]


def _is_anti_downgrade_reminder(text: str, match: re.Match[str]) -> bool:
    """“禁止只路过”是在强化必游要求，不是把景点降级。"""
    prefix = _clause_prefix(text, match.start())
    directives = list(_ANTI_DOWNGRADE_DIRECTIVE_RE.finditer(prefix))
    if not directives:
        return False
    trailing = prefix[directives[-1].end():]
    # “不得不远眺”不是禁止远眺，而是被迫远眺。
    if trailing.lstrip().startswith("不"):
        return False
    # “不要进入景区，只在外围远眺”中的不要修饰“进入”，不能豁免后面的降级。
    if re.search(r"(?:进入|入园|参观|游览)", trailing):
        return False
    return len(trailing) <= 16


def _is_official_closure_fallback(
    text: str,
    match: re.Match[str],
    *,
    has_real_visit: bool,
    has_overnight: bool,
) -> bool:
    """主方案已入园住宿时，官方闭园/管制下的安全备用不算主动降级。"""
    if not (has_real_visit and has_overnight):
        return False
    context = text[max(0, match.start() - 80):min(len(text), match.end() + 80)]
    return bool(
        _CONDITIONAL_FALLBACK_RE.search(context)
        and _OFFICIAL_CLOSURE_RE.search(context)
        and not _TIME_SAVING_DOWNGRADE_RE.search(context)
    )


def _find_forbidden_pass_through(
    text: str,
    *,
    has_real_visit: bool,
    has_overnight: bool,
) -> re.Match[str] | None:
    """返回真正把必游点降级的措辞，忽略反降级提醒与不可抗力备用。"""
    for match in _FORBIDDEN_PASS_THROUGH_RE.finditer(text):
        if _is_anti_downgrade_reminder(text, match):
            continue
        if _is_official_closure_fallback(
            text,
            match,
            has_real_visit=has_real_visit,
            has_overnight=has_overnight,
        ):
            continue
        # 主方案已明确入园并住宿时，单独“远眺雪峰/群山/日落”等附加观景
        # 不等于把必游目标降级。若同句含不入园或时间不足，仍然拦截。
        context = text[max(0, match.start() - 50):min(len(text), match.end() + 50)]
        matched = match.group(0)
        if (
            has_real_visit
            and has_overnight
            and matched.startswith("远眺")
            and not re.search(r"(?:不进入|不进景区|不入园)", context)
            and not _TIME_SAVING_DOWNGRADE_RE.search(context)
        ):
            continue
        return match
    return None


def _strip_visit_requirement_reminders(text: str, requirement: str) -> str:
    """移除提示性硬约束，避免把“必须入园/禁止路过”当作实际行程证据。"""
    normalized_requirement = _normalize_evidence(requirement)
    kept: list[str] = []
    for line in text.splitlines():
        normalized_line = _normalize_evidence(line)
        is_labelled_reminder = bool(re.search(
            r"(?:必游|游览|路线).{0,6}(?:硬约束|硬性要求)|(?:硬约束|硬性要求).{0,6}(?:必游|游览)",
            line,
        ))
        repeats_requirement = bool(
            len(normalized_requirement) >= 8
            and normalized_requirement in normalized_line
        )
        if is_labelled_reminder or repeats_requirement:
            continue
        kept.append(line)
    return "\n".join(kept)


def audit_report(
    content: str,
    query: str = "",
    day_plan: Mapping[str, Any] | None = None,
    route: Mapping[str, Any] | None = None,
) -> list[ReviewIssue]:
    """执行不依赖模型的专业版硬校验，返回可直接合并的程序问题。"""
    text = content or ""
    issues: list[ReviewIssue] = []

    def add(**kwargs: Any) -> None:
        issues.append(_program_issue(len(issues) + 1, **kwargs))

    if not text.strip():
        add(
            severity="critical", category="incomplete_report",
            diagnosis="报告正文为空",
            repair_instruction="重新生成完整报告后再进入审核",
        )
        return issues

    if not re.search(r"^#\s+\S", text, re.MULTILINE):
        add(
            severity="major", category="incomplete_report",
            diagnosis="缺少一级报告标题",
            repair_instruction="以完整的一级旅行攻略标题开头",
        )

    sections = _section_ranges(text)
    for key, aliases in _REQUIRED_SECTIONS:
        if key not in sections or not sections[key].strip():
            title = aliases[0]
            add(
                severity="critical" if key in {"itinerary", "budget", "knowledge"} else "major",
                category="missing_section",
                locations=(title,),
                diagnosis=f"专业版报告缺少有效的「{title}」板块",
                repair_instruction=f"补齐「{title}」板块及其实际内容",
            )

    expected = _expected_days(query, day_plan)
    heading_numbers = [int(item) for item in re.findall(
        r"^#{1,4}\s*Day\s*(\d+)\b", text, re.MULTILINE | re.IGNORECASE,
    )]
    if not heading_numbers:
        add(
            severity="critical", category="missing_day",
            diagnosis="报告没有任何独立 Day 小节",
            repair_instruction="按天逐节输出完整分日行程",
        )
    else:
        duplicates = sorted({day for day in heading_numbers if heading_numbers.count(day) > 1})
        if duplicates:
            add(
                severity="critical", category="missing_day",
                locations=tuple(f"Day {day}" for day in duplicates),
                evidence=f"Day {duplicates[0]}",
                diagnosis="报告包含重复 Day 编号",
                repair_instruction="每个 Day 只能出现一个分日行程小节",
            )
        if expected:
            actual = set(heading_numbers)
            missing = [day for day in expected if day not in actual]
            extras = [day for day in heading_numbers if day not in set(expected)]
            if missing or extras:
                details = []
                if missing:
                    details.append("缺少 " + "、".join(f"Day {day}" for day in missing))
                if extras:
                    details.append("多出 " + "、".join(f"Day {day}" for day in extras))
                add(
                    severity="critical", category="missing_day",
                    locations=tuple(f"Day {day}" for day in [*missing, *extras]),
                    diagnosis="；".join(details),
                    repair_instruction="严格按预定总天数逐日重写，不合并、不遗漏 Day",
                )

    knowledge = sections.get("knowledge", "")
    if knowledge and expected:
        knowledge_days = {
            int(item) for item in re.findall(r"\bDay\s*(\d+)\b", knowledge, re.IGNORECASE)
        }
        missing = [day for day in expected if day not in knowledge_days]
        if missing:
            add(
                severity="major", category="knowledge_graph",
                locations=("行程知识图谱",),
                diagnosis="知识图谱缺少 " + "、".join(f"Day {day}" for day in missing),
                repair_instruction="让知识图谱覆盖全部 Day，且每天独占一行",
            )
        graph_lines = [line for line in knowledge.splitlines() if re.search(r"\bDay\s*\d+\b", line, re.I)]
        if any(len(re.findall(r"\bDay\s*\d+\b", line, re.I)) > 1 for line in graph_lines):
            evidence = next(line.strip() for line in graph_lines if len(re.findall(r"\bDay\s*\d+\b", line, re.I)) > 1)
            add(
                severity="major", category="knowledge_graph",
                locations=("行程知识图谱",), evidence=evidence,
                diagnosis="知识图谱把多个 Day 合并在同一行",
                repair_instruction="每个 Day 单独输出一行树状节点",
            )

    if text.count("```") % 2:
        add(
            severity="major", category="incomplete_report",
            evidence="```", diagnosis="Markdown 代码块没有闭合，报告可能被截断",
            repair_instruction="补完被截断内容并闭合代码块",
        )

    # 复用路线规划器对 Day 标题、锁定城市和每个 Day 独立表格的校验。
    if day_plan and isinstance(day_plan.get("days"), list) and day_plan.get("days"):
        try:
            from services.route_planner import validate_day_sequence
            sequence_ok, reason = validate_day_sequence(text, dict(day_plan))
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            sequence_ok, reason = False, f"锁定骨架校验异常：{exc}"
        if not sequence_ok:
            add(
                severity="critical", category="route_consistency",
                diagnosis=reason,
                repair_instruction="严格按照锁定 day_plan 重写对应 Day，不改路线、顺序、里程或时长",
            )

        overview = _clean_text(day_plan.get("overview"), 5000)
        first_section = re.search(r"^##\s+", text, re.MULTILINE)
        preamble = text[:first_section.start()] if first_section else text
        if overview and _normalize_evidence(overview) not in _normalize_evidence(preamble):
            add(
                severity="critical", category="route_consistency",
                locations=("路线总览",),
                diagnosis="报告没有原样采用程序锁定的路线总览",
                repair_instruction=f"路线总览必须原样写为：{overview}",
            )

        headings = _day_headings(text)
        for day in day_plan.get("days") or []:
            if not isinstance(day, Mapping) or day.get("kind") != "transfer":
                continue
            try:
                number = int(day.get("day"))
            except (TypeError, ValueError):
                continue
            heading = headings.get(number, "")
            if not heading:
                continue
            expected_km = day.get("km")
            actual_km = _numeric_match(_KM_RE, heading)
            try:
                km_mismatch = expected_km is not None and (
                    actual_km is None
                    or abs(actual_km - float(expected_km)) > 0.6
                )
            except (TypeError, ValueError):
                km_mismatch = False
            expected_hours = day.get("hours")
            actual_hours = _numeric_match(_HOURS_RE, heading)
            try:
                hours_mismatch = expected_hours is not None and (
                    actual_hours is None
                    or abs(actual_hours - float(expected_hours)) > 0.11
                )
            except (TypeError, ValueError):
                hours_mismatch = False
            if km_mismatch or hours_mismatch:
                add(
                    severity="critical", category="route_consistency",
                    locations=(f"Day {number}",), evidence=heading,
                    diagnosis=f"Day {number} 的里程或驾驶时长与锁定 day_plan 不一致",
                    repair_instruction="原样采用锁定 day_plan 的里程和驾驶时长",
                )

            try:
                unsafe_km = float(expected_km or 0) > 800
                unsafe_hours = float(expected_hours or 0) > 10
            except (TypeError, ValueError):
                unsafe_km = unsafe_hours = False
            if unsafe_km or unsafe_hours:
                add(
                    severity="critical", category="driving_feasibility",
                    locations=(f"Day {number}",),
                    diagnosis=f"锁定 day_plan 的 Day {number} 超过单日 800km/10h 驾驶上限",
                    repair_instruction="先把该路段拆成多个真实驾驶日并重建 day_plan",
                    action="route_replan",
                )

    route = route or {}

    # 路线正文可以百分之百服从一条错误骨架，因此必须独立检查地图定位阶段
    # 留下的 requested -> resolved 证据。任何未解析、行政区不符或 POI 类型
    # 不适合作为旅行目的地的问题，都只能重建路线，不能靠改写正文掩盖。
    location_validation_issues = route.get("location_validation_issues")
    if isinstance(location_validation_issues, list):
        for item in location_validation_issues:
            if not isinstance(item, Mapping):
                continue
            requested = _clean_text(item.get("requested"), 200) or "未知地点"
            reason = _clean_text(item.get("reason"), 300) or "地图定位未通过校验"
            add(
                severity="critical",
                category="location_mismatch",
                locations=(requested,),
                diagnosis=f"路线节点「{requested}」定位校验失败：{reason}",
                repair_instruction="重新定位并重建路线骨架，禁止沿用当前坐标或让正文模型猜测",
                action="route_replan",
            )

    location_resolutions = route.get("location_resolutions")
    route_scope = tuple(route.get("scope_provinces") or ())
    from services.route_planner import _query_directly_mentions_location
    if isinstance(location_resolutions, list):
        for item in location_resolutions:
            if not isinstance(item, Mapping):
                continue
            admin_invalid = item.get("admin_match") is False
            type_invalid = item.get("type_valid") is False
            scope_invalid = bool(
                (item.get("role") or "destination") == "destination"
                and route_scope
                and not _query_directly_mentions_location(
                    query,
                    _clean_text(item.get("requested"), 200),
                )
                and item.get("scope_match") is not True
            )
            if not (admin_invalid or type_invalid or scope_invalid):
                continue
            requested = _clean_text(item.get("requested"), 200) or "未知地点"
            resolved = _clean_text(item.get("resolved_name"), 200) or "未知地图结果"
            resolved_area = "".join(
                _clean_text(item.get(key), 80)
                for key in ("province", "city", "district")
            )
            reasons = []
            if admin_invalid:
                reasons.append("行政区与请求名称不一致")
            if type_invalid:
                poi_type = _clean_text(item.get("poi_type"), 120)
                reasons.append(f"POI 类型不适合作为旅行目的地{f'（{poi_type}）' if poi_type else ''}")
            if scope_invalid:
                reasons.append(f"模型推荐地点超出用户目标省域（{'、'.join(route_scope)}）")
            add(
                severity="critical",
                category="location_mismatch",
                locations=(requested,),
                evidence=f"{requested} -> {resolved_area}{resolved}",
                diagnosis=f"路线节点「{requested}」被解析为「{resolved_area}{resolved}」：{'；'.join(reasons)}",
                repair_instruction="淘汰错误候选，使用行政区和类型均匹配的地图结果后重建路线",
                action="route_replan",
            )

    seq_names = route.get("seq_names")
    if isinstance(seq_names, list):
        for place in dict.fromkeys(str(item).strip() for item in seq_names if str(item).strip()):
            if not _place_matches_text(text, place):
                add(
                    severity="critical", category="route_consistency",
                    locations=(place,),
                    diagnosis=f"锁定路线节点「{place}」没有出现在报告中",
                    repair_instruction=f"在保持锁定顺序的前提下补回「{place}」",
                )
        # day_plan 可能为超长路段插入安全过夜点，其 overview 比原始
        # route.seq_names 更具体且是最终权威；仅在没有 day_plan 时检查原始顺序。
        if not (day_plan or {}).get("overview"):
            expected_overview = " → ".join(
                str(item).strip() for item in seq_names if str(item).strip()
            )
            first_section = re.search(r"^##\s+", text, re.MULTILINE)
            preamble = text[:first_section.start()] if first_section else text
            if (
                len(seq_names) >= 2
                and expected_overview
                and _normalize_evidence(expected_overview) not in _normalize_evidence(preamble)
            ):
                add(
                    severity="critical", category="route_order",
                    locations=("路线总览",),
                    diagnosis="报告路线总览没有按锁定途经顺序完整呈现",
                    repair_instruction=f"路线总览必须按以下顺序：{expected_overview}",
                )

    legs = route.get("legs")
    if isinstance(legs, list):
        for leg in legs:
            if not isinstance(leg, Mapping):
                continue
            corridor = _clean_text(leg.get("corridor"), 100)
            if corridor and corridor not in text:
                add(
                    severity="critical", category="requirement_mismatch",
                    locations=(str(leg.get("from") or ""), str(leg.get("to") or "")),
                    diagnosis=f"锁定景观道路「{corridor}」没有在报告中落实",
                    repair_instruction=f"在对应 Day 明确按「{corridor}」行驶，并保留实时通行复核提示",
                )
            for via in leg.get("via") or []:
                via_name = str(via).strip()
                if via_name and not _place_matches_text(text, via_name):
                    add(
                        severity="major", category="requirement_mismatch",
                        locations=(via_name,),
                        diagnosis=f"锁定途经节点「{via_name}」没有在报告中落实",
                        repair_instruction=f"在对应 Day 的路线与时段表中补回「{via_name}」",
                    )

    # 必游要求同时验证 day_plan 本身是否留出停留日，以及正文是否把景点降级成路过。
    visit_requirements = route.get("visit_requirements")
    minimum_stays = route.get("min_stay_days")
    if isinstance(visit_requirements, Mapping):
        day_items = (day_plan or {}).get("days") or []
        day_bodies = _day_sections(text)
        for place, requirement in visit_requirements.items():
            place = str(place).strip()
            if not place:
                continue
            related_days: list[int] = []
            stay_days: list[int] = []
            stay_count = 0
            for day in day_items:
                if not isinstance(day, Mapping):
                    continue
                target = day.get("at") if day.get("kind") == "stay" else day.get("to")
                if target and _place_matches_text(str(target), place):
                    try:
                        day_number = int(day.get("day"))
                        related_days.append(day_number)
                        if day.get("kind") == "stay":
                            stay_days.append(day_number)
                    except (TypeError, ValueError):
                        pass
                    if day.get("kind") == "stay":
                        stay_count += 1
            required_stay = 0
            if isinstance(minimum_stays, Mapping):
                try:
                    required_stay = int(minimum_stays.get(place, 0) or 0)
                except (TypeError, ValueError):
                    required_stay = 0
            if required_stay and stay_count < required_stay:
                add(
                    severity="critical", category="must_visit",
                    locations=(place,),
                    diagnosis=f"锁定日程只给「{place}」安排 {stay_count} 个停留日，低于必需的 {required_stay} 天",
                    repair_instruction="先重新规划 day_plan，不能靠正文虚构停留时间",
                    action="route_replan",
                )

            # 有强制停留日时，必游活动应主要落在 stay Day；抵达日可能仅办理
            # 入住或观赏沿途其他景观，不能因其中出现“远眺”就误判目标景点。
            visit_days = stay_days if required_stay and stay_days else related_days
            visit_scoped = "\n".join(day_bodies.get(day, "") for day in visit_days) or text
            overnight_scoped = "\n".join(
                day_bodies.get(day, "") for day in related_days
            ) or text
            visit_scoped = _strip_visit_requirement_reminders(
                visit_scoped, str(requirement)
            )
            overnight_scoped = _strip_visit_requirement_reminders(
                overnight_scoped, str(requirement)
            )
            # 锁定标题本身会包含“深度游/休整”，不能把标题当成已安排游览的证据。
            body_only = re.sub(
                r"^#{1,4}\s*Day[^\n]*$", "", visit_scoped,
                flags=re.MULTILINE | re.IGNORECASE,
            )
            overnight_body = re.sub(
                r"^#{1,4}\s*Day[^\n]*$", "", overnight_scoped,
                flags=re.MULTILINE | re.IGNORECASE,
            )
            has_real_visit = bool(_VISIT_ACTIVITY_RE.search(body_only))
            has_overnight = bool(_OVERNIGHT_RE.search(overnight_body))
            forbidden = _find_forbidden_pass_through(
                visit_scoped,
                has_real_visit=has_real_visit,
                has_overnight=has_overnight,
            )
            if forbidden:
                add(
                    severity="critical", category="must_visit",
                    locations=(place,), evidence=_excerpt(visit_scoped, forbidden),
                    diagnosis=f"报告把必游地点「{place}」降级成路过、远眺或不入园",
                    repair_instruction=str(requirement) or f"为「{place}」安排实质游览",
                )
            else:
                requires_visit = bool(re.search(r"进入|入园|游览|参观", str(requirement)))
                requires_overnight = "住宿" in str(requirement) or "过夜" in str(requirement)
                missing_visit = requires_visit and not has_real_visit
                missing_overnight = requires_overnight and not has_overnight
                if not requires_visit and not requires_overnight:
                    missing_visit = not (has_real_visit or has_overnight)
                if not (missing_visit or missing_overnight):
                    continue
                add(
                    severity="major", category="must_visit",
                    locations=(place,),
                    diagnosis=(
                        f"报告没有明确落实「{place}」的"
                        + ("实质游览和住宿安排" if missing_visit and missing_overnight
                           else "实质游览安排" if missing_visit else "住宿安排")
                    ),
                    repair_instruction=str(requirement) or f"为「{place}」安排实质游览",
                )

    return issues


def _issues_from(value: ReviewResult | Iterable[ReviewIssue]) -> list[ReviewIssue]:
    if isinstance(value, ReviewResult):
        return list(value.issues)
    return list(value)


def needs_rewrite(value: ReviewResult | Iterable[ReviewIssue]) -> bool:
    """是否需要调用模型重写；路线骨架问题由 ``route_replan`` 单独处理。"""
    return any(
        issue.severity in {"critical", "major"}
        and issue.suggested_action in {"rewrite", "program_fix"}
        for issue in _issues_from(value)
    )


def requires_route_replan(value: ReviewResult | Iterable[ReviewIssue]) -> bool:
    return any(
        issue.severity in {"critical", "major"}
        and issue.suggested_action == "route_replan"
        for issue in _issues_from(value)
    )


def decide_review_action(value: ReviewResult | Iterable[ReviewIssue]) -> str:
    """返回 ``route_replan`` / ``rewrite`` / ``manual_verify`` / ``publish``。"""
    issues = _issues_from(value)
    if requires_route_replan(issues):
        return "route_replan"
    if needs_rewrite(issues):
        return "rewrite"
    if any(
        issue.severity in {"critical", "major"}
        and issue.suggested_action == "manual_verify"
        for issue in issues
    ):
        return "manual_verify"
    return "publish"


def merge_review_results(
    program_issues: Iterable[ReviewIssue],
    model_result: ReviewResult,
) -> ReviewResult:
    """合并程序与模型结果，并按类别、位置、证据去重。"""
    merged: list[ReviewIssue] = []
    seen: set[tuple[Any, ...]] = set()
    for issue in [*program_issues, *model_result.issues]:
        key = (
            issue.category,
            tuple(item.casefold() for item in issue.locations),
            _normalize_evidence(issue.evidence).casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(issue)
    return ReviewResult(
        verdict=_derived_verdict(merged),
        highest_severity=_highest_severity(merged),
        summary=model_result.summary,
        issues=merged,
        valid=model_result.valid,
        parse_error=model_result.parse_error,
        rejected_count=model_result.rejected_count,
        raw_issue_count=model_result.raw_issue_count,
    )


def _limited_json(value: Any, max_chars: int = 18_000) -> str:
    try:
        serialized = json.dumps(value or {}, ensure_ascii=False, default=str, indent=2)
    except (TypeError, ValueError):
        serialized = json.dumps(str(value), ensure_ascii=False)
    if len(serialized) <= max_chars:
        return serialized
    return serialized[:max_chars].rsplit("\n", 1)[0] + "\n…（实时数据已截断）"


def _issues_json(issues: Iterable[ReviewIssue]) -> str:
    return json.dumps(
        [issue.to_dict() for issue in issues],
        ensure_ascii=False,
        indent=2,
    )


def build_review_messages(
    query: str,
    draft: str,
    travel_data: Mapping[str, Any] | None = None,
    day_plan: Mapping[str, Any] | None = None,
    program_issues: Iterable[ReviewIssue] = (),
    route: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    """构建独立审核会话；只诊断，不允许审核模型顺手改写正文。"""
    categories = ", ".join(sorted(ALLOWED_CATEGORIES))
    system = f"""你是旅行攻略的独立质量审核员。你面对的是已生成的隐藏初稿，只负责找出可由原文证实的问题，不得改写报告。

重点检查：明显折返、锁定路线与逐日安排冲突、用户点名道路/景点遗漏、必游景点被降级成路过、Day/板块不完整、驾驶与游览时间不可执行、预算/住宿/路线前后矛盾。不要凭空核验实时事实；缺少证据时不要报问题。

只输出一个 JSON 对象，不要代码块或解释。schema：
{{"schema_version":"1.0","verdict":"pass|repair","highest_severity":"none|minor|major|critical","summary":"一句话","issues":[{{"id":"I1","severity":"critical|major|minor","category":"白名单类别","locations":["Day 1"],"evidence":"必须逐字摘自报告的连续短句","violated_constraint":"违反的需求或约束","diagnosis":"为什么不合理","repair_instruction":"具体修复指令","confidence":0.0,"suggested_action":"rewrite|route_replan|program_fix|manual_verify|none"}}]}}

类别白名单：{categories}
规则：
1. evidence 必须是报告原文中可搜索到的连续文本，禁止概括或编造；
2. 只有高置信度硬伤才标 major/critical，措辞润色只能标 minor；
3. 只有程序锁定的 day_plan 本身形成明显折返、超过驾驶上限、容不下必游停留或漏掉用户硬约束时才用 route_replan；报告正文、时段表或住宿没有严格遵循已锁定骨架时必须用 rewrite，不能用 route_replan；
4. 实时票价、开放状态等无法由现有数据确认时用 manual_verify；
5. 已被程序问题清单覆盖的同一问题无需重复；没有新问题时 issues=[]；
6. Day 标题中出现的途经点已经算明确落实，不能声称“未明确为途经点”；
7. 景点安排 2-3 小时本身不构成 major。只有报告内的时间明确重叠、总时长算术不成立，或违反锁定硬约束时，才报 timing_feasibility；
8. 程序预检为空只表示正文结构与当前锁定骨架一致，不代表骨架的地图定位必然正确。必须结合用户原始需求和路线定位摘要，检查锁定 day_plan 本身是否出现目的地区域越界、显示地名与实际行政区不一致或明显无意义折返；不得因为“已锁定”而忽略这类硬伤；
9. 需要整篇重写的 major/critical 必须会实质改变路线可执行性、用户硬需求或内部一致性。仅增加一句说明、调整措辞或延长普通景点停留时间的建议应标 minor 或省略。"""
    system += (
        "\n10. 大型景区、湖区和自然保护区可能横跨多个行政区，地名不同本身不能证明底层路线骨架错误，"
        "更不能据此使用 route_replan；但正文若改用另一个入口、绕到其他地区住宿，且未证明仍符合"
        "锁定方向、里程和时长，应报告 route_consistency 并使用 rewrite，不能直接忽略。"
    )
    user = f"""【用户原始需求】
{query}

【程序锁定 day_plan】
{_limited_json(day_plan or {}, 12_000)}

【程序路线与地图定位摘要】
{_limited_json({
    "seq_names": (route or {}).get("seq_names"),
    "legs": (route or {}).get("legs"),
    "location_resolutions": (route or {}).get("location_resolutions"),
    "location_validation_issues": (route or {}).get("location_validation_issues"),
}, 14_000)}

【实时数据摘要】
{_limited_json(travel_data or {}, 18_000)}

【程序预检已发现的问题】
{_issues_json(program_issues)}

【待审核的隐藏初稿】
{draft}"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_repair_messages(
    query: str,
    draft: str,
    travel_data: Mapping[str, Any] | None,
    day_plan: Mapping[str, Any] | None,
    approved_issues: Iterable[ReviewIssue],
) -> list[dict[str, str]]:
    """构建一次性完整修复请求，只允许处理已经批准的问题。"""
    issues = list(approved_issues)
    system = """你是旅行攻略终稿修复器。请基于初稿输出一份完整、可直接发布的 Markdown 终稿，从一级标题开始，不写任何解释、审稿记录或代码块包裹。

必须修复审核清单中的所有问题，同时保留初稿中正确、具体、有价值的内容。程序锁定的 day_plan、路线总览、每天城市顺序、里程、时长以及实时数据是硬约束，禁止擅自修改；若审核项要求 route_replan，不得猜测新路线，也不得伪装成已修好。每个 Day 必须独立成节并有完整时段表，所有专业版板块与覆盖全部 Day 的知识图谱必须保留。不得加入审核清单之外的新事实或虚构实时信息。"""
    user = f"""【用户原始需求】
{query}

【程序锁定 day_plan · 不可改动】
{_limited_json(day_plan or {}, 14_000)}

【实时数据摘要】
{_limited_json(travel_data or {}, 18_000)}

【必须修复的已批准问题】
{_issues_json(issues)}

【隐藏初稿】
{draft}

请输出修复后的完整终稿。"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


__all__ = [
    "ALLOWED_ACTIONS",
    "ALLOWED_CATEGORIES",
    "ALLOWED_SEVERITIES",
    "MIN_LLM_ISSUE_CONFIDENCE",
    "ReviewIssue",
    "ReviewResult",
    "audit_report",
    "audit_route_plan",
    "build_repair_messages",
    "build_review_messages",
    "decide_review_action",
    "merge_review_results",
    "needs_rewrite",
    "parse_review_result",
    "requires_route_replan",
]
