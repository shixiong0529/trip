"""
路线骨架预规划器

多点自驾行程在生成攻略前，先把途经顺序用真实地理数据排定：
1. LLM 从用户需求中抽取出发地与途经点
2. 高德地理编码取得各点坐标
3. 高德批量测距得到两两驾车距离矩阵（山区路网与直线差异大，
   直线最短环线常把顺路点排成折返，故必须用实测驾车距离）
4. 在驾车距离矩阵上求最短环线（点少穷举，点多贪心+2-opt）

产出可注入提示词的「路线骨架」Markdown，从源头避免 Z 字形绕路
和"凑天数折返"。任何一步失败都返回空串，退化为纯 LLM 排线。
"""

import asyncio
import itertools
import json
import math
import os
import re
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

from services.amap_client import AMAP_BASE_URL, _geocode, _request

load_dotenv()

# 高德免费个人配额约 3 QPS，超限返回空结果：全局按最小间隔节流，
# 失败视作可能限流，退避后重试一次
_AMAP_MIN_INTERVAL = 0.35
_AMAP_RETRY_BACKOFF = 0.8
_amap_lock = asyncio.Lock()


async def _amap_paced(call):
    """节流执行一次高德请求；空结果时退避重试一次。call 为无参异步函数。"""
    result = None
    for attempt in range(2):
        async with _amap_lock:
            await asyncio.sleep(_AMAP_MIN_INTERVAL)
        try:
            result = await call()
        except Exception:
            result = None
        if result:
            return result
        if attempt == 0:
            await asyncio.sleep(_AMAP_RETRY_BACKOFF)
    return result
# 穷举排序的途经点数上限（8! = 40320，毫秒级；再多切换启发式）
_BRUTE_FORCE_LIMIT = 8

# 超过其一即视为"长途驾驶段"（仍在 800km/10h 安全上限内），
# 攻略中给出中途过夜拆分建议
_LONG_LEG_KM = 500
_LONG_LEG_HOURS = 6

# 高德 POI 名称里的状态标注，如「坐龙峡风景区(暂停开放)」
_POI_STATUS_RE = re.compile(r"[（(]([^（）()]*(?:暂停|关闭|停业|歇业|闭园|维护|装修)[^（）()]*)[)）]")


def _detect_poi_alerts(stop_names: list[str], poi_names: list[str]) -> list[dict[str, str]]:
    alerts = []
    for stop, poi in zip(stop_names, poi_names):
        m = _POI_STATUS_RE.search(poi or "")
        if m:
            alerts.append({"stop": stop, "status": m.group(1).strip()})
    return alerts


async def _regeo_city(
    client: httpx.AsyncClient, key: str, loc: tuple[float, float]
) -> Optional[str]:
    """坐标逆地理编码到「市+区县」，失败返回 None。"""
    data = await _amap_paced(lambda: _request(client, key, "/geocode/regeo", {
        "location": f"{loc[0]:.6f},{loc[1]:.6f}",
    }))
    comp = ((data or {}).get("regeocode") or {}).get("addressComponent") or {}
    city = comp.get("city")
    if not isinstance(city, str) or not city:
        city = comp.get("province") if isinstance(comp.get("province"), str) else ""
    district = comp.get("district") if isinstance(comp.get("district"), str) else ""
    return (city + district) or None

_EXTRACT_SYSTEM = (
    "你是旅行路线信息抽取器，只输出一个 JSON 对象，"
    "禁止输出任何其他文字、解释或代码块标记。"
)

_EXTRACT_TEMPLATE = """从下面的旅行需求中抽取自驾途经点，输出 JSON：
{{"origin": "出发地", "stops": ["途经点1", "途经点2"], "user_fixed_order": false, "round_trip": true}}

- stops 是需要前往游玩的具体景点/目的地列表（不含出发地），每项写成「县市名+景点名」（如 "龙山县八面山"、"古丈县坐龙峡"），便于精确定位到景点本身而不是县城
- 只有相距很近（同一景区、<20km）的景点才合并为一项；同县但相距较远的景点必须各自单独列出
- 用户明确指定了游览顺序时 user_fixed_order 为 true，stops 按用户顺序排列
- round_trip 默认为 true（自驾默认回出发地取/还车）；只有用户明确表示单程、异地还车、或以其他城市为终点时才为 false
- 提取不到出发地时 origin 为 null

旅行需求：{query}"""


