"""
并行数据采集器
根据用户输入智能拆分查询 → 并行调用携程问道 → 聚合结果为结构化字典
"""

import asyncio
import re
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
            "transport": str,      # 机票/火车票 Markdown
            "hotels": str,         # 酒店推荐 Markdown
            "attractions": str,    # 景点门票 Markdown
            "tips": str,           # 实用贴士 Markdown
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

    # 并行执行
    labels, queries = zip(*tasks)
    results = await client.query_many(list(queries))

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

    return data


def _extract_destination(query: str) -> Optional[str]:
    """简单提取目的地，提取失败返回 None"""
    patterns = [
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
