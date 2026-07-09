"""飞猪航班动态服务单元测试(不触网)"""
import asyncio

import pytest

from services import fliggy_flight


def test_sign_deterministic():
    """TOP MD5 签名:排序拼接 + secret 首尾包裹 + 大写"""
    params = {"method": "alitrip.flight.dynamic.query", "app_key": "12345", "v": "2.0"}
    sig = fliggy_flight._sign(params, "secret")
    # 手工计算期望值
    import hashlib
    raw = "secret" + "app_key12345" + "methodalitrip.flight.dynamic.query" + "v2.0" + "secret"
    assert sig == hashlib.md5(raw.encode()).hexdigest().upper()
    assert sig == sig.upper()


def test_is_configured(monkeypatch):
    monkeypatch.delenv("FLIGGY_APP_KEY", raising=False)
    monkeypatch.delenv("FLIGGY_APP_SECRET", raising=False)
    assert fliggy_flight.is_configured() is False
    monkeypatch.setenv("FLIGGY_APP_KEY", "k")
    monkeypatch.setenv("FLIGGY_APP_SECRET", "s")
    assert fliggy_flight.is_configured() is True


def test_query_unconfigured_returns_hint(monkeypatch):
    monkeypatch.delenv("FLIGGY_APP_KEY", raising=False)
    monkeypatch.delenv("FLIGGY_APP_SECRET", raising=False)
    result = asyncio.run(fliggy_flight.query_flight_dynamic("MU5100"))
    assert "FLIGGY_APP_KEY" in result


def test_format_response_error():
    data = {"error_response": {"msg": "Invalid app Key", "code": 25}}
    out = fliggy_flight._format_response(data, "MU5100")
    assert "查询失败" in out and "Invalid app Key" in out


def test_format_response_models_wrapped():
    """TOP 数组通常包一层类型 key"""
    data = {
        "alitrip_flight_dynamic_query_response": {
            "result": {
                "models": {
                    "flight_dynamic_do": [
                        {
                            "flight_no": "MU5100",
                            "flight_date": "2026-07-10",
                            "depart_code": "PEK",
                            "arrive_code": "SHA",
                            "flight_status": "到达",
                            "gmt_plan_depart": "2026-07-10 08:00:00",
                            "plane_type": "B77W",
                        }
                    ]
                }
            }
        }
    }
    out = fliggy_flight._format_response(data, "MU5100")
    assert "MU5100" in out and "PEK" in out and "到达" in out


def test_format_response_empty():
    data = {"alitrip_flight_dynamic_query_response": {"result": {"models": {}}}}
    out = fliggy_flight._format_response(data, "MU9999")
    assert "未查到" in out
