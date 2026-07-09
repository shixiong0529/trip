"""
高德 Web 服务数据客户端

仅用于旅行攻略的信息补充：地理编码、POI、天气、驾车距离。
不处理酒店预订、机票预订或任何交易能力。
"""

import asyncio
import os
from typing import Any

import httpx
from dotenv import load_dotenv


AMAP_BASE_URL = "https://restapi.amap.com/v3"

load_dotenv()


async def collect_amap_reference(destination: str, origin: str | None = None) -> str:
    """采集目的地周边位置数据，失败时返回空串。"""
    key = os.getenv("AMAP_WEB_SERVICE_KEY", "").strip()
    destination = (destination or "").strip()
    origin = (origin or "").strip() or None
    if not key or not destination:
        return ""

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            dest_geo = await _geocode(client, key, destination)
            if not dest_geo:
                return ""

            location = dest_geo.get("location") or ""
            adcode = dest_geo.get("adcode") or ""
            city = dest_geo.get("city") or destination

            route_coro = _driving_route(client, key, origin, destination) if origin else _empty_route()
            hotels_coro = _search_pois(client, key, city, "100000", 5)
            restaurants_coro = _around_pois(client, key, location, "050000", 5)
            scenic_coro = _search_pois(client, key, city, "110000|060000", 6)
            weather_coro = _weather(client, key, adcode)

            hotels, restaurants, scenic, weather, route = await asyncio.gather(
                hotels_coro,
                restaurants_coro,
                scenic_coro,
                weather_coro,
                route_coro,
            )

        return format_amap_summary({
            "destination": destination,
            "geocode": dest_geo,
            "hotels": hotels,
            "restaurants": restaurants,
            "scenic": scenic,
            "weather": weather,
            "route": route,
        })
    except Exception:
        return ""


