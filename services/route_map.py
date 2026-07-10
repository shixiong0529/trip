"""
HTML 线路示意图生成

从生成后的攻略 Markdown 中提取关键节点，生成报告开头的轻量 SVG
路线示意图。这里不使用真实地图底图，避免静态地图在长线/多节点
行程中出现视野、标注和加载效果不可控的问题。
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor

import httpx


MAX_ROUTE_NODES = 10


# 同一份 markdown 的路线图结果缓存（仅缓存成功结果）
_route_map_cache: dict[int, str] = {}
_ROUTE_MAP_CACHE_MAX = 16
_coordinate_cache: dict[str, tuple[float, float] | None] = {}
_COORDINATE_CACHE_MAX = 128

_ROAD_NODE_RE = re.compile(
    r"(?:^[GgSsXxYy]\s*\d+|高速|国道|省道|县道|乡道|公路|快速路|服务区|收费站|"
    r"隧道|大桥|立交|路段|环线|川藏[南北]线|青甘大环线|高速启程)",
)


def build_route_map_html(markdown_content: str) -> str:
    """生成报告开头的全程路线图 HTML。失败时返回空串，不影响报告输出。"""
    cache_key = hash(markdown_content)
    cached = _route_map_cache.get(cache_key)
    if cached:
        return cached

    items = extract_route_items(markdown_content)[:MAX_ROUTE_NODES]
    if len(items) < 2:
        return ""

    try:
        diagram = _build_schematic_svg(items, _resolve_item_coordinates(items))
    except Exception:
        return ""

    legend = "".join(
        f'<span class="route-map-chip" title="{idx + 1}. {_escape_html(item["name"])}">'
        f'<strong>{idx + 1}</strong> {_escape_html(item["name"])}</span>'
        for idx, item in enumerate(items)
    )
    html = f"""<div class="route-map-card">
  <div class="route-map-header">
    <h2>🗺️ 全程路线图</h2>
    <p>按路线总览中的城市、城镇与景点绘制，节点方位尽量贴近实际地理位置。</p>
  </div>
  <div class="route-map-diagram">{diagram}</div>
  <div class="route-map-legend">{legend}</div>
</div>
"""
    if len(_route_map_cache) >= _ROUTE_MAP_CACHE_MAX:
        _route_map_cache.clear()
    _route_map_cache[cache_key] = html
    return html


def extract_route_items(markdown_content: str) -> list[dict[str, str]]:
    """仅从“路线总览”提取地图节点，严格保持其先后顺序。

    分日表里的餐厅、道路、活动名会让一张总览图变成密集的日程图，因此
    不再作为补充来源。重复的起终点也保留，用于呈现环线或返程。
    """
    return [
        {"name": name, "day": f"第{index + 1}站"}
        for index, name in enumerate(_extract_route_overview_nodes(markdown_content))
    ]


def normalize_route_overview(content: str) -> str:
    """将路线总览规范为只包含城市、城镇和景点节点的单行路线。"""
    nodes = _extract_route_overview_nodes(content)
    if len(nodes) < 2:
        return content
    return "**路线总览：** " + " → ".join(nodes)


def _build_schematic_svg(
    items: list[dict[str, str]],
    coordinates: list[tuple[float, float] | None] | None = None,
) -> str:
    width, height = 1200, 560
    points = _geographic_points(coordinates or [], width, height) or _schematic_points(len(items))
    segment_parts = []
    marker_parts = []

    for idx, (start, end) in enumerate(zip(points, points[1:])):
        color, dashed, _ = _segment_style(idx, len(points))
        dash_attr = ' stroke-dasharray="12 10"' if dashed else ""
        segment_parts.append(
            f'<line x1="{start[0]:.1f}" y1="{start[1]:.1f}" '
            f'x2="{end[0]:.1f}" y2="{end[1]:.1f}" '
            f'stroke="{color}" stroke-width="8" stroke-linecap="round"{dash_attr}/>'
        )

    for idx, (item, point) in enumerate(zip(items, points)):
        color, _, _ = _segment_style(max(idx - 1, 0), len(points))
        if idx == 0:
            color = "#2563eb"
        elif idx == len(items) - 1:
            color = "#dc2626"
        radius = 19 if idx in {0, len(items) - 1} else 11
        label_anchor = "middle"
        label_x = point[0]
        label_y = point[1] - 28 if idx % 2 == 0 else point[1] + 44
        if point[0] < 170:
            label_anchor = "start"
            label_x = point[0] + 18
        elif point[0] > width - 170:
            label_anchor = "end"
            label_x = point[0] - 18
        day = item.get("day") or f"第{idx + 1}站"
        name = _truncate_label(item["name"], 9)
        marker_parts.append(
            f'<circle cx="{point[0]:.1f}" cy="{point[1]:.1f}" r="{radius + 5}" fill="#fff" opacity=".95"/>'
            f'<circle cx="{point[0]:.1f}" cy="{point[1]:.1f}" r="{radius}" fill="{color}" stroke="#fff" stroke-width="4"/>'
            f'<text x="{label_x:.1f}" y="{label_y:.1f}" class="route-svg-node-label" '
            f'text-anchor="{label_anchor}">{_escape_html(name)} · {_escape_html(day)}</text>'
        )

    return f"""<svg class="route-map-svg" viewBox="0 0 {width} {height}" role="img" aria-label="全程线路示意图">
  <rect x="0" y="0" width="{width}" height="{height}" rx="24" fill="#edf2f7"/>
  <text x="{width / 2}" y="54" class="route-svg-title" text-anchor="middle">行程线路示意图</text>
  <g class="route-svg-lines">{''.join(segment_parts)}</g>
  <g class="route-svg-markers">{''.join(marker_parts)}</g>
  <g class="route-svg-legend">
    <rect x="855" y="420" width="300" height="104" rx="14" fill="#ffffff" opacity=".92"/>
    <text x="885" y="456" class="route-svg-legend-title">图例</text>
    <line x1="885" y1="482" x2="955" y2="482" stroke="#7c3aed" stroke-width="6" stroke-linecap="round"/>
    <text x="970" y="488" class="route-svg-legend-text">行程主线</text>
    <line x1="885" y1="508" x2="955" y2="508" stroke="#16a34a" stroke-width="6" stroke-linecap="round"/>
    <text x="970" y="514" class="route-svg-legend-text">延伸 / 返程线</text>
  </g>
