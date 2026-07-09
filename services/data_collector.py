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
    # 避免外部接口把总耗时变成串行相加
    labels, queries = zip(*tasks)
    query_many_coro = client.query_many(list(queries))

    extra_coros = []
    extra_labels = []
    if org and dest:
        extra_coros.append(_query_train_reference(org, dest))
        extra_labels.append("train")
    if dest:
        extra_coros.append(_query_amap_reference(dest, org))
        extra_labels.append("amap")

    gathered = await asyncio.gather(query_many_coro, *extra_coros)
    results = gathered[0]
    extras = {
        label: result if isinstance(result, str) else ""
        for label, result in zip(extra_labels, gathered[1:])
    }

    # 聚合结果
    data = {}
    for label, result in zip(labels, results):
        if isinstance(result, Exception):
            # 查询失败置空，让 LLM 按"无实时数据"处理，而不是把错误文本当数据
            data[label] = ""
        elif isinstance(result, str):
            data[label] = result
        else:
            data[label] = str(result)

    data["train"] = extras.get("train", "")
    data["amap"] = extras.get("amap", "")

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


def _extract_destination(query: str) -> Optional[str]:
    """简单提取目的地，提取失败返回 None"""
    patterns = [
        r"(?:从)?[一-龥]{2,4}出发(?:去|到|飞|自驾)?([一-龥]{2,4})(?:玩|旅游|旅行|游|度假|待|自驾|\d|$)",
        r"(?:去|到|在|飞|自驾)([一-龥]{2,4}?)(?:玩|旅游|旅行|游|度假|待|自驾|\d)",
        r"([一-龥]{2,4})\d*日游",
        r"([一-龥]{2,4})(?:旅游|旅行|亲子游|自由行|深度游|美食之旅|赏樱|环线)",
        r"(?:去|到|飞|自驾)([一-龥]{2,4})",
    ]
    for pat in patterns:
        m = re.search(pat, query)
        if m:
            return m.group(1)
    return None


def _extract_origin(query: str) -> Optional[str]:
    """简单提取出发地"""
    patterns = [
        r"从([一-龥]{2,4})[出发出到去]",
        r"([一-龥]{2,4})出发",
    ]
    for pat in patterns:
        m = re.search(pat, query)
        if m:
            return m.group(1)
    return None
