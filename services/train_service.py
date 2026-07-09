"""
12306 高铁查票服务
封装 TripStar 的 client.py（4679 行），只暴露查询接口，不实现下单。
"""

import subprocess
import json
import sys
from pathlib import Path

CLIENT_PATH = Path(__file__).parent / "12306_client.py"


def _run_client(command: list[str]) -> dict:
    """运行 12306 client.py 并解析 JSON 输出"""
    try:
        result = subprocess.run(
            [sys.executable, str(CLIENT_PATH)] + command,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return {"error": result.stderr.strip() or "12306查询失败", "raw": result.stdout.strip()}

        output = result.stdout.strip()
        if not output:
            return {"error": "无返回数据"}

        # 尝试解析 JSON
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return {"text": output}
    except subprocess.TimeoutExpired:
        return {"error": "12306 查询超时"}
    except FileNotFoundError:
        return {"error": f"Python 不可用: {sys.executable}"}


def query_tickets(from_station: str, to_station: str, date: str) -> dict:
    """查询余票

    Args:
        from_station: 出发站（中文/拼音/三字码）
        to_station: 到达站
        date: 日期，格式 YYYY-MM-DD
    """
    return _run_client(["left-ticket", "--from", from_station, "--to", to_station, "--date", date])


def query_route(train_no: str, from_station: str, to_station: str, date: str) -> dict:
    """查询经停站"""
    return _run_client(["route", "--train-no", train_no, "--from", from_station, "--to", to_station, "--date", date])


def query_transfer(from_station: str, to_station: str, date: str) -> dict:
    """查询中转换乘方案"""
    return _run_client(["transfer-ticket", "--from", from_station, "--to", to_station, "--date", date])


def format_ticket_result(result: dict) -> str:
    """将 12306 查询结果格式化为可读文本"""
    if "error" in result:
        return f"12306 查询失败: {result['error']}"

    text = result.get("text", "")
    if text:
        return text

    # 如果返回了结构化数据，做简单格式化
    if "raw" in result:
        return result["raw"]

    return json.dumps(result, ensure_ascii=False, indent=2)
