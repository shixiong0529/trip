"""
并行数据采集器
根据用户输入智能拆分查询 → 并行调用携程问道/12306/高德 → 聚合结果为结构化字典
"""

import asyncio
import re
from datetime import datetime, timedelta
from typing import Optional
from services.ctrip_client import get_ctrip_client


async def collect_travel_data(
    query: str,
    destination: Optional[str] = None,
    origin: Optional[str] = None,
    days: Optional[int] = None,
    travelers: Optional[int] = None,
    budget: Optional[int] = None,
    is_international: bool = False,
    on_progress=None,
    deadline: float = 45.0,
) -> dict[str, str]:
    """根据用户需求并行采集旅行数据

    Returns:
        {
            "transport": str,      # 机票/火车票 Markdown（携程问道）
            "hotels": str,         # 酒店推荐 Markdown
            "attractions": str,    # 景点门票 Markdown
            "tips": str,           # 实用贴士 Markdown
            "train": str,          # 12306 真实余票参考（origin/destination 均提取成功时才有）
            "amap": str,           # 高德位置、POI、天气、路线参考（配置 key 且有 destination 时才有）
        }
    """
    client = get_ctrip_client()

    # 尝试从 query 中提取关键信息
    dest = destination or _extract_destination(query)
    org = origin or _extract_origin(query)
    day_info = f"{days}天" if days else ""

    # 构建并行查询列表
    tasks = []

    if dest:
        # 1. 交通查询
        if org:
            transport_query = f"{org}到{dest}交通方式，机票价格和火车票信息"
        else:
            transport_query = f"到{dest}的交通方式和机票价格"
        hotel_query = f"{dest}{day_info}酒店推荐，不同价位选择"
        attraction_query = f"{dest}{day_info}热门景点门票价格和预约规则"
        tips_query = f"{dest}{day_info}旅游攻略，天气穿搭建议，注意事项，必吃美食"
    else:
        # 提取不到目的地时，直接用原始需求组织查询，避免出现"到目的地的交通方式"这类无意义问题
        transport_query = f"{query}，查询相关交通方式和机票火车票价格"
        hotel_query = f"{query}，推荐不同价位的酒店"
        attraction_query = f"{query}，热门景点门票价格和预约规则"
        tips_query = f"{query}，天气穿搭建议、注意事项、必吃美食"

    tasks.append(("transport", transport_query))
    tasks.append(("hotels", hotel_query))
    tasks.append(("attractions", attraction_query))
    tasks.append(("tips", tips_query))

    # 并行执行：携程问道四路查询 + 12306 + 高德参考数据一起并行，
    # 避免外部接口把总耗时变成串行相加。整体设截止时间（deadline 秒），
    # 到点未返回的数据源直接跳过（LLM 按"无实时数据"推算），保证生成
    # 不被单个慢数据源拖住。on_progress(msg) 用于向前端播报各源完成情况。
    labels, queries = zip(*tasks)

    def _notify(msg: str) -> None:
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass

    data = {label: "" for label in labels}
    data["train"] = ""
    data["amap"] = ""

    async def run_wendao() -> None:
        results = await client.query_many(list(queries))
        for label, result in zip(labels, results):
            # 查询失败置空，让 LLM 按"无实时数据"处理，而不是把错误文本当数据
            data[label] = result if isinstance(result, str) else ""
        _notify("携程问道 · 交通/酒店/景点/贴士数据就绪")

    async def run_train() -> None:
        result = await _query_train_reference(org, dest)
        data["train"] = result if isinstance(result, str) else ""
        _notify("12306 余票参考数据就绪")

    async def run_amap() -> None:
        result = await _query_amap_reference(dest, org)
        data["amap"] = result if isinstance(result, str) else ""
        _notify("高德位置与周边数据就绪")

    pending_tasks = [asyncio.create_task(run_wendao())]
    if org and dest:
        pending_tasks.append(asyncio.create_task(run_train()))
    if dest:
        pending_tasks.append(asyncio.create_task(run_amap()))

    done, still_pending = await asyncio.wait(pending_tasks, timeout=deadline)
    if still_pending:
        for task in still_pending:
            task.cancel()
        _notify("部分数据源超时，已跳过（相应板块将由 AI 推算）")

    return data


async def _query_train_reference(org: str, dest: str) -> str:
    """并行调用 12306 查询真实余票，作为交通数据的补充参考。

    参考日期取今天 + 7 天。查询失败（error dict / 异常）时返回空串，不抛出，
    避免阻塞其余四路携程问道查询。
    """
    from services.train_service import query_tickets, format_ticket_result

    # 12306 是国内服务，参考日期固定按中国时区算，避免部署在 UTC 服务器时差一天
    from zoneinfo import ZoneInfo
    ref_date = (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        result = await asyncio.to_thread(query_tickets, org, dest, ref_date)
        if not isinstance(result, dict) or result.get("error"):
            return ""
        text = format_ticket_result(result)
        if not text:
            return ""
        return f"（以下为 {ref_date} 的12306余票参考，仅供交通规划参考）\n{text}"
    except Exception:
        return ""


async def _query_amap_reference(dest: str, org: Optional[str]) -> str:
    """调用高德 Web 服务补充 POI/天气/路线数据，失败时返回空串。"""
    from services.amap_client import collect_amap_reference

    try:
        return await collect_amap_reference(dest, org)
    except Exception:
        return ""


# 命中这些词说明抓到的是需求描述而非地名（如"一份详细的旅游攻略"里的"份详细"）
_PLACE_STOPWORDS = ("详细", "攻略", "行程", "计划", "方案", "推荐", "什么", "哪里", "帮我", "出发")
_PLACE_PREFIXES = ("一个", "一份", "这个", "那个", "从", "去", "到")


def _clean_place(name: Optional[str]) -> Optional[str]:
    """清洗提取到的地名：剥掉助词前缀，剔除明显不是地名的捕获"""
    if not name:
        return None
    for prefix in _PLACE_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    name = name.strip("的")
    if len(name) < 2 or any(w in name for w in _PLACE_STOPWORDS):
        return None
    return name


def _extract_destination(query: str) -> Optional[str]:
    """简单提取目的地，提取失败返回 None"""
    patterns = [
        r"(?:从)?[一-龥]{2,4}出发(?:去|到|飞|自驾)?([一-龥]{2,4}?)(?:玩|旅游|旅行|游|度假|待|自驾|\d|$)",
        r"(?:去|到|在|飞|自驾)([一-龥]{2,4}?)(?:玩|旅游|旅行|游|度假|待|自驾|\d)",
        r"([一-龥]{2,4})\d*日游",
        r"([一-龥]{2,4})(?:旅游|旅行|亲子游|自由行|深度游|美食之旅|赏樱|环线)",
        r"(?:去|到|飞|自驾)([一-龥]{2,4})",
    ]
    for pat in patterns:
        m = re.search(pat, query)
        if m:
            place = _clean_place(m.group(1))
            if place:
                return place
    return None


def _extract_origin(query: str) -> Optional[str]:
    """简单提取出发地"""
    patterns = [
        r"从([一-龥]{2,4}?)(?:出发|[出到去])",
        r"([一-龥]{2,4}?)出发",
    ]
    for pat in patterns:
        m = re.search(pat, query)
        if m:
            place = _clean_place(m.group(1))
            if place:
                return place
    return None
