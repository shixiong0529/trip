"""
飞猪开放平台 · 航班动态查询
API: alitrip.flight.dynamic.query（免费，无需用户授权）
文档: https://open.alitrip.com/docs/api.htm?apiId=55436

走淘宝开放平台 TOP 协议：
  网关   https://eco.taobao.com/router/rest
  签名   sign_method=md5：所有参数按 key 排序后拼接 key+value，
         首尾包裹 AppSecret，MD5 后转大写

凭证从环境变量 FLIGGY_APP_KEY / FLIGGY_APP_SECRET 读取，未配置时
is_configured() 返回 False，调用方应回退到其他数据源（OpenSky）。
"""

import os
import json
import hashlib
from datetime import datetime, timezone, timedelta

import httpx

TOP_GATEWAY = "https://eco.taobao.com/router/rest"
API_METHOD = "alitrip.flight.dynamic.query"

_CN_TZ = timezone(timedelta(hours=8))


def is_configured() -> bool:
    return bool(os.getenv("FLIGGY_APP_KEY", "").strip() and os.getenv("FLIGGY_APP_SECRET", "").strip())


def _sign(params: dict, secret: str) -> str:
    """TOP MD5 签名：secret + (key+value 按 key 升序拼接) + secret，MD5 大写"""
    joined = "".join(f"{k}{params[k]}" for k in sorted(params))
    raw = f"{secret}{joined}{secret}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def _build_params(business: dict) -> dict:
    """组装系统参数 + 业务参数并签名"""
    app_key = os.getenv("FLIGGY_APP_KEY", "").strip()
    secret = os.getenv("FLIGGY_APP_SECRET", "").strip()
    params = {
        "method": API_METHOD,
        "app_key": app_key,
        # TOP 要求 GMT+8 时间，误差不能超过 10 分钟
        "timestamp": datetime.now(_CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "format": "json",
        "v": "2.0",
        "sign_method": "md5",
    }
    params.update({k: v for k, v in business.items() if v not in (None, "")})
    params["sign"] = _sign(params, secret)
    return params


async def query_flight_dynamic(
    flight_no: str = "",
    flight_date: str = "",
    dep_airport_code: str = "",
    arr_airport_code: str = "",
) -> str:
    """查询航班动态，返回 Markdown 文本

    Args:
        flight_no: 航班号（如 MU5100）
        flight_date: 起飞日期 YYYY-MM-DD，缺省用今天（东八区）
        dep_airport_code: 出发机场三字码（可选）
        arr_airport_code: 到达机场三字码（可选）
    """
    if not is_configured():
        return "飞猪航班动态未配置（需在 .env 设置 FLIGGY_APP_KEY / FLIGGY_APP_SECRET）"

    business = {
        "flight_no": flight_no.strip().upper(),
        "flight_date": flight_date or datetime.now(_CN_TZ).strftime("%Y-%m-%d"),
        "dep_airport_code": dep_airport_code.strip().upper(),
        "arr_airport_code": arr_airport_code.strip().upper(),
    }
    params = _build_params(business)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(TOP_GATEWAY, data=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return f"飞猪航班动态查询失败: {str(e) or type(e).__name__}"

    return _format_response(data, business["flight_no"])


def _format_response(data: dict, flight_no: str) -> str:
    """格式化 TOP 响应为 Markdown"""
    err = data.get("error_response")
    if err:
        msg = err.get("sub_msg") or err.get("msg") or json.dumps(err, ensure_ascii=False)
        return f"飞猪航班动态查询失败: {msg}"

    body = data.get("alitrip_flight_dynamic_query_response", {})
    result = body.get("result", {}) or {}
    models = result.get("models", {})
    # TOP 返回的数组通常包一层类型 key，如 {"flight_dynamic_do": [...]}
    if isinstance(models, dict):
        for v in models.values():
            if isinstance(v, list):
                models = v
                break
        else:
            models = []
    if not models:
        return f"未查到航班 {flight_no} 的动态信息"

    lines = [f"### 航班动态 · {flight_no}\n"]
    lines.append("| 航班 | 日期 | 出发→到达 | 状态 | 计划起飞 | 实际起飞 | 实际到达 | 机型 | 值机 |")
    lines.append("|------|------|----------|------|----------|----------|----------|------|------|")
    for m in models[:10]:
        lines.append(
            "| {} | {} | {}→{} | {} | {} | {} | {} | {} | {} |".format(
                m.get("flight_no", "—"),
                m.get("flight_date", "—"),
                m.get("depart_code", "—"),
                m.get("arrive_code", "—"),
                m.get("flight_status", "—"),
                m.get("gmt_plan_depart", "—"),
                m.get("gmt_depart", "—"),
                m.get("gmt_arrive", "—"),
                m.get("plane_type", "—"),
                m.get("board_status", "—"),
            )
        )
    lines.append("\n> 数据来源：飞猪开放平台 alitrip.flight.dynamic.query")
    return "\n".join(lines)
