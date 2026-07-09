"""
AI 编排层 v2.0
两阶段生成：实时数据采集 → LLM 生成攻略
集成携程问道、12306、Google Flights、OpenSky、航空气象

除两阶段编排外，app.py 还暴露两个独立查询端点（不经过 LLM，直接返回原始数据）：
  GET /api/train/tickets   — 12306 余票查询（?from_station&to_station&date）
  GET /api/flights/search  — 国际机票查询，Google Flights（?origin&destination&date&nonstop&passengers）
"""

import json
import httpx
import asyncio
from typing import AsyncGenerator

from prompts import SYSTEM_PROMPT, build_user_message


class LLMClientError(Exception):
    """LLM 调用异常"""
    pass


class LLMClient:
    """OpenAI 兼容 API 客户端（httpx 直接调用）"""

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.last_finish_reason = None

    @property
    def chat_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _build_payload(
        self, messages: list[dict], stream: bool = True,
        max_tokens: int = 8192, temperature: float = 0.7
    ) -> dict:
        return {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

    async def chat_stream(self, messages: list[dict]) -> AsyncGenerator[str, None]:
        """流式调用 LLM

        流开始时重置 self.last_finish_reason，流结束后该属性保存最后一个
        非空的 finish_reason（如 "stop"/"length"），供调用方判断是否被截断。
        """
        self.last_finish_reason = None
        payload = self._build_payload(messages, stream=True)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                async with client.stream(
                    "POST", self.chat_url, json=payload, headers=headers
                ) as response:
                    if response.status_code == 401:
                        raise LLMClientError("API Key 无效，请检查配置")
                    elif response.status_code == 429:
                        raise LLMClientError("API 调用频率过高，请稍后再试")
                    elif response.status_code >= 400:
                        body = await response.aread()
                        raise LLMClientError(f"API 返回错误 (HTTP {response.status_code}): {body.decode()[:200]}")

                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:].strip()
                            if data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data)
                                choices = chunk.get("choices", [])
                                if choices:
                                    delta = choices[0].get("delta", {})
                                    content = delta.get("content", "")
                                    if content:
                                        yield content
                                    finish_reason = choices[0].get("finish_reason")
                                    if finish_reason:
                                        self.last_finish_reason = finish_reason
                            except json.JSONDecodeError:
                                continue
            except httpx.ConnectError:
                raise LLMClientError(f"无法连接到 {self.base_url}，请检查网络和 API 地址")
            except httpx.TimeoutException:
                raise LLMClientError("API 请求超时，请重试")


class TravelGuideOrchestrator:
    """旅游攻略编排器 v2.0 — 实时数据 + AI 生成"""

    def __init__(self, base_url: str, api_key: str, model: str):
        if not api_key:
            raise LLMClientError("未配置 API Key，请在 .env 文件中设置 LLM_API_KEY")
        self.llm = LLMClient(base_url, api_key, model)

    async def generate(self, query: str) -> AsyncGenerator[dict, None]:
        """两阶段生成攻略

        Phase 1: 并行采集实时数据（携程问道 + 12306 + OpenSky + 航空气象）
        Phase 2: LLM 基于实时数据生成结构化攻略

        Yields:
            {"type": "progress", "data": "..."}  - 进度
            {"type": "content", "data": "..."}   - 流式文本
            {"type": "error", "data": "..."}     - 错误
        """
        # ---------- Phase 1: 实时数据采集 ----------
        yield {"type": "progress", "data": "正在查询携程问道 · 机票酒店景点数据..."}

        travel_data = {}
        try:
            from services.data_collector import collect_travel_data
            travel_data = await collect_travel_data(query)
        except Exception as e:
            # 数据采集失败不影响后续流程，退化为纯 LLM 生成
            yield {"type": "progress", "data": f"实时数据查询异常（将使用AI推算）: {str(e)[:60]}"}

        # ---------- Phase 2: LLM 生成 ----------
        yield {"type": "progress", "data": "AI 正在分析数据并规划行程..."}

        user_message = build_user_message(query, travel_data)
        system_message = SYSTEM_PROMPT

        messages = [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ]

        try:
            first_chunk = True
            buffer = ""
            full_content = ""
            async for content in self.llm.chat_stream(messages):
                buffer += content
                full_content += content
                if first_chunk:
                    yield {"type": "progress", "data": "AI 正在生成详细攻略..."}
                    first_chunk = False
                # 每积累 200 字符或遇到换行就发送
                if len(buffer) >= 200 or "\n" in buffer:
                    yield {"type": "content", "data": buffer}
                    buffer = ""
            # 发送剩余缓冲
            if buffer:
                yield {"type": "content", "data": buffer}

            # 输出被截断（finish_reason == "length"）时自动续写一轮，避免攻略戛然而止
            if self.llm.last_finish_reason == "length":
                yield {"type": "progress", "data": "输出较长，正在自动续写..."}
                continue_messages = messages + [
                    {"role": "assistant", "content": full_content},
                    {
                        "role": "user",
                        "content": "继续输出剩余内容，从中断处无缝续写，不要重复任何已输出内容，不要加任何过渡语。",
                    },
                ]
                buffer = ""
                async for content in self.llm.chat_stream(continue_messages):
                    buffer += content
                    if len(buffer) >= 200 or "\n" in buffer:
                        yield {"type": "content", "data": buffer}
                        buffer = ""
                if buffer:
                    yield {"type": "content", "data": buffer}
                # 续写轮 finish_reason 仍为 "length" 也不再继续（最多续写 1 轮）

            yield {"type": "progress", "data": "正在生成精美文档..."}

        except LLMClientError as e:
            yield {"type": "error", "data": str(e)}
        except Exception as e:
            yield {"type": "error", "data": f"未知错误: {str(e)}"}
