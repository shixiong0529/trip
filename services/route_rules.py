"""可扩展的自驾路线语义规则。

距离矩阵只能回答“两个点之间导航多远”，不能表达景观公路、季节性道路、
必须回到门户城市的支线，以及某个景点至少需要多少停留时间。本模块把这些
语义约束声明成数据，由 route_planner 的通用排序/可行性引擎统一执行。

新增其他区域规则时，只需扩展下方规则表，不需要在正文提示词里写死行程。
"""

from __future__ import annotations

from datetime import date
import re
from typing import Any, Iterable, Optional


# 独库公路是首个接入的景观走廊规则。节点顺序来自新疆官方“独库公路风景道”
# 定义；参考里程/时长只在地图接口因当前时段管制返回数百公里绕行时使用，并且
# 会明确标为“路线参考、出发前复核”，绝不冒充高德实时实测值。
SCENIC_CORRIDORS: tuple[dict[str, Any], ...] = (
    {
        "name": "G217独库公路南段",
        "region_hints": ("新疆",),
        "active_months": frozenset({6, 7, 8, 9}),
        "nodes": (
            {
                "aliases": ("那拉提",),
                "canonical": "新源县那拉提旅游风景区",
            },
            {
                "aliases": ("巴音布鲁克",),
                "canonical": "和静县巴音布鲁克草原",
                "min_stay_days": 1,
                "visit_requirement": (
                    "必须进入巴音布鲁克景区安排实质游览并在巴音布鲁克镇住宿，"
                    "禁止只路过、远眺或只作短暂休息"
                ),
            },
            {
                "aliases": ("库车大峡谷", "天山神秘大峡谷"),
                "canonical": "库车市天山神秘大峡谷",
            },
        ),
        # key 是 nodes 中的相邻下标。此段夜间/季节封路时，高德实时距离接口
        # 会返回经库尔勒绕行的 700km+ 结果；这里采用保守参考值，并保留复核提示。
        "leg_overrides": {
            (1, 2): {
                "km": 280.0,
                "hours": 7.5,
                "via": ["G217独库公路南段", "库车大小龙池"],
                "measured": False,
            },
        },
        "notice": (
            "G217独库公路为季节性且可能分时段管制的景观道路；"
            "须在出发前按新疆交通/交警最新公告复核开放时段、车型限制和天气，"
            "若关闭则不得宣称已走独库公路，必须改走官方绕行路线并重新核算天数"
        ),
    },
)


# “门户城市 → 支线目的地 → 门户城市”关系。通用规划器允许重复 gateway，
# 从而不再受“每个 POI 只能访问一次”的 TSP 数据结构限制。
GATEWAY_EXCURSIONS: tuple[dict[str, Any], ...] = (
    {
        "region_hints": ("新疆",),
        "gateway_aliases": ("喀什",),
        "destination_aliases": ("塔什库尔干", "塔县", "帕米尔"),
        "label": "帕米尔高原支线须经喀什往返",
    },
)


STOP_VISIT_RULES: tuple[dict[str, Any], ...] = (
    {
        "region_hints": ("新疆",),
        "aliases": ("巴音布鲁克",),
        "min_stay_days": 1,
        "visit_requirement": (
            "必须进入巴音布鲁克景区安排实质游览并在巴音布鲁克镇住宿，"
            "禁止只路过、远眺或只作短暂休息"
        ),
    },
)


def _contains_any(text: str, aliases: Iterable[str]) -> bool:
    return any(alias and alias in (text or "") for alias in aliases)


def _query_month(query: str, today: Optional[date] = None) -> int:
    """提取出行月份；没有日期时按当前月份规划近期出行。

    这里只用于季节性道路的候选判断。最终报告仍必须要求出发前核实实时路况。
    """
    text = query or ""
    matches = re.findall(r"(?<!\d)(1[0-2]|0?[1-9])\s*月", text)
    if matches:
        return int(matches[0])
    iso = re.search(r"\b\d{4}[-/.](1[0-2]|0?[1-9])(?:[-/.]\d{1,2})?\b", text)
    if iso:
        return int(iso.group(1))
    if "国庆" in text:
        return 10
    for hints, month in (
        (("冬季", "冬天", "寒假"), 1),
        (("春季", "春天"), 4),
        (("夏季", "夏天", "暑假"), 7),
        (("秋季", "秋天"), 9),
    ):
        if _contains_any(text, hints):
            return month
    return (today or date.today()).month


