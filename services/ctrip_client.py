"""
携程问道 API 客户端
POST https://externalcallback.ctrip.com/skills/api/crew/qclaw/searchInfo

支持异步并行调用，Token 从环境变量 WENDAO_API_KEY 读取。
"""

import os
import json
import httpx
from typing import Optional

WENDAO_URL = "https://externalcallback.ctrip.com/skills/api/crew/qclaw/searchInfo"


class CtripClient:
    """携程问道 API 客户端"""

    def __init__(self, token: Optional[str] = None):
        self.token = token or os.getenv("WENDAO_API_KEY", "").strip()
        if not self.token:
            raise ValueError("WENDAO_API_KEY 未配置，请在连接器设置中配置携程问道 Token")

    async def query(self, question: str) -> str:
        """单次查询，返回 Markdown 格式结果"""
        payload = {
            "inputs": {
                "token": self.token,
                "query": question,
            }
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                WENDAO_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            return self._extract_result(data)

    async def query_many(self, questions: list[str]) -> list[str]:
        """并行查询多个问题"""
        import asyncio
        tasks = [self.query(q) for q in questions]
        return await asyncio.gather(*tasks, return_exceptions=True)

    def _extract_result(self, data: dict) -> str:
        """从 API 响应中提取 result 字段。

        无数据或 API 报错时返回空串，调用方（build_user_message）会跳过空板块，
        避免把错误信息当作真实数据注入 LLM 上下文。
        """
        if not isinstance(data, dict) or data.get("error"):
            return ""
        result = data.get("result", "")
        if isinstance(result, dict):
            if result.get("error"):
                return ""
            # 某些情况下 result 是嵌套对象，取 content 或转为 JSON
            result = result.get("content", "") or json.dumps(result, ensure_ascii=False)
        if isinstance(result, str) and '"error"' in result[:80]:
            return ""
        return result or ""


# 全局单例
_ctrip_client: Optional[CtripClient] = None


def get_ctrip_client() -> CtripClient:
    global _ctrip_client
    if _ctrip_client is None:
        _ctrip_client = CtripClient()
    return _ctrip_client
