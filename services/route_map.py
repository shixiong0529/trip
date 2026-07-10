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


# 同一份 markdown 的路线图结果缓存（仅缓存成功结果）：
# /view、重复下载等场景不再重复发起 ~20 个高德请求
_route_map_cache: dict[int, str] = {}
_ROUTE_MAP_CACHE_MAX = 16


def build_route_map_html(markdown_content: str) -> str:
    """生成报告开头的全程路线图 HTML。失败时返回空串，不影响报告输出。"""
    key = os.getenv("AMAP_WEB_SERVICE_KEY", "").strip()
    if not key:
        return ""

    cache_key = hash(markdown_content)
    cached = _route_map_cache.get(cache_key)
    if cached:
        return cached

    names = extract_route_nodes(markdown_content)
    if len(names) < 2:
        return ""

    destination = _extract_destination(markdown_content)
    try:
        city = _resolve_city(key, destination)
        resolved = _resolve_nodes(key, city, names[:MAX_ROUTE_NODES])
        # 剔除跨省错解析的离群点，避免地图视野被拉到全国、密集路线糊成一团
        resolved = _drop_outliers(resolved)
        if len(resolved) < 2:
            return ""
        route_points = _resolve_route_points(key, resolved)
        image_bytes = _fetch_static_map(key, resolved, route_points)
        if not image_bytes:
            return ""
        # 行程跨度大时（如市区景点 + 远郊长城），单张图里密集簇会被压成一团，
        # 追加一张只含密集簇的"细节图"
        detail = _build_detail_map(key, resolved, route_points)
    except Exception:
        return ""

    image_data = base64.b64encode(image_bytes).decode("ascii")
    legend = "".join(
        f'<span class="route-map-chip" title="{chr(65 + idx)} {_escape_html(node["name"])}">'
        f'<strong>{chr(65 + idx)}</strong> {_escape_html(node["name"])}</span>'
        for idx, node in enumerate(resolved)
    )
    detail_html = ""
    if detail:
        detail_data = base64.b64encode(detail).decode("ascii")
        detail_html = (
            '\n  <p class="route-map-subtitle">🔍 密集区域细节（标注字母与上图一致）</p>'
            f'\n  <img class="route-map-image" src="data:image/png;base64,{detail_data}" alt="密集区域路线细节">'
        )
    html = f"""<div class="route-map-card">
  <div class="route-map-header">
    <h2>🗺️ 全程路线图</h2>
    <p>基于高德地图真实底图、POI 坐标与驾车路径生成，关键节点按行程顺序标注。</p>
  </div>
  <img class="route-map-image" src="data:image/png;base64,{image_data}" alt="全程路线图">{detail_html}
  <div class="route-map-legend">{legend}</div>
</div>
"""
    if len(_route_map_cache) >= _ROUTE_MAP_CACHE_MAX:
        _route_map_cache.clear()
    _route_map_cache[cache_key] = html
    return html


def _build_detail_map(key: str, resolved: list[dict[str, str]], route_points: list[str]) -> bytes:
    """行程整体跨度 > 60km 时，为密集簇（距中位中心 30km 内的点）生成细节图。

    细节图上的标注字母沿用全程图的原字母，图例可以对照；
    折线复用已算好的全程路径中落在簇范围内的点，不再发起新的路线请求。
    返回空 bytes 表示不需要或生成失败（不影响主图输出）。
    """
    if len(resolved) < 4:
        return b""

    def _to_xy(loc: str) -> tuple[float, float]:
        lng, lat = loc.split(",")
        return float(lng), float(lat)

    def _median(values: list[float]) -> float:
        s = sorted(values)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2

    def _dist_km(a: tuple[float, float], b: tuple[float, float]) -> float:
        return (((a[0] - b[0]) * 88) ** 2 + ((a[1] - b[1]) * 111) ** 2) ** 0.5

    points = [_to_xy(n["location"]) for n in resolved]
    # 整体跨度：bounding box 对角线
    lngs = [p[0] for p in points]
    lats = [p[1] for p in points]
    span_km = _dist_km((min(lngs), min(lats)), (max(lngs), max(lats)))
    if span_km < 60:
        return b""  # 本来就紧凑，单图已够清晰

    center = (_median(lngs), _median(lats))
    cluster_idx = [i for i, p in enumerate(points) if _dist_km(p, center) < 30]
    # 密集簇至少 3 个点、且确实只是行程的一部分时才值得出细节图
    if len(cluster_idx) < 3 or len(cluster_idx) == len(resolved):
        return b""

    cluster_nodes = [resolved[i] for i in cluster_idx]
    cluster_labels = [chr(65 + i) for i in cluster_idx]

    # 复用全程折线中落在簇 bbox（外扩约 5km）内的点
    c_pts = [points[i] for i in cluster_idx]
    pad = 0.06
    min_lng, max_lng = min(p[0] for p in c_pts) - pad, max(p[0] for p in c_pts) + pad
    min_lat, max_lat = min(p[1] for p in c_pts) - pad, max(p[1] for p in c_pts) + pad
    detail_points = []
    for loc in route_points:
        try:
            x, y = _to_xy(loc)
        except (ValueError, TypeError):
            continue
        if min_lng <= x <= max_lng and min_lat <= y <= max_lat:
            detail_points.append(loc)

    try:
        return _fetch_static_map(key, cluster_nodes, detail_points, labels=cluster_labels)
    except Exception:
        return b""


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


