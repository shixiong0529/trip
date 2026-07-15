"""
AI 编排层 v2.0
两阶段生成：实时数据采集 → LLM 生成攻略
集成携程问道、12306、Google Flights、OpenSky、航空气象

除两阶段编排外，app.py 还暴露两个独立查询端点（不经过 LLM，直接返回原始数据）：
  GET /api/train/tickets   — 12306 余票查询（?from_station&to_station&date）
  GET /api/flights/search  — 国际机票查询，Google Flights（?origin&destination&date&nonstop&passengers）
"""

import asyncio
import json
import logging
import httpx
from typing import AsyncGenerator

from prompts import SYSTEM_PROMPT, build_user_message


class LLMClientError(Exception):
    """LLM 调用异常"""
    pass


class LLMClient:
    """OpenAI 兼容 API 客户端（httpx 直接调用）"""

    def __init__(
        self, base_url: str, api_key: str, model: str,
        max_tokens: int = 16384, temperature: float = 0.7,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.last_finish_reason = None

    @property
    def chat_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _build_payload(self, messages: list[dict], stream: bool = True) -> dict:
        return {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
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
        # max_tokens 从配置读取（.env 的 LLM_MAX_TOKENS）：上限给足可避免长行程
        # 输出被 8192 截断而触发自动续写——续写会让总生成时间接近翻倍
        from config import llm_config
        self.llm = LLMClient(
            base_url, api_key, model,
            max_tokens=llm_config.max_tokens,
            temperature=llm_config.temperature,
        )
        # 结构化小任务（途经点抽取、停留天分配）用快速模型，输出短、
        # 温度低；正文生成仍用主模型
        self.fast_llm = LLMClient(
            base_url, api_key, llm_config.fast_model,
            max_tokens=2048,
            temperature=0.1,
        )

    @staticmethod
    def _correction_prompt(reason: str, day_plan: dict) -> str:
        """分日顺序不符时的纠正指令：点名问题 + 重贴锁定骨架，要求整篇重写。"""
        return (
            f"你上一版的分日行程不符合锁定骨架：{reason}。\n"
            "请严格重写整篇攻略。分日行程必须与下面的锁定骨架逐天一一对应："
            "Day 数量、每天的城市/路线、里程、时长完全一致，禁止反向遍历，"
            "禁止增删天数，禁止改动里程。「路线总览」必须原样使用给定的一行。\n\n"
            f"【路线总览 · 必须原样采用】\n{day_plan['overview']}\n\n"
            f"{day_plan['scaffold_md']}"
        )

    async def _stream_events(self, messages: list[dict], sink: dict):
        """流式生成：content 事件边生成边外发，全文同步累积到 sink["content"]。

        输出被截断（finish_reason == "length"）时自动续写一轮。
        边流式边累积让锁定骨架路径也能实时出字——校验在流结束后进行，
        不合格由调用方发 reset 事件清屏重来，而不是让用户对着空屏等全文。
        """
        full = ""
        buffer = ""
        async for content in self.llm.chat_stream(messages):
            full += content
            buffer += content
            if len(buffer) >= 200 or "\n" in buffer:
                yield {"type": "content", "data": buffer}
                buffer = ""
        if buffer:
            yield {"type": "content", "data": buffer}

        if self.llm.last_finish_reason == "length":
            yield {"type": "progress", "data": "输出较长，正在自动续写..."}
            continue_messages = messages + [
                {"role": "assistant", "content": full},
                {"role": "user", "content": "继续输出剩余内容，从中断处无缝续写，不要重复任何已输出内容，不要加任何过渡语。"},
            ]
            buffer = ""
            async for content in self.llm.chat_stream(continue_messages):
                full += content
                buffer += content
                if len(buffer) >= 200 or "\n" in buffer:
                    yield {"type": "content", "data": buffer}
                    buffer = ""
            if buffer:
                yield {"type": "content", "data": buffer}
            # 续写轮 finish_reason 仍为 "length" 也不再继续（最多续写 1 轮）

        sink["content"] = full

    async def generate(self, query: str) -> AsyncGenerator[dict, None]:
        """两阶段生成攻略

        Phase 1: 并行采集实时数据（携程问道 + 12306 + OpenSky + 航空气象）
        Phase 2: LLM 基于实时数据生成结构化攻略

        Yields:
            {"type": "progress", "data": "..."}  - 进度
            {"type": "content", "data": "..."}   - 流式文本
            {"type": "error", "data": "..."}     - 错误
        """
        # ---------- Phase 1: 实时数据采集 + 路线规划（并行，滚动播报） ----------
        yield {"type": "progress", "data": "正在查询携程问道 · 机票酒店景点数据..."}

        travel_data = {}
        day_plan = None
        route, plan_status = None, "failed"
        try:
            from services.data_collector import collect_travel_data
            from services.route_planner import plan_route, build_day_plan

            # 各任务的内部进度通过队列上报，这里边等边转发成滚动字幕，
            # 避免最长一步（问道查询）期间界面静止
            status_q: asyncio.Queue = asyncio.Queue()

            def note(msg: str) -> None:
                status_q.put_nowait(msg)

            async def plan_and_scaffold():
                # 规划 + 日程脚手架串成一个任务，与数据采集并行，
                # 脚手架耗时被问道查询完全覆盖
                r, s = await plan_route(query, self.fast_llm, fallback_llm=self.llm, on_progress=note)
                dp = None
                if r:
                    note("路线骨架已锁定，正在分配每日行程节奏...")
                    try:
                        dp = await build_day_plan(query, r, self.fast_llm)
                    except Exception:
                        logging.getLogger("orchestrator").exception("日程脚手架生成异常，退化为仅注入路线骨架")
                return r, s, dp

            collect_task = asyncio.create_task(collect_travel_data(query, on_progress=note))
            # 规划整体设 90s 兜底上限：内部各 LLM 小调用已有 25-30s 超时，
            # 正常远够用；万一 API 拥堵挂起，宁可降级也不能让页面无限等待
            plan_task = asyncio.create_task(asyncio.wait_for(plan_and_scaffold(), timeout=90.0))

            import time
            start = time.monotonic()
            pending = {collect_task, plan_task}
            while pending:
                done, pending = await asyncio.wait(pending, timeout=3)
                emitted = False
                while not status_q.empty():
                    yield {"type": "progress", "data": status_q.get_nowait()}
                    emitted = True
                if not emitted and pending:
                    # 没有新事件也报个心跳，让用户知道后端在干活
                    waiting = "携程问道数据" if collect_task in pending else "路线规划"
                    yield {"type": "progress", "data": f"正在等待{waiting}返回...（已用时 {int(time.monotonic() - start)} 秒）"}
            # 任务结束后清空剩余播报
            while not status_q.empty():
                yield {"type": "progress", "data": status_q.get_nowait()}

            try:
                travel_data = collect_task.result()
            except Exception as e:
                yield {"type": "progress", "data": f"实时数据查询异常（将使用AI推算）: {str(e)[:60]}"}
            try:
                route, plan_status, day_plan = plan_task.result()
            except asyncio.TimeoutError:
                logging.getLogger("orchestrator").error("路线规划超时（90s），降级为纯 LLM 排线")
            except Exception:
                logging.getLogger("orchestrator").exception("路线规划任务异常")

            if route:
                travel_data["route_plan"] = route["markdown"]
                if day_plan:
                    travel_data["route_overview"] = day_plan["overview"]
                    travel_data["day_scaffold"] = day_plan["scaffold_md"]
                    yield {"type": "progress", "data": "多点路线已按地图实测距离排定，并锁定每日行程骨架..."}
                else:
                    yield {"type": "progress", "data": "多点路线已按地图实测距离排定最短环线..."}
            elif plan_status == "failed":
                # 规划失败对路线质量影响很大，必须让用户可见，而不是静默降级
                yield {"type": "progress", "data": "⚠️ 多点路线规划未生效，本次路线顺序由 AI 自行推算，建议重新生成一次..."}
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
            if day_plan:
                # 锁定骨架路径：实时流式生成 + 逐块增量校验分日顺序。
                # 第 1 版一旦偏离立即掐断（不等错误版本写完），reset 清屏后
                # 带原因重生成；第 2 版必须完整输出，避免报告被截断。
                from services.route_planner import validate_day_sequence, check_day_sequence_prefix

                log = logging.getLogger("orchestrator")
                attempt_messages = messages
                for attempt in (1, 2):
                    yield {"type": "progress", "data": "AI 正在按锁定行程骨架生成攻略..."}
                    sink = {}
                    streamed = ""
                    early_reason = None
                    agen = self._stream_events(attempt_messages, sink)
                    try:
                        async for event in agen:
                            yield event
                            if event["type"] != "content" or attempt > 1:
                                continue
                            streamed += event["data"]
                            ok, reason = check_day_sequence_prefix(streamed, day_plan)
                            if not ok:
                                early_reason = reason
                                break
                    finally:
                        await agen.aclose()

                    full_content = sink.get("content", streamed)
                    if early_reason is not None:
                        ok, reason = False, early_reason
                    else:
                        ok, reason = validate_day_sequence(full_content, day_plan)
                    if ok:
                        break
                    log.warning("锁定骨架校验失败（第 %d 版）: %s", attempt, reason)
                    if attempt == 1:
                        yield {"type": "reset"}
                        yield {"type": "progress", "data": f"行程与锁定顺序不符（{reason}），正在按锁定顺序重新生成..."}
                        attempt_messages = messages + [
                            {"role": "assistant", "content": full_content},
                            {"role": "user", "content": self._correction_prompt(reason, day_plan)},
                        ]
                    else:
                        yield {"type": "progress", "data": "已尽力对齐锁定顺序，以当前版本输出..."}
            else:
                yield {"type": "progress", "data": "AI 正在生成详细攻略..."}
                sink = {}
                async for event in self._stream_events(messages, sink):
                    yield event

            yield {"type": "progress", "data": "正在生成精美文档..."}

        except LLMClientError as e:
            yield {"type": "error", "data": str(e)}
        except Exception as e:
            yield {"type": "error", "data": f"未知错误: {str(e)}"}