async def plan_route(query: str, llm) -> Optional[dict]:
    """规划多点自驾路线，返回结构化结果；不适用或失败时返回 None。

    返回 dict:
        seq_names: 有序途经点（含出发地，环线含返程回到出发地）
        legs:      [{from,to,km,hours,measured}...]，长度 = len(seq_names)-1
        failed:    未能定位、未纳入排序的点
        round_trip: 是否回到出发地
        markdown:  可直接注入的「路线骨架」表格

    llm 需提供 chat_stream(messages) 异步生成器（orchestrator.LLMClient）。
    """
    try:
        return await _plan(query, llm)
    except Exception:
        return None


async def plan_route_skeleton(query: str, llm) -> str:
    """兼容旧接口：只取路线骨架 Markdown，失败返回空串。"""
    route = await plan_route(query, llm)
    return route["markdown"] if route else ""


async def _plan(query: str, llm) -> Optional[dict]:
    key = os.getenv("AMAP_WEB_SERVICE_KEY", "").strip()
    if not key:
        return ""

    extracted = await _extract_stops(query, llm)
    if not extracted:
        return ""
    origin = (extracted.get("origin") or "").strip()
    stops = [s.strip() for s in extracted.get("stops") or [] if s and s.strip()]
    if not origin or len(stops) < 2:
        # 单目的地不存在排序问题，交给既有的高德参考数据即可
        return None
    user_fixed_order = bool(extracted.get("user_fixed_order"))
    round_trip = extracted.get("round_trip", True) is not False

    async with httpx.AsyncClient(timeout=20.0) as client:
        names = [origin] + stops
        located = await _geocode_all(client, key, names)
        if located[0] is None:
            # 出发地都定位不到，骨架无从谈起
            return None

        located_names, located_coords, poi_names, failed = [], [], [], []
        for name, item in zip(names, located):
            if item:
                located_names.append(name)
                located_coords.append(item["coord"])
                poi_names.append(item.get("poi_name") or "")
            else:
                failed.append(name)
        # 除出发地外至少要有 2 个可定位途经点才有排序价值
        if len(located_coords) < 3:
            return None

        # 高德 POI 名里的状态标注（如「坐龙峡风景区(暂停开放)」）提出来，
        # 让攻略主动提醒核实，而不是当它不存在
        alerts = _detect_poi_alerts(located_names[1:], poi_names[1:])

        # 用真实驾车距离矩阵排序：山区路网与直线距离差异很大，直线最短
        # 环线常常不是驾车最短环线（会把顺路点排成折返）。矩阵拿不到时
        # 退回直线估算。
        dist_km, dur_h, measured = await _driving_matrix(client, key, located_coords)

        if user_fixed_order:
            order = list(range(1, len(located_coords)))
        else:
            order = _best_order(dist_km, round_trip)

        seq = [0] + order + ([0] if round_trip else [])
        seq_names = [located_names[i] for i in seq]
        legs = [
            {
                "from": located_names[a],
                "to": located_names[b],
                "km": dist_km[a][b],
                "hours": dur_h[a][b],
                "measured": measured[a][b],
            }
            for a, b in zip(seq, seq[1:])
        ]

        # 长途驾驶段（在安全上限内但很累）标注中途落脚点：
        # 取该段直线中点逆地理编码到城市，供攻略给出"拆一晚"建议
        for leg, (a, b) in zip(legs, zip(seq, seq[1:])):
            if leg["km"] > _LONG_LEG_KM or (leg["hours"] or 0) > _LONG_LEG_HOURS:
                ca, cb = located_coords[a], located_coords[b]
                mid = ((ca[0] + cb[0]) / 2, (ca[1] + cb[1]) / 2)
                leg["split_hint"] = await _regeo_city(client, key, mid)

    return {
        "seq_names": seq_names,
        "legs": legs,
        "failed": failed,
        "alerts": alerts,
        "round_trip": round_trip,
        "markdown": _format_skeleton(seq_names, legs, failed, user_fixed_order, alerts),
    }