def _resolve_city(key: str, destination: str) -> str:
    """把标题里提取的目的地字符串规整成高德可识别的城市名。

    标题提取可能带杂质（如"云南亲子自然"），逐步截短做地理编码，
    取第一个能编码成功的形态；全部失败则原样返回。
    """
    candidates = [destination, destination[:4], destination[:2]]
    seen = set()
    with httpx.Client(timeout=20.0) as client:
        for cand in candidates:
            cand = cand.strip()
            if len(cand) < 2 or cand in seen:
                continue
            seen.add(cand)
            geo = _geocode_raw(client, key, cand)
            if geo:
                # 优先市级名；省级查询（如"云南"）city 字段为空列表，退回省名
                city = geo.get("city")
                if isinstance(city, str) and city:
                    return city
                province = geo.get("province")
                if isinstance(province, str) and province:
                    return province
                return cand
    return destination


def _resolve_nodes(key: str, city: str, names: list[str]) -> list[dict[str, str]]:
    resolved = []
    with httpx.Client(timeout=20.0) as client:
        for name in names:
            # citylimit=true 把 POI 搜索圈死在目的地城市，杜绝"昆明的活动搜到河北"的跨省错配；
            # 搜不到用裸地名做地理编码兜底（不拼城市前缀——"广州市清晖园"会退化成广州市中心泛点，
            # 而裸"清晖园"能正确定位到顺德；偶发的跨省误配交给 _drop_outliers 剔除），
            # 仍失败则丢弃该节点
            node = _search_poi(client, key, city, name) or _geocode(client, key, name)
            if node:
                resolved.append(node)
    return resolved


def _drop_outliers(resolved: list[dict[str, str]]) -> list[dict[str, str]]:
    """剔除明显错解析的离群节点。

    用中位数坐标作为中心（对离群点天然鲁棒，不像均值会被单个远点拉偏），
    各点到中位中心的距离超过 max(300km, 3×中位距离) 即剔除：
    - 昆明→大理→丽江这类真实跨城行程（点间 100-260km）不会被误杀；
    - 被错配到外省的点（如云南行程里出现 1900km 外的河北 POI）会被剔除。
    """
    if len(resolved) < 3:
        return resolved

    def _to_xy(loc: str) -> tuple[float, float]:
        lng, lat = loc.split(",")
        return float(lng), float(lat)

    def _median(values: list[float]) -> float:
        s = sorted(values)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2

    points = [_to_xy(n["location"]) for n in resolved]
    center = (_median([p[0] for p in points]), _median([p[1] for p in points]))

    def _dist_km(a: tuple[float, float], b: tuple[float, float]) -> float:
        # 中国纬度范围内的近似换算：1° 经度 ≈ 88km，1° 纬度 ≈ 111km
        return (((a[0] - b[0]) * 88) ** 2 + ((a[1] - b[1]) * 111) ** 2) ** 0.5

    dists = [_dist_km(p, center) for p in points]
    threshold = max(300.0, _median(dists) * 3)
    return [n for n, d in zip(resolved, dists) if d <= threshold]


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


def _fetch_static_map(
    key: str,
    resolved: list[dict[str, str]],
    route_points: list[str],
    labels: list[str] | None = None,
) -> bytes:
    """labels 缺省按 A、B、C…顺序编号；细节图传入原字母保持与图例一致"""
    if labels is None:
        labels = [chr(65 + idx) for idx in range(len(resolved))]
    markers = "|".join(
        f"mid,0x2563eb,{label}:{node['location']}"
        for label, node in zip(labels, resolved[:MAX_ROUTE_NODES])
    )
    # 不再使用 labels 参数：POI 名称里的逗号/冒号会撕碎高德的样式字段格式导致生图失败，
    # 且中文 URL 编码后体积巨大。地点名由图下方的 A-J 图例承担。
    paths = f"6,0x2563eb,0.85,,:{';'.join(route_points)}" if route_points else ""
    params = {
        "key": key,
        # 高德静态图 scale=2 时 size 上限 512*512，此前 900*420 超限属未定义行为
        "size": "512*320",
        "scale": "2",
        "markers": markers,
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
        "citylimit": "true",
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


def _geocode_raw(client: httpx.Client, key: str, address: str) -> dict[str, Any]:
    """返回高德地理编码的原始结果（含 city/province/adcode 等字段）"""
    data = _request(client, key, "/geocode/geo", {"address": address})
    geocodes = data.get("geocodes") or []
    return geocodes[0] if geocodes else {}


def _geocode(client: httpx.Client, key: str, address: str) -> dict[str, str]:
    geocode = _geocode_raw(client, key, address)
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
    # "昆明老街/南强街"这类并列写法取前半段，斜杠原文拿去搜 POI 必然失败
    name = name.split("/")[0].split("／")[0]
    return name.strip(" ：:，,。·-")


def _looks_like_place(name: str) -> bool:
    if len(name) < 2 or len(name) > 24:
        return False
    blocked = (
        "本日亮点", "本日预算", "免责声明", "省钱技巧", "应对方案", "总计", "门票",
        # 活动/提示类加粗文本不是地名，拿去搜 POI 会跨省错配（如"生态廊道骑行"曾配到河北）
        "骑行", "徒步", "体验", "自由活动", "预约", "无法", "建议", "提示", "注意",
        "集合", "返程", "入住", "退房", "休整", "自理", "打卡", "路线", "行程",
        "周边",  # "大理古城周边民宿"这类模糊描述会被 POI 搜索乱配
    )
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


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
