"""
实时航班追踪服务
基于 OpenSky Network REST API（免费，匿名 400次/天）
文档: https://openskynetwork.github.io/opensky-api/
"""

import time
import httpx
from typing import Optional

OPENSKY_URL = "https://opensky-network.org/api"

# /flights/arrival 接口 begin/end 为必填，且区间最长 7 天
_ARRIVAL_WINDOW_SECONDS = 2 * 86400  # 默认查最近 2 天


async def track_by_airport(airport: str, begin: int = 0, end: int = 0) -> str:
    """按机场追踪起降航班

    Args:
        airport: ICAO 机场代码（如 ZBAA=北京, ZGGG=广州）
        begin: 开始时间戳（0=now-2天）
        end: 结束时间戳（0=now）
    """
    now = int(time.time())
    params = {
        "airport": airport.upper(),
        "begin": begin or now - _ARRIVAL_WINDOW_SECONDS,
        "end": end or now,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{OPENSKY_URL}/flights/arrival", params=params)
            if resp.status_code == 404:
                # OpenSky 对无数据的机场返回 404
                return f"机场 {airport} 在查询时段内暂无航班数据"
            resp.raise_for_status()
            flights = resp.json()

        if not flights:
            return f"机场 {airport} 在查询时段内暂无航班数据"

        return _format_flights(flights[:15], airport)
    except Exception as e:
        return f"航班追踪失败: {str(e) or type(e).__name__}"


async def track_by_callsign(callsign: str) -> str:
    """按呼号追踪特定航班（OpenSky 不支持服务端按呼号过滤，取全量后本地匹配）"""
    target = callsign.strip().upper()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{OPENSKY_URL}/states/all")
            resp.raise_for_status()
            data = resp.json()

        states = data.get("states") or []
        matched = [
            s for s in states
            if s and len(s) > 1 and s[1] and s[1].strip().upper() == target
        ]
        if not matched:
            return f"未找到呼号 {callsign} 的航班（可能未在飞行中）"

        return _format_state(matched[0])
    except Exception as e:
        return f"航班追踪失败: {str(e) or type(e).__name__}"


def _format_flights(flights: list, airport: str) -> str:
    """格式化航班列表"""
    lines = [f"### {airport} 航班动态\n"]
    lines.append("| 呼号 | 出发地 | 到达时间 | 状态 |")
    lines.append("|------|--------|---------|------|")
    for f in flights:
        callsign = (f.get("callsign") or "N/A").strip() or "N/A"
        dep = f.get("estDepartureAirport") or "N/A"
        arr_time = _ts_to_str(f.get("lastSeen", 0))
        lines.append(f"| {callsign} | {dep} | {arr_time} | 已到达 |")
    return "\n".join(lines)


def _format_state(state: list) -> str:
    """格式化航班状态向量

    OpenSky 状态向量字段（按索引）:
      0 icao24, 1 callsign, 2 origin_country, 3 time_position, 4 last_contact,
      5 longitude, 6 latitude, 7 baro_altitude, 8 on_ground, 9 velocity,
      10 true_track, 11 vertical_rate, 12 sensors, 13 geo_altitude
    """
    if len(state) < 12:
        return "数据不完整"

    callsign = (state[1] or "N/A").strip()
    origin_country = state[2] or "N/A"
    time_pos = state[3]
    longitude, latitude = state[5], state[6]
    baro_altitude = state[7]
    on_ground = state[8]
    velocity = state[9]
    geo_altitude = state[13] if len(state) > 13 else None

    altitude = baro_altitude if baro_altitude is not None else geo_altitude
    position = (
        f"({latitude:.4f}, {longitude:.4f})"
        if latitude is not None and longitude is not None
        else "N/A"
    )

    on_ground_str = "地面" if on_ground else "飞行中"
    return "\n".join([
        f"**呼号**: {callsign}",
        f"**注册国**: {origin_country}",
        f"**位置**: {position}",
        f"**高度**: {altitude:.0f}m" if altitude is not None else "**高度**: N/A",
        f"**速度**: {velocity:.0f}m/s" if velocity is not None else "**速度**: N/A",
        f"**状态**: {on_ground_str}",
        f"**更新时间**: {_ts_to_str(time_pos)}",
    ])


def _ts_to_str(ts: Optional[int]) -> str:
    """时间戳转字符串"""
    if not ts:
        return "N/A"
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