async def _extract_stops(query: str, llm) -> Optional[dict]:
    messages = [
        {"role": "system", "content": _EXTRACT_SYSTEM},
        {"role": "user", "content": _EXTRACT_TEMPLATE.format(query=query)},
    ]
    text = ""
    async for chunk in llm.chat_stream(messages):
        text += chunk
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _parse_location(location: str) -> Optional[tuple[float, float]]:
    parts = (location or "").split(",")
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


async def _geocode_all(
    client: httpx.AsyncClient, key: str, names: list[str], poi_from_index: int = 1
) -> list[Optional[dict[str, Any]]]:
    """逐个定位，返回 {"coord": (lng,lat), "poi_name": str|None} 或 None。

    景点（index >= poi_from_index）优先走 POI 搜索——地理编码只认行政地址，
    "古丈县坐龙峡"会落到古丈县城坐标；而坐龙峡实际在县境最北端、紧贴去
    龙山的高速走廊，用县城坐标排序会把顺路点排成折返。出发地是城市名，
    直接地理编码更稳。两种方式互为回退。poi_name 保留高德官方名称，
    其中可能带「(暂停开放)」等状态标注，供上层提取提醒。"""

    async def poi_search(name: str) -> Optional[dict[str, Any]]:
        data = await _amap_paced(lambda: _request(client, key, "/place/text", {
            "keywords": name,
            "offset": 1,
            "page": 1,
        }))
        pois = (data or {}).get("pois") or []
        if pois:
            coord = _parse_location(pois[0].get("location") or "")
            if coord:
                return {"coord": coord, "poi_name": pois[0].get("name") or None}
        return None

    async def geocode(name: str) -> Optional[dict[str, Any]]:
        geo = await _amap_paced(lambda: _geocode(client, key, name))
        coord = _parse_location((geo or {}).get("location") or "")
        return {"coord": coord, "poi_name": None} if coord else None

    async def one(i: int, name: str) -> Optional[dict[str, Any]]:
        primary, fallback = (poi_search, geocode) if i >= poi_from_index else (geocode, poi_search)
        return await primary(name) or await fallback(name)

    return list(await asyncio.gather(*[one(i, n) for i, n in enumerate(names)]))


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    """球面距离（km）；坐标为 (经度, 纬度)。"""
    lng1, lat1, lng2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    )
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def _route_cost(dist: list[list[float]], order: list[int], round_trip: bool) -> float:
    cost = dist[0][order[0]]
    for a, b in zip(order, order[1:]):
        cost += dist[a][b]
    if round_trip:
        cost += dist[order[-1]][0]
    return cost


def _best_order(dist: list[list[float]], round_trip: bool) -> list[int]:
    """求从 0 号点（出发地）出发遍历所有途经点的最短顺序。"""
    idxs = list(range(1, len(dist)))
    if len(idxs) <= _BRUTE_FORCE_LIMIT:
        return list(min(
            itertools.permutations(idxs),
            key=lambda p: _route_cost(dist, list(p), round_trip),
        ))

    # 贪心最近邻起步
    order, remaining, cur = [], set(idxs), 0
    while remaining:
        nxt = min(remaining, key=lambda i: dist[cur][i])
        order.append(nxt)
        remaining.discard(nxt)
        cur = nxt
    # 2-opt 反转改进
    improved = True
    while improved:
        improved = False
        for i in range(len(order) - 1):
            for j in range(i + 1, len(order)):
                candidate = order[:i] + order[i:j + 1][::-1] + order[j + 1:]
                if _route_cost(dist, candidate, round_trip) < _route_cost(dist, order, round_trip):
                    order = candidate
                    improved = True
    return order