</svg>"""


def _resolve_item_coordinates(items: list[dict[str, str]]) -> list[tuple[float, float] | None]:
    """并行查询高德地理编码，失败时返回空坐标并由 SVG 使用保底布局。"""
    key = os.getenv("AMAP_WEB_SERVICE_KEY", "").strip()
    if not key:
        return [None] * len(items)

    names = [item["name"] for item in items]
    unresolved = list(dict.fromkeys(name for name in names if name not in _coordinate_cache))
    if unresolved:
        workers = min(4, len(unresolved))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = executor.map(lambda name: _geocode_place(name, key), unresolved)
            for name, coordinate in zip(unresolved, results):
                if len(_coordinate_cache) >= _COORDINATE_CACHE_MAX:
                    _coordinate_cache.clear()
                _coordinate_cache[name] = coordinate

    return [_coordinate_cache.get(name) for name in names]


def _geocode_place(name: str, key: str) -> tuple[float, float] | None:
    try:
        response = httpx.get(
            "https://restapi.amap.com/v3/geocode/geo",
            params={"key": key, "address": name},
            timeout=3.0,
        )
        payload = response.json()
        geocodes = payload.get("geocodes") or []
        location = geocodes[0].get("location", "") if payload.get("status") == "1" and geocodes else ""
        longitude, latitude = location.split(",", maxsplit=1)
        return float(longitude), float(latitude)
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError):
        return None


def _geographic_points(
    coordinates: list[tuple[float, float] | None], width: int, height: int,
) -> list[tuple[float, float]] | None:
    """将经纬度投影到 SVG，让左右/上下方向与真实地图大致一致。"""
    if len(coordinates) < 2 or any(point is None for point in coordinates):
        return None

    resolved = [point for point in coordinates if point is not None]
    longitudes = [point[0] for point in resolved]
    latitudes = [point[1] for point in resolved]
    lon_span = max(longitudes) - min(longitudes)
    lat_span = max(latitudes) - min(latitudes)
    if lon_span < 0.03 and lat_span < 0.03:
        return None

    left, right = 90.0, width - 90.0
    top, bottom = 100.0, height - 150.0
    safe_lon_span = max(lon_span, 0.12)
    safe_lat_span = max(lat_span, 0.12)
    return [
        (
            left + (longitude - min(longitudes)) / safe_lon_span * (right - left),
            bottom - (latitude - min(latitudes)) / safe_lat_span * (bottom - top),
        )
        for longitude, latitude in resolved
    ]


def _schematic_points(count: int) -> list[tuple[float, float]]:
    template = [
        (1030.0, 390.0), (910.0, 300.0), (790.0, 190.0), (650.0, 125.0),
        (505.0, 165.0), (385.0, 260.0), (275.0, 365.0), (430.0, 430.0),
        (610.0, 405.0), (790.0, 445.0), (965.0, 400.0), (1085.0, 470.0),
    ]
    if count <= 1:
        return template[:1]
    if count >= len(template):
        return template[:count]
    points = []
    max_index = len(template) - 1
    for idx in range(count):
        pos = idx * max_index / (count - 1)
        left = int(pos)
        right = min(left + 1, max_index)
        ratio = pos - left
        x = template[left][0] + (template[right][0] - template[left][0]) * ratio
        y = template[left][1] + (template[right][1] - template[left][1]) * ratio
        points.append((x, y))
    return points


def _segment_style(index: int, point_count: int) -> tuple[str, bool, str]:
    if point_count <= 3:
        return "#7c3aed", False, "行程主线"
    if index == 0:
        return "#64748b", True, "交通衔接"
    last_segment = max(point_count - 2, 1)
    if index >= last_segment * 0.66:
        return "#16a34a", False, "延伸 / 返程线"
    if index >= last_segment * 0.38:
        return "#d97706", False, "重点游览线"
    return "#7c3aed", False, "行程主线"


def _truncate_label(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def extract_route_nodes(markdown_content: str) -> list[str]:
    """从路线总览和分日行程表中提取地点名，按出现顺序去重。

    LLM 输出并不稳定：有时地点用 **加粗**，有时用「书名号」，
    有时只集中写在"路线总览"里。这里兼容三种形态。
    """
    nodes: list[str] = []
    seen = set()

    def add_node(raw_name: str) -> None:
        name = _clean_node_name(raw_name)
        if not _looks_like_place(name):
            return
        if name not in seen:
            seen.add(name)
            nodes.append(name)

    for name in _extract_route_overview_nodes(markdown_content):
        add_node(name)
        if len(nodes) >= MAX_ROUTE_NODES:
            return nodes

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
            add_node(match.group(1))
            if len(nodes) >= MAX_ROUTE_NODES:
                return nodes

        for name in _extract_quoted_place_nodes(line):
            add_node(name)
            if len(nodes) >= MAX_ROUTE_NODES:
                return nodes

    return nodes


def _extract_route_overview_nodes(markdown_content: str) -> list[str]:
    nodes: list[str] = []
    for line in markdown_content.splitlines():
        if "路线总览" not in line:
            continue
        text = re.sub(r"^\s*[-+]\s*", "", line)
        text = re.sub(r"\*\*路线总览\*\*\s*[:：]?", "", text)
        text = re.sub(r"路线总览\s*[:：]?", "", text)
        parts = re.split(r"(?:→|->|⇒|➡️|➡)", text)
        for part in parts:
            part = re.sub(r"[✈️🚄🚗🚇🚌🚕🏁]+", " ", part)
            part = re.split(r"[，,。；;]", part, maxsplit=1)[0]
            name = _clean_node_name(part)
            if _looks_like_place(name):
                nodes.append(name)
    return nodes


def _extract_quoted_place_nodes(line: str) -> list[str]:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    text = cells[1] if len(cells) > 1 else line
    # 餐饮行里的书名号通常是餐厅名/菜品名，放进路线图会让线路过细且易误配。
    if re.search(r"早餐|午餐|晚餐|下午茶|餐厅|咖啡|火锅|米线|斋饭|菜", text):
        return []
    return [m.group(1) for m in re.finditer(r"[「『]([^」』]{2,30})[」』]", text)]


def _clean_node_name(name: str) -> str:
    name = re.sub(r"\*", "", name)
    name = re.sub(r"[（(].*?[）)]", "", name)
    name = re.sub(r"^(从|经|途经|经过|抵达|到达|前往|返回|回到|出发|直奔|游览|夜游|午餐|晚餐|早餐|打卡)\s*", "", name)
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
        "周边", "环湖", "精华段",  # "大理古城周边民宿"这类模糊描述会被 POI 搜索乱配
    )
    if "¥" in name or "￥" in name or _ROAD_NODE_RE.search(name):
        return False
    return not any(word in name for word in blocked)


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