async def _request(client: httpx.AsyncClient, key: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
    payload = {**params, "key": key, "output": "json"}
    resp = await client.get(f"{AMAP_BASE_URL}{path}", params=payload)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "1":
        return {}
    return data


async def _geocode(client: httpx.AsyncClient, key: str, address: str) -> dict[str, Any]:
    data = await _request(client, key, "/geocode/geo", {"address": address})
    geocodes = data.get("geocodes") or []
    return geocodes[0] if geocodes else {}


async def _search_pois(
    client: httpx.AsyncClient,
    key: str,
    city: str,
    types: str,
    limit: int,
) -> list[dict[str, Any]]:
    data = await _request(client, key, "/place/text", {
        "types": types,
        "city": city,
        "citylimit": "true",
        "offset": limit,
        "page": 1,
        "extensions": "all",
    })
    return (data.get("pois") or [])[:limit]


async def _around_pois(
    client: httpx.AsyncClient,
    key: str,
    location: str,
    types: str,
    limit: int,
) -> list[dict[str, Any]]:
    if not location:
        return []
    data = await _request(client, key, "/place/around", {
        "location": location,
        "types": types,
        "radius": 3000,
        "offset": limit,
        "page": 1,
        "extensions": "all",
    })
    return (data.get("pois") or [])[:limit]


async def _weather(client: httpx.AsyncClient, key: str, adcode: str) -> dict[str, Any]:
    if not adcode:
        return {}
    data = await _request(client, key, "/weather/weatherInfo", {
        "city": adcode,
        "extensions": "all",
    })
    forecasts = data.get("forecasts") or []
    return forecasts[0] if forecasts else {}


async def _driving_route(
    client: httpx.AsyncClient,
    key: str,
    origin: str | None,
    destination: str,
) -> dict[str, Any]:
    if not origin:
        return {}
    origin_geo = await _geocode(client, key, origin)
    dest_geo = await _geocode(client, key, destination)
    origin_loc = origin_geo.get("location")
    dest_loc = dest_geo.get("location")
    if not origin_loc or not dest_loc:
        return {}
    data = await _request(client, key, "/direction/driving", {
        "origin": origin_loc,
        "destination": dest_loc,
        "extensions": "base",
    })
    paths = (data.get("route") or {}).get("paths") or []
    if not paths:
        return {}
    path = paths[0]
    return {
        "origin": origin,
        "distance": path.get("distance", ""),
        "duration": path.get("duration", ""),
        "tolls": path.get("tolls", ""),
        "traffic_lights": path.get("traffic_lights", ""),
    }


async def _empty_route() -> dict[str, Any]:
    return {}


def format_amap_summary(data: dict[str, Any]) -> str:
    """把高德 JSON 摘要成可注入 LLM 的 Markdown。"""
    destination = data.get("destination") or "目的地"
    geocode = data.get("geocode") or {}
    lines = [f"### 高德地图参考数据 · {destination}"]

    address = geocode.get("formatted_address")
    location = geocode.get("location")
    adcode = geocode.get("adcode")
    if address or location:
        parts = [p for p in [address, f"坐标 {location}" if location else "", f"adcode {adcode}" if adcode else ""] if p]
        lines.append(f"- 目的地定位：{' | '.join(parts)}")

    route = data.get("route") or {}
    if route.get("origin") and route.get("distance") and route.get("duration"):
        km = _format_km(route.get("distance"))
        minutes = _format_minutes(route.get("duration"))
        lines.append(f"- 驾车距离参考：{route['origin']} → {destination}约 {km} / {minutes}")

    weather = data.get("weather") or {}
    casts = weather.get("casts") or []
    if casts:
        lines.append(f"- 天气预报（{weather.get('city', destination)}，{weather.get('reporttime', '')}）：")
        for cast in casts[:4]:
            lines.append(
                f"  - {cast.get('date')} {cast.get('dayweather')}/{cast.get('nightweather')} "
                f"{cast.get('nighttemp')}-{cast.get('daytemp')}°C"
            )

    _append_poi_section(lines, "酒店 POI（位置/评分参考，非实时房价库存）", data.get("hotels") or [])
    _append_poi_section(lines, "餐饮 POI（周边与人均参考）", data.get("restaurants") or [])
    _append_poi_section(lines, "景点/商圈 POI（位置与评分参考）", data.get("scenic") or [])

    return "\n".join(lines).strip()


def _append_poi_section(lines: list[str], title: str, pois: list[dict[str, Any]]) -> None:
    if not pois:
        return
    lines.append(f"\n#### {title}")
    for poi in pois[:6]:
        biz = poi.get("biz_ext") or {}
        details = []
        if poi.get("address"):
            details.append(str(poi["address"]))
        rating = biz.get("rating")
        if rating not in (None, "", []):
            details.append(f"评分 {rating}")
        cost = biz.get("cost")
        if cost not in (None, "", []):
            details.append(f"人均约 ¥{_trim_price(cost)}")
        lowest_price = biz.get("lowest_price")
        if lowest_price not in (None, "", []):
            details.append(f"参考低价 ¥{_trim_price(lowest_price)}")
        open_time = biz.get("open_time") or biz.get("opentime2")
        if open_time not in (None, "", []):
            details.append(f"营业 {open_time}")
        if poi.get("distance"):
            details.append(f"距定位点 {poi['distance']}m")
        if poi.get("tel") not in (None, "", []):
            details.append(f"电话 {poi['tel']}")
        suffix = "；".join(details)
        lines.append(f"- {poi.get('name', '未命名地点')}：{suffix}" if suffix else f"- {poi.get('name', '未命名地点')}")


def _format_km(meters: str) -> str:
    try:
        return f"{float(meters) / 1000:.1f}km"
    except (TypeError, ValueError):
        return f"{meters}m"


def _format_minutes(seconds: str) -> str:
    try:
        return f"{round(float(seconds) / 60)}分钟"
    except (TypeError, ValueError):
        return f"{seconds}秒"


def _trim_price(value: Any) -> str:
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return str(value)
