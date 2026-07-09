"""
航空气象服务
基于 FAA aviationweather.gov API，获取 METAR/TAF 数据
"""

import httpx

AVWX_URL = "https://aviationweather.gov/api/data"


async def get_metar(airport: str) -> str:
    """获取 METAR（例行天气报告）

    Args:
        airport: ICAO 四字机场代码（如 ZBAA=北京, ZGGG=广州, VHHH=香港）
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{AVWX_URL}/metar",
                params={"ids": airport.upper(), "format": "raw"}
            )
            resp.raise_for_status()
            raw = resp.text.strip()
    except Exception as e:
        return f"航空气象查询失败: {str(e)}"

    if not raw:
        return f"机场 {airport} 暂无 METAR 数据"

    return _parse_metar(raw, airport)


async def get_taf(airport: str) -> str:
    """获取 TAF（机场天气预报）"""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{AVWX_URL}/taf",
                params={"ids": airport.upper(), "format": "raw"}
            )
            resp.raise_for_status()
            raw = resp.text.strip()
    except Exception as e:
        return f"TAF 查询失败: {str(e)}"

    if not raw:
        return f"机场 {airport} 暂无 TAF 数据"

    return "\n".join([
        f"### {airport} 机场天气预报 (TAF)",
        f"",
        f"```",
        f"{raw}",
        f"```",
        f"",
        f"> TAF 为机场预报，时效通常 24-30 小时。",
    ])


def _parse_metar(raw: str, airport: str) -> str:
    """简单解析 METAR 报告"""
    lines = raw.strip().split("\n")
    metar = lines[0] if lines else raw

    return "\n".join([
        f"### {airport} 当前天气 (METAR)",
        f"",
        f"```",
        f"{metar}",
        f"```",
        f"",
        f"> METAR 为机场例行天气报告，每小时更新。包含风向风速、能见度、云量、温度露点等信息。",
    ])
