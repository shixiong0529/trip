"""
国际机票比价服务
基于 fast-flights 库（Google Flights 数据）
"""

import subprocess
import json


def search_flights(
    origin: str,
    destination: str,
    date: str,
    nonstop: bool = False,
    passengers: int = 1,
) -> str:
    """搜索国际机票

    Args:
        origin: 出发机场三字码（如 "PEK", "PVG"）
        destination: 到达机场三字码（如 "NRT", "LAX"）
        date: 日期 YYYY-MM-DD
        nonstop: 仅直飞
        passengers: 乘客数
    Returns:
        Markdown 格式的航班列表
    """
    cmd = [
        "uvx", "--with", "fast-flights", "python3", "-c", _SEARCH_SCRIPT,
        origin, destination, date, str(passengers), "1" if nonstop else "0",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return f"国际机票查询失败: {result.stderr[:200]}"
        return result.stdout.strip() or "未找到符合条件的航班"
    except subprocess.TimeoutExpired:
        return "国际机票查询超时"
    except FileNotFoundError:
        return "uvx 未安装，国际机票搜索不可用。请安装: pip install uv"


# 通过 argv 传参，避免字符串拼接引入注入或引号嵌套问题
_SEARCH_SCRIPT = """
import sys
from fast_flights import FlightData, Passengers, get_flights

origin, dest, date, psg, nonstop = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5] == "1"

result = get_flights(
    flight_data=[FlightData(date=date, from_airport=origin, to_airport=dest)],
    trip="one-way",
    seat="economy",
    passengers=Passengers(adults=psg),
    fetch_mode="fallback",
)

flights = result.flights[:10]
if not flights:
    print("未找到符合条件的航班")

for f in flights:
    stops = getattr(f, "stops", None)
    if nonstop and stops not in (0, "0"):
        continue
    stop_str = "直飞" if stops in (0, "0") else "经停{}站".format(stops)
    print("- **{}** {} → {} | {} | {} | {}".format(
        getattr(f, "name", "未知航司"),
        getattr(f, "departure", "?"),
        getattr(f, "arrival", "?"),
        getattr(f, "duration", "?"),
        stop_str,
        getattr(f, "price", "价格未知"),
    ))
"""