async def _driving_matrix(
    client: httpx.AsyncClient,
    key: str,
    coords: list[tuple[float, float]],
) -> tuple[list[list[float]], list[list[Optional[float]]], list[list[bool]]]:
    """全点两两驾车距离矩阵（km）、耗时矩阵（h）、是否实测标记。

    用高德 /distance 批量接口：一次调用一个终点、其余点作为起点，取回一整列，
    n 个点只需 n 次请求。任一格取不到时退化为球面距离 ×1.4，耗时置 None。
    """
    n = len(coords)
    dist = [[0.0] * n for _ in range(n)]
    dur: list[list[Optional[float]]] = [[0.0 if i == j else None for j in range(n)] for i in range(n)]
    measured = [[i == j for j in range(n)] for i in range(n)]

    origin_str = "|".join(f"{c[0]:.6f},{c[1]:.6f}" for c in coords)

    async def column(j: int) -> None:
        async def call():
            resp = await client.get(f"{AMAP_BASE_URL}/distance", params={
                "origins": origin_str,
                "destination": f"{coords[j][0]:.6f},{coords[j][1]:.6f}",
                "type": 1,  # 1=驾车
                "key": key,
                "output": "json",
            })
            resp.raise_for_status()
            data = resp.json()
            return data if data.get("status") == "1" else None

        data = await _amap_paced(call)
        results = (data or {}).get("results") or []
        by_id = {}
        for item in results:
            try:
                by_id[int(item.get("origin_id"))] = item
            except (TypeError, ValueError):
                continue
        for i in range(n):
            if i == j:
                continue
            item = by_id.get(i + 1)  # origin_id 从 1 开始
            km = hrs = None
            if item:
                try:
                    km = float(item.get("distance", 0)) / 1000
                    hrs = float(item.get("duration", 0)) / 3600
                except (TypeError, ValueError):
                    km = hrs = None
            if km and km > 0:
                dist[i][j], dur[i][j], measured[i][j] = km, hrs, True
            else:
                dist[i][j], dur[i][j], measured[i][j] = _haversine(coords[i], coords[j]) * 1.4, None, False

    # 逐列查询；_amap_paced 内部已全局节流，无需再并发
    for j in range(n):
        await column(j)
    return dist, dur, measured


def _format_skeleton(
    seq_names: list[str],
    legs: list[dict[str, Any]],
    failed: list[str],
    user_fixed_order: bool,
    alerts: Optional[list[dict[str, str]]] = None,
) -> str:
    source = "用户指定顺序" if user_fixed_order else "按高德实测距离计算的最短环线"
    lines = [f"### 路线骨架（{source}，顺序禁止更改）", ""]
    lines.append(" → ".join(seq_names))
    lines.append("")
    lines.append("| 段 | 路线 | 驾车距离 | 预估耗时 |")
    lines.append("|----|------|---------|---------|")
    total_km = 0.0
    for i, leg in enumerate(legs, 1):
        total_km += leg["km"]
        km_text = f"约 {leg['km']:.0f}km" + ("" if leg["measured"] else "（直线估算）")
        hours_text = f"约 {leg['hours']:.1f}h" if leg["hours"] is not None else "—"
        lines.append(f"| {i} | {leg['from']} → {leg['to']} | {km_text} | {hours_text} |")
    lines.append("")
    lines.append(f"全程总里程：约 {total_km:,.0f}km")
    if failed:
        lines.append(
            f"（以下途经点未能定位、不在上表中，请按地理位置就近插入相邻段：{'、'.join(failed)}）"
        )
    for leg in legs:
        if leg.get("split_hint"):
            lines.append(
                f"⚠️ {leg['from']} → {leg['to']} 为长途驾驶段（约 {leg['km']:.0f}km），"
                f"攻略须建议可在 {leg['split_hint']} 一带中途过夜拆分"
            )
    for alert in alerts or []:
        lines.append(
            f"⚠️ 高德当前标注「{alert['stop']}」状态为「{alert['status']}」，"
            f"攻略必须提醒出行前核实开放状态并给出附近备选"
        )
    return "\n".join(lines)


# ---------- 日程脚手架：把路线顺序 + 里程锁进分日标题，模型只填内容 ----------

_MAX_STAY_PER_STOP = 3        # 单个途经点最多附加的深度游/休整天数
_MAX_TOTAL_DAYS = 30          # 全程天数上限，防止模型给出离谱数字

_DAYPLAN_SYSTEM = (
    "你是行程天数分配器，只输出一个 JSON 对象，禁止输出任何其他文字或代码块标记。"
)