def _find_index(
    names: list[str], aliases: Iterable[str], active_indices: Optional[set[int]] = None,
) -> Optional[int]:
    for index, name in enumerate(names):
        if active_indices is not None and index not in active_indices:
            continue
        if _contains_any(name, aliases):
            return index
    return None


def canonicalize_corridor_stops(query: str, stops: list[str]) -> list[str]:
    """纠正已识别景观走廊节点的含混/错误行政前缀，并保持原顺序。

    只有 query 命中对应区域且 stop 自身命中别名时才替换；不会给无关行程
    擅自增加景点。这样诸如“新源县巴音布鲁克”不会把 POI 搜索带到那拉提。
    """
    result = list(stops)
    for rule in SCENIC_CORRIDORS:
        if not _contains_any(query, rule["region_hints"]):
            continue
        for node in rule["nodes"]:
            for index, stop in enumerate(result):
                if _contains_any(stop, node["aliases"]):
                    result[index] = node["canonical"]
    deduped = []
    for stop in result:
        if stop not in deduped:
            deduped.append(stop)
    return deduped


def resolve_route_rules(
    query: str,
    names: list[str],
    active_indices: Optional[Iterable[int]] = None,
    today: Optional[date] = None,
) -> dict[str, Any]:
    """把声明式规则解析为当前坐标矩阵可执行的索引约束。"""
    active = set(active_indices) if active_indices is not None else set(range(len(names)))
    ordered_chains: list[list[int]] = []
    leg_overrides: dict[tuple[int, int], dict[str, Any]] = {}
    gateway_excursions: list[dict[str, Any]] = []
    min_stay_days: dict[int, int] = {}
    visit_requirements: dict[int, str] = {}
    protected_indices: set[int] = set()
    notes: list[str] = []
    blocking_issues: list[str] = []
    month = _query_month(query, today=today)

    for rule in STOP_VISIT_RULES:
        if not _contains_any(query, rule["region_hints"]):
            continue
        index = _find_index(names, rule["aliases"], active)
        if index is None:
            continue
        min_stay_days[index] = max(
            min_stay_days.get(index, 0), int(rule.get("min_stay_days") or 0)
        )
        visit_requirements[index] = rule["visit_requirement"]
        protected_indices.add(index)

    for rule in SCENIC_CORRIDORS:
        if not _contains_any(query, rule["region_hints"]):
            continue
        node_indices = [
            _find_index(names, node["aliases"], active)
            for node in rule["nodes"]
        ]
        if any(index is None for index in node_indices):
            continue
        indexes = [int(index) for index in node_indices if index is not None]
        explicitly_requested = rule["name"] in query or "独库公路" in query
        if month not in rule["active_months"]:
            if explicitly_requested:
                blocking_issues.append(
                    f"出行月份为 {month} 月，{rule['name']}通常不在常规开放季，"
                    "不能把该道路锁进路线；请调整日期或接受绕行"
                )
            continue

        ordered_chains.append(indexes)
        protected_indices.update(indexes)
        notes.append(rule["notice"])
        for node_index, node in zip(indexes, rule["nodes"]):
            if node.get("min_stay_days"):
                min_stay_days[node_index] = max(
                    min_stay_days.get(node_index, 0), int(node["min_stay_days"])
                )
            if node.get("visit_requirement"):
                visit_requirements[node_index] = node["visit_requirement"]
        for (left_pos, right_pos), override in rule.get("leg_overrides", {}).items():
            left, right = indexes[left_pos], indexes[right_pos]
            leg_overrides[(left, right)] = {
                **override,
                "corridor": rule["name"],
                "notice": rule["notice"],
            }

    for rule in GATEWAY_EXCURSIONS:
        if not _contains_any(query, rule["region_hints"]):
            continue
        gateway = _find_index(names, rule["gateway_aliases"], active)
        destination = _find_index(names, rule["destination_aliases"], active)
        if gateway is None or destination is None or gateway == destination:
            continue
        gateway_excursions.append({
            "gateway": gateway,
            "destination": destination,
            "label": rule["label"],
        })

    return {
        "ordered_chains": ordered_chains,
        "leg_overrides": leg_overrides,
        "gateway_excursions": gateway_excursions,
        "min_stay_days": min_stay_days,
        "visit_requirements": visit_requirements,
        "protected_indices": protected_indices,
        "notes": notes,
        "blocking_issues": blocking_issues,
        "month": month,
    }
