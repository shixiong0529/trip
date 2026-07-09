"""
高德静态路线图生成

从生成后的攻略 Markdown 中提取关键节点，使用高德 Web 服务解析坐标、
规划路线路径，并把静态地图图片转成 base64 嵌入 HTML 报告。
"""

import base64
import os
import re
from typing import Any

import httpx
from dotenv import load_dotenv


AMAP_BASE_URL = "https://restapi.amap.com/v3"
MAX_ROUTE_NODES = 10
MAX_ROUTE_POINTS = 80

load_dotenv()


def build_route_map_html(markdown_content: str) -> str:
    """生成报告开头的全程路线图 HTML。失败时返回空串，不影响报告输出。"""
    key = os.getenv("AMAP_WEB_SERVICE_KEY", "").strip()
    if not key:
        return ""

    names = extract_route_nodes(markdown_content)
    if len(names) < 2:
        return ""

    destination = _extract_destination(markdown_content)
    try:
        resolved = _resolve_nodes(key, destination, names[:MAX_ROUTE_NODES])
        if len(resolved) < 2:
            return ""
        route_points = _resolve_route_points(key, resolved)
        image_bytes = _fetch_static_map(key, resolved, route_points)
        if not image_bytes:
            return ""
    except Exception:
        return ""

    image_data = base64.b64encode(image_bytes).decode("ascii")
    legend = "".join(
        f'<span class="route-map-chip" title="{chr(65 + idx)} {_escape_html(node["name"])}">'
        f'<strong>{chr(65 + idx)}</strong> {_escape_html(node["name"])}</span>'
        for idx, node in enumerate(resolved)
    )
    return f"""<div class="route-map-card">
  <div class="route-map-header">
    <h2>🗺️ 全程路线图</h2>
    <p>基于高德地图真实底图、POI 坐标与驾车路径生成，关键节点按行程顺序标注。</p>
  </div>
  <img class="route-map-image" src="data:image/png;base64,{image_data}" alt="全程路线图">
  <div class="route-map-legend">{legend}</div>
</div>
"""


def extract_route_nodes(markdown_content: str) -> list[str]:
    """从分日行程表中提取加粗地点名，按出现顺序去重。"""
    nodes: list[str] = []
    seen = set()
    in_day_section = False

    for line in markdown_content.splitlines():
        if line.startswith("### Day"):
            in_day_section = True
            continue
        if line.startswith("## "):
            in_day_section = False
        if not in_day_section or "|" not in line:
            continue

        for match in re.finditer(r"\*\*([^*\n]{2,30})\*\*", line):
            name = _clean_node_name(match.group(1))
            if not _looks_like_place(name):
                continue
            if name not in seen:
                seen.add(name)
                nodes.append(name)
            if len(nodes) >= MAX_ROUTE_NODES:
                return nodes

    return nodes


def _resolve_nodes(key: str, destination: str, names: list[str]) -> list[dict[str, str]]:
    resolved = []
    with httpx.Client(timeout=20.0) as client:
        for name in names:
            node = _search_poi(client, key, destination, name) or _geocode(client, key, f"{destination}{name}")
            if node:
                resolved.append(node)
    return resolved


def _resolve_route_points(key: str, resolved: list[dict[str, str]]) -> list[str]:
    points: list[str] = []
    with httpx.Client(timeout=20.0) as client:
        for current, nxt in zip(resolved, resolved[1:]):
            segment = _driving_polyline(client, key, current["location"], nxt["location"])
            if not segment:
                segment = [current["location"], nxt["location"]]
            for point in segment:
                if not points or points[-1] != point:
                    points.append(point)
    return _downsample(points, MAX_ROUTE_POINTS)


def _fetch_static_map(key: str, resolved: list[dict[str, str]], route_points: list[str]) -> bytes:
    markers = "|".join(
        f"mid,0x2563eb,{chr(65 + idx)}:{node['location']}"
        for idx, node in enumerate(resolved[:MAX_ROUTE_NODES])
    )
    labels = "|".join(
        f"{_truncate_label(node['name'])},0,1,14,0xffffff,0x2563eb:{node['location']}"
        for node in resolved[:MAX_ROUTE_NODES]
    )
    paths = f"6,0x2563eb,0.85,,:{';'.join(route_points)}" if route_points else ""
    params = {
        "key": key,
        "size": "900*420",
        "scale": "2",
        "markers": markers,
        "labels": labels,
        "paths": paths,
    }
    with httpx.Client(timeout=20.0) as client:
        resp = client.get(f"{AMAP_BASE_URL}/staticmap", params=params)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "image" not in content_type:
            return b""
        return resp.content


def _search_poi(client: httpx.Client, key: str, city: str, name: str) -> dict[str, str]:
    data = _request(client, key, "/place/text", {
        "keywords": name,
        "city": city,
        "citylimit": "false",
        "offset": 1,
        "page": 1,
        "extensions": "base",
    })
    pois = data.get("pois") or []
    if not pois:
        return {}
    poi = pois[0]
    location = poi.get("location")
    if not location:
        return {}
    return {"name": poi.get("name") or name, "location": location}


def _geocode(client: httpx.Client, key: str, address: str) -> dict[str, str]:
    data = _request(client, key, "/geocode/geo", {"address": address})
    geocodes = data.get("geocodes") or []
    if not geocodes:
        return {}
    geocode = geocodes[0]
    location = geocode.get("location")
    if not location:
        return {}
    return {"name": geocode.get("formatted_address") or address, "location": location}


def _driving_polyline(client: httpx.Client, key: str, origin: str, destination: str) -> list[str]:
    data = _request(client, key, "/direction/driving", {
        "origin": origin,
        "destination": destination,
        "extensions": "base",
    })
    paths = (data.get("route") or {}).get("paths") or []
    if not paths:
        return []
    points: list[str] = []
    for step in paths[0].get("steps") or []:
        polyline = step.get("polyline") or ""
        for point in polyline.split(";"):
            if point and (not points or points[-1] != point):
                points.append(point)
    return points


def _request(client: httpx.Client, key: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
    payload = {**params, "key": key, "output": "json"}
    resp = client.get(f"{AMAP_BASE_URL}{path}", params=payload)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "1":
        return {}
    return data


def _extract_destination(markdown_content: str) -> str:
    first_line = markdown_content.splitlines()[0] if markdown_content.splitlines() else ""
    m = re.search(r"[\U0001f300-\U0001faff\ufe0f\s]*([一-龥]{2,6})", first_line)
    return m.group(1) if m else ""


def _clean_node_name(name: str) -> str:
    name = re.sub(r"[（(].*?[）)]", "", name)
    name = re.sub(r"^(直奔|前往|游览|夜游|午餐|晚餐|早餐|打卡)", "", name)
    return name.strip(" ：:，,。·-")


def _looks_like_place(name: str) -> bool:
    if len(name) < 2 or len(name) > 24:
        return False
    blocked = ("本日亮点", "本日预算", "免责声明", "省钱技巧", "应对方案", "总计", "门票")
    if "¥" in name or "￥" in name:
        return False
    return not any(word in name for word in blocked)


def _downsample(points: list[str], limit: int) -> list[str]:
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    sampled = [points[round(i * step)] for i in range(limit)]
    sampled[0] = points[0]
    sampled[-1] = points[-1]
    return sampled


def _truncate_label(text: str) -> str:
    return text[:15]


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