_DAYPLAN_TEMPLATE = """已按地图实测距离锁定自驾途经顺序（不可更改）：
{stops_line}

请只决定"在每个途经点停留几天深度游/休整"，输出 JSON：
{{"stay_days": [{zeros}]}}

- stay_days 长度必须等于途经点数量 {n}，逐个对应上面的途经点
- 每个数字是"抵达当天之外额外增加的整天数"：0 表示当天到、次日就走；1-3 表示多住几晚深度游
- 参考用户的节奏偏好与天数要求：{query}
- 节奏慵懒/时间充裕就多给热门大景点 1-2 天；行程紧凑就多给 0
- 只输出 JSON，数字为整数"""


async def build_day_plan(query: str, route: dict, llm) -> Optional[dict]:
    """在锁定的路线顺序上分配每天，产出可注入的日程脚手架。

    返回 dict:
        overview:    路线总览一行（模型须原样采用）
        scaffold_md: 分日锁定骨架（每天城市+里程已定死，模型只填主题与表格）
        days:        结构化日程，供生成后校验
    失败时返回 None（此时退化为仅注入路线骨架）。
    """
    try:
        seq_names = route["seq_names"]
        legs = route["legs"]
        round_trip = route.get("round_trip", True)
    except (KeyError, TypeError):
        return None
    if not legs:
        return None

    # 真实途经点（环线不含最后返程回到的出发地）
    n_stops = len(legs) - 1 if round_trip else len(legs)
    if n_stops < 1:
        return None
    stop_names = [legs[i]["to"] for i in range(n_stops)]

    stay_days = await _allocate_stay_days(query, stop_names, llm)

    days, d = [], 1
    for i, leg in enumerate(legs):
        is_return = round_trip and i == len(legs) - 1
        days.append({
            "day": d, "kind": "transfer",
            "from": leg["from"], "to": leg["to"],
            "km": leg["km"], "hours": leg["hours"], "measured": leg["measured"],
            "is_return": is_return,
            "split_hint": leg.get("split_hint"),
        })
        d += 1
        if not is_return:
            for _ in range(stay_days[i]):
                days.append({"day": d, "kind": "stay", "at": leg["to"]})
                d += 1

    return {
        "overview": " → ".join(seq_names),
        "scaffold_md": _render_scaffold(days, route.get("alerts") or []),
        "days": days,
    }


async def _allocate_stay_days(query: str, stop_names: list[str], llm) -> list[int]:
    """让模型只决定各途经点的停留天数；失败或非法一律夹到合法范围。"""
    n = len(stop_names)
    fallback = [0] * n
    stops_line = " → ".join(f"{i+1}.{name}" for i, name in enumerate(stop_names))
    prompt = _DAYPLAN_TEMPLATE.format(
        stops_line=stops_line, zeros=", ".join(["0"] * n), n=n, query=query,
    )
    text = ""
    try:
        async for chunk in llm.chat_stream([
            {"role": "system", "content": _DAYPLAN_SYSTEM},
            {"role": "user", "content": prompt},
        ]):
            text += chunk
    except Exception:
        return fallback

    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return fallback
    try:
        raw = json.loads(text[start:end + 1]).get("stay_days")
    except (json.JSONDecodeError, AttributeError):
        return fallback
    if not isinstance(raw, list):
        return fallback

    result = []
    for i in range(n):
        try:
            v = int(raw[i]) if i < len(raw) else 0
        except (TypeError, ValueError):
            v = 0
        result.append(max(0, min(_MAX_STAY_PER_STOP, v)))
    # 全程天数封顶：从后往前削减停留，保证转移日不被挤掉
    total = len(stop_names) + 1 + sum(result)  # 转移段数 + 停留
    while total > _MAX_TOTAL_DAYS and any(result):
        for i in range(n - 1, -1, -1):
            if result[i] > 0:
                result[i] -= 1
                total -= 1
                break
    return result


_DAY_HEADER_RE = re.compile(r"^#{1,4}\s*Day\s*(\d+)\s*[·:\-—]\s*(.+)$", re.MULTILINE)


def _norm_city(name: str) -> str:
    """归一化地名，便于宽松比对：去空白，去掉常见行政后缀。"""
    name = name.strip()
    for suffix in ("土家族苗族自治州", "自治县", "自治州", "地区", "城区"):
        name = name.replace(suffix, "")
    return name.rstrip("市县区镇乡")


def _cities_match(a: str, b: str) -> bool:
    a, b = _norm_city(a), _norm_city(b)
    return bool(a) and bool(b) and (a in b or b in a)


def validate_day_sequence(markdown: str, day_plan: dict) -> tuple[bool, str]:
    """校验成稿的分日标题是否与锁定骨架逐天一致。

    返回 (是否通过, 不一致原因)。只比对天数、每天的城市转移/停留，
    不纠结主题名与文案。
    """
    expected = day_plan.get("days") or []
    headers = _DAY_HEADER_RE.findall(markdown)
    if len(headers) != len(expected):
        return False, f"天数不符：应为 {len(expected)} 天，实际 {len(headers)} 天"

    for (num, text), day in zip(headers, expected):
        # 取标题中含「→」或「深度游/休整」的那一段做地名比对
        seg = next(
            (p for p in text.split("·") if "→" in p or "深度游" in p or "休整" in p),
            text,
        )
        if day["kind"] == "transfer":
            if "→" not in seg:
                return False, f"Day {day['day']} 应为转移日 {day['from']}→{day['to']}，标题却无转移"
            left, right = [s.strip() for s in seg.split("→", 1)]
            right = right.split("·")[0].strip()
            if not (_cities_match(left, day["from"]) and _cities_match(right, day["to"])):
                return False, (
                    f"Day {day['day']} 城市不符：应为 {day['from']}→{day['to']}，"
                    f"实际 {left}→{right}"
                )
        else:  # stay
            if not _cities_match(seg, day["at"]):
                return False, f"Day {day['day']} 应为 {day['at']} 停留日，标题不符"
    return True, ""


def _render_scaffold(days: list[dict[str, Any]], alerts: Optional[list[dict[str, str]]] = None) -> str:
    first = next((d for d in days if d["kind"] == "transfer"), None)
    last = next((d for d in reversed(days) if d["kind"] == "transfer"), None)
    dir_lock = ""
    if first and last:
        dir_lock = (
            f" Day 1 必须是「{first['from']} → {first['to']}」，"
            f"最后一个转移日必须是「{last['to']}"
            + ("（返程回到出发地）" if last.get("is_return") else "")
            + f"」；禁止把整条线路反向遍历，禁止丢掉返程段。"
        )
    lines = [
        "【分日行程 · 锁定骨架】",
        "以下每一天的「城市/路线 · 里程 · 时长」由程序按地图实测距离锁定。"
        "你必须为每天补一个主题名，并原样照抄城市与里程，禁止改动顺序、城市、里程、时长，"
        "禁止增加或删除任何一天，禁止反向。" + dir_lock +
        "请为每一天输出形如 "
        "`### Day N · <你起的主题> · <下面锁定的城市与里程原样照抄>` 的小节标题。"
        "「↳」开头的行是对该天正文内容的附加要求，不要出现在标题里。",
        "",
    ]
    for day in days:
        if day["kind"] == "transfer":
            hours = f" · 约 {day['hours']:.1f}h" if day["hours"] is not None else ""
            est = "" if day["measured"] else "（直线估算）"
            tail = "（返程回到出发地）" if day.get("is_return") else ""
            lines.append(
                f"Day {day['day']} · 【主题待填】· {day['from']} → {day['to']} · "
                f"约 {day['km']:.0f}km{est}{hours}{tail}"
            )
            if day.get("split_hint"):
                lines.append(
                    f"　↳ 本段为全程长途驾驶日：该天提示列必须包含「每 2 小时进服务区休息」，"
                    f"并明确建议「不想赶路可在 {day['split_hint']} 一带中途过夜、行程加 1 天」"
                )
        else:
            lines.append(
                f"Day {day['day']} · 【主题待填】· {day['at']} 深度游/休整 · "
                f"0km（当地游览，无城际转移）"
            )
    for alert in alerts or []:
        lines.append(
            f"　⚠️ 「{alert['stop']}」当前被高德标注为「{alert['status']}」：必须在对应 Day 的"
            f"提示列和「避坑提示」板块中提醒读者出行前核实开放状态，并给出附近替代景点"
        )
    return "\n".join(lines)
