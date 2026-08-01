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
import re
import time
import httpx
from typing import AsyncGenerator

from prompts import (
    build_user_message,
    get_system_prompt,
    normalize_generation_mode,
    report_max_tokens,
)

logger = logging.getLogger("uvicorn.error.orchestrator")


class LLMClientError(Exception):
    """LLM 调用异常"""
    pass


class LLMClient:
    """OpenAI 兼容 API 客户端（httpx 直接调用）"""

    def __init__(
        self, base_url: str, api_key: str, model: str,
        max_tokens: int = 16384, temperature: float = 0.7,
        first_token_timeout: float = 45.0, stream_timeout: float = 75.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.first_token_timeout = max(0.01, first_token_timeout)
        self.stream_timeout = max(self.first_token_timeout + 0.01, stream_timeout)
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
        started = time.monotonic()
        first_chunk_elapsed = None
        chunk_count = 0
        char_count = 0
        payload = self._build_payload(messages, stream=True)
        # DeepSeek V4 默认可能启用思考模式并长时间只返回 reasoning_content，
        # 甚至耗尽输出额度而没有最终正文。旅行报告只需要最终 Markdown。
        payload["thinking"] = {"type": "disabled"}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=self.stream_timeout + 5.0) as client:
            try:
                async with asyncio.timeout(self.stream_timeout):
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

                        lines = response.aiter_lines().__aiter__()
                        while True:
                            try:
                                if first_chunk_elapsed is None:
                                    remaining = self.first_token_timeout - (
                                        time.monotonic() - started
                                    )
                                    if remaining <= 0:
                                        raise TimeoutError
                                    async with asyncio.timeout(remaining):
                                        line = await anext(lines)
                                else:
                                    line = await anext(lines)
                            except StopAsyncIteration:
                                break

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
                                            if first_chunk_elapsed is None:
                                                first_chunk_elapsed = time.monotonic() - started
                                            chunk_count += 1
                                            char_count += len(content)
                                            yield content
                                        finish_reason = choices[0].get("finish_reason")
                                        if finish_reason:
                                            self.last_finish_reason = finish_reason
                                except json.JSONDecodeError:
                                    continue
            except TimeoutError:
                self.last_finish_reason = "timeout"
                phase = "首字" if first_chunk_elapsed is None else "总生成"
                logger.warning(
                    "LLM stream timeout model=%s phase=%s limit=%.1fs",
                    self.model,
                    phase,
                    self.first_token_timeout if first_chunk_elapsed is None else self.stream_timeout,
                )
                return
            except httpx.ConnectError:
                raise LLMClientError(f"无法连接到 {self.base_url}，请检查网络和 API 地址")
            except httpx.TimeoutException:
                raise LLMClientError("API 请求超时，请重试")
            finally:
                logger.info(
                    "LLM stream model=%s elapsed=%.2fs first_token=%s chunks=%d chars=%d finish=%s",
                    self.model,
                    time.monotonic() - started,
                    f"{first_chunk_elapsed:.2f}s" if first_chunk_elapsed is not None else "none",
                    chunk_count,
                    char_count,
                    self.last_finish_reason,
                )

    async def chat_json(self, messages: list[dict]) -> str:
        """以非流式 JSON 模式执行短结构化任务，并兼容不支持 response_format 的网关。"""
        started = time.monotonic()
        payload = self._build_payload(messages, stream=False)
        payload["response_format"] = {"type": "json_object"}
        # DeepSeek V4 默认开启思考模式。短结构化任务若不显式关闭，模型可能
        # 只返回 reasoning_content（分析文字）而没有最终 JSON content。
        payload["thinking"] = {"type": "disabled"}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # 主模型作为回退时允许更长的外层时限（route_planner: 55s）。
        # httpx 必须略长于它，否则会先在客户端层以 40s 终止。
        async with httpx.AsyncClient(timeout=65.0) as client:
            for structured in (True, False):
                request_payload = dict(payload)
                if not structured:
                    request_payload.pop("response_format", None)
                try:
                    response = await client.post(
                        self.chat_url, json=request_payload, headers=headers
                    )
                except httpx.ConnectError:
                    raise LLMClientError(f"无法连接到 {self.base_url}，请检查网络和 API 地址")
                except httpx.TimeoutException:
                    raise LLMClientError("API 请求超时，请重试")

                # 部分 OpenAI 兼容网关尚未实现 response_format；仅这种请求
                # 失败时降级为普通非流式调用，避免两个模型都无条件失败。
                if structured and response.status_code in (400, 404, 422):
                    logger.warning(
                        "model=%s 的网关不接受 response_format（HTTP %s），降级为普通 JSON 提示",
                        self.model,
                        response.status_code,
                    )
                    continue
                if response.status_code == 401:
                    raise LLMClientError("API Key 无效，请检查配置")
                if response.status_code == 429:
                    raise LLMClientError("API 调用频率过高，请稍后再试")
                if response.status_code >= 400:
                    raise LLMClientError(f"API 返回错误 (HTTP {response.status_code})")

                try:
                    data = response.json()
                    choice = (data.get("choices") or [])[0]
                    message = choice.get("message") or {}
                    # reasoning_content 是思考过程而非最终答案，绝不能拿它当
                    # JSON 解析；否则会出现“有返回但抽取失败”的误导日志。
                    content = message.get("content") or ""
                    if isinstance(content, list):
                        content = "".join(
                            item.get("text", "") if isinstance(item, dict) else str(item)
                            for item in content
                        )
                    self.last_finish_reason = choice.get("finish_reason")
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    raise LLMClientError("结构化任务返回格式异常") from exc
                if not isinstance(content, str) or not content.strip():
                    raise LLMClientError("结构化任务返回空内容")
                logger.info(
                    "LLM json model=%s elapsed=%.2fs chars=%d finish=%s structured=%s",
                    self.model,
                    time.monotonic() - started,
                    len(content),
                    self.last_finish_reason,
                    structured,
                )
                return content

        raise LLMClientError("结构化任务请求失败")


class TravelGuideOrchestrator:
    """旅游攻略编排器 v2.0 — 实时数据 + AI 生成"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        mode: str = "professional",
    ):
        if not api_key:
            raise LLMClientError("未配置 API Key，请在 .env 文件中设置 LLM_API_KEY")
        # max_tokens 从配置读取（.env 的 LLM_MAX_TOKENS）：上限给足可避免长行程
        # 输出被 8192 截断而触发自动续写——续写会让总生成时间接近翻倍
        from config import llm_config
        self.mode = normalize_generation_mode(mode)
        if self.mode == "professional":
            first_token_timeout = llm_config.professional_first_token_timeout
            stream_timeout = llm_config.professional_stream_timeout
        else:
            first_token_timeout = llm_config.first_token_timeout
            stream_timeout = llm_config.stream_timeout
        self.llm = LLMClient(
            base_url, api_key, model,
            max_tokens=report_max_tokens(self.mode, llm_config.max_tokens),
            temperature=llm_config.temperature,
            first_token_timeout=first_token_timeout,
            stream_timeout=stream_timeout,
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

    async def _stream_once(self, messages: list[dict], sink: dict):
        """执行一轮模型流并同时外发、累积正文。"""
        full = ""
        buffer = ""
        stream = self.llm.chat_stream(messages).__aiter__()
        pending = None
        wait_started = time.monotonic()
        try:
            while True:
                if pending is None:
                    pending = asyncio.create_task(anext(stream))
                done, _ = await asyncio.wait({pending}, timeout=10.0)
                if not done:
                    elapsed = int(time.monotonic() - wait_started)
                    label = "返回正文" if not full else "继续生成"
                    yield {
                        "type": "progress",
                        "data": f"正在等待模型{label}...（已等待 {elapsed} 秒）",
                    }
                    continue
                try:
                    content = pending.result()
                except StopAsyncIteration:
                    break
                finally:
                    pending = None
                full += content
                buffer += content
                if len(buffer) >= 200 or "\n" in buffer:
                    yield {"type": "content", "data": buffer}
                    buffer = ""
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        if buffer:
            yield {"type": "content", "data": buffer}
        sink["content"] = full

    @staticmethod
    def _report_completion_issue(content: str, expected_days: int | None = None) -> str:
        """返回会导致报告不可交付的结构缺口；空字符串表示完整。"""
        day_numbers = {
            int(number)
            for number in re.findall(
                r"^###\s+Day\s*(\d+)\b", content or "", re.MULTILINE | re.IGNORECASE
            )
        }
        if not day_numbers:
            return "缺少分日行程"
        if expected_days:
            missing_days = [day for day in range(1, expected_days + 1) if day not in day_numbers]
            if missing_days:
                return "缺少 " + "、".join(f"Day {day}" for day in missing_days)
        if not re.search(r"^##[^\n]*(?:总预算|预算拆解|费用汇总)", content or "", re.MULTILINE):
            return "缺少总预算板块"
        return ""

    @staticmethod
    def _expected_days_from_query(query: str) -> int | None:
        match = re.search(r"(?<!\d)(\d{1,2})\s*(?:天|日)(?!\d)", query or "")
        if not match:
            return None
        days = int(match.group(1))
        return days if 1 <= days <= 60 else None

    @staticmethod
    def _expected_days_from_report(content: str) -> int | None:
        heading = re.search(r"^#\s+[^\n]*?(\d{1,2})\s*(?:天|日)", content or "", re.MULTILINE)
        if not heading:
            return None
        days = int(heading.group(1))
        return days if 1 <= days <= 60 else None

    async def _stream_events(
        self,
        messages: list[dict],
        sink: dict,
        expected_days: int | None = None,
        validate_completion: bool = False,
    ):
        """流式生成正文；空流重试，截断或结构不完整时补写一次。"""
        first_sink = {}
        async for event in self._stream_once(messages, first_sink):
            yield event
        full = first_sink.get("content", "")

        # 部分 OpenAI 兼容上游偶发等待到超时边缘后返回 finish_reason=length，
        # 但没有任何 delta 内容。此时数据采集结果仍然有效，只重试正文模型，
        # 不让请求以“空报告”结束并把页面直接复位。
        if not full.strip():
            logger.warning(
                "报告模型返回空流，自动重试一次 finish=%s",
                self.llm.last_finish_reason,
            )
            yield {
                "type": "progress",
                "data": "模型暂未返回正文，正在自动重试（无需重新采集数据）...",
            }
            retry_sink = {}
            async for event in self._stream_once(messages, retry_sink):
                yield event
            full = retry_sink.get("content", "")
            if not full.strip():
                if self.llm.last_finish_reason == "timeout":
                    raise LLMClientError("模型连续两次等待正文超时，请稍后重新提交")
                raise LLMClientError("模型连续两次未返回报告正文，请重新提交")

        effective_expected_days = expected_days or self._expected_days_from_report(full)
        should_validate = validate_completion or effective_expected_days is not None
        completion_issue = (
            self._report_completion_issue(full, effective_expected_days)
            if should_validate else ""
        )
        needs_continuation = (
            self.llm.last_finish_reason == "length" or bool(completion_issue)
        )
        if needs_continuation:
            reason = "输出被截断" if self.llm.last_finish_reason == "length" else completion_issue
            logger.warning("报告不完整，自动补写一次 reason=%s", reason)
            yield {"type": "progress", "data": "报告尚未完整，正在自动补全剩余内容..."}
            continue_messages = messages + [
                {"role": "assistant", "content": full},
                {
                    "role": "user",
                    "content": (
                        "继续输出剩余内容，从中断处无缝续写，不要重复任何已输出内容，"
                        "不要加过渡语。必须补完全部 Day、总预算、预约证件、避坑提示、"
                        "行前物品清单、行程知识图谱和免责声明。"
                    ),
                },
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
            # 最多补写一轮；若仍缺关键内容，不把残缺报告交付给用户。
            effective_expected_days = expected_days or self._expected_days_from_report(full)
            completion_issue = (
                self._report_completion_issue(full, effective_expected_days)
                if should_validate else ""
            )
            if completion_issue:
                raise LLMClientError(f"报告自动补全后仍不完整：{completion_issue}，请重新提交")

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
        generation_started = time.monotonic()
        from services.trip_intent import classify_trip_intent
        intent = classify_trip_intent(query)

        # ---------- Phase 1: 实时数据采集 + 路线规划（并行，滚动播报） ----------
        yield {"type": "progress", "data": "正在查询携程问道 · 机票酒店景点数据..."}

        travel_data = {}
        day_plan = None
        route, plan_status = None, "failed"
        collect_task = None
        plan_task = None
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
                stage_started = time.monotonic()
                try:
                    if not intent.use_drive_planner:
                        if intent.is_outbound:
                            note("已识别为出境行程，跳过中国大陆自驾路线规划...")
                        else:
                            note("未检测到明确自驾需求，跳过驾车路线规划...")
                        return None, "not_applicable", None
                    r, s = await plan_route(query, self.fast_llm, fallback_llm=self.llm, on_progress=note)
                    dp = None
                    if r:
                        note("路线骨架已锁定，正在分配每日行程节奏...")
                        try:
                            dp = await build_day_plan(query, r, self.fast_llm)
                        except Exception:
                            logger.exception("日程脚手架生成异常，退化为仅注入路线骨架")
                    return r, s, dp
                finally:
                    logger.info("stage=route_plan elapsed=%.2fs", time.monotonic() - stage_started)

            async def collect_with_timing():
                stage_started = time.monotonic()
                try:
                    return await collect_travel_data(
                        query,
                        is_international=intent.is_outbound,
                        on_progress=note,
                    )
                finally:
                    logger.info("stage=data_collect elapsed=%.2fs", time.monotonic() - stage_started)

            collect_task = asyncio.create_task(collect_with_timing())
            # 规划整体设 90s 兜底上限：内部各 LLM 小调用已有 25-30s 超时，
            # 正常远够用；万一 API 拥堵挂起，宁可降级也不能让页面无限等待
            plan_task = asyncio.create_task(asyncio.wait_for(plan_and_scaffold(), timeout=90.0))

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
                logger.error("路线规划超时（90s），降级为纯 LLM 排线")
            except Exception:
                logger.exception("路线规划任务异常")

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
        finally:
            # 浏览器取消 SSE 时 generate() 会被关闭。显式取消内部任务，避免
            # 携程查询/路线规划在用户已取消后继续占用连接、令牌与并发资源。
            unfinished = [
                task for task in (collect_task, plan_task)
                if task is not None and not task.done()
            ]
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)

        # ---------- Phase 2: LLM 生成 ----------
        yield {"type": "progress", "data": "AI 正在分析数据并规划行程..."}

        user_message = build_user_message(query, travel_data, mode=self.mode)
        system_message = get_system_prompt(self.mode)
        logger.info(
            "stage=prompt mode=%s outbound=%s self_drive=%s system_chars=%d user_chars=%d",
            self.mode,
            intent.is_outbound,
            intent.is_self_drive,
            len(system_message),
            len(user_message),
        )

        messages = [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ]

        try:
            report_started = time.monotonic()
            if day_plan:
                # 锁定骨架路径只生成一次完整草稿。标题偏差由程序按骨架
                # 确定性修正；其他结构问题只告警并继续输出当前完整版本。
                # 不能因为同一个校验误差整篇调用模型两遍，让页面看起来
                # 陷入“每日行程”循环且迟迟不产出 HTML。
                from services.route_planner import (
                    validate_day_sequence,
                    repair_day_headings,
                )

                attempt_started = time.monotonic()
                yield {"type": "progress", "data": "AI 正在按锁定行程骨架生成攻略..."}
                sink = {}
                async for event in self._stream_events(messages, sink):
                    yield event

                full_content = sink.get("content", "")
                full_content, repaired = repair_day_headings(full_content, day_plan)
                if repaired:
                    # 用修正后的全文替换已经流出的草稿；这是内存字符串
                    # 操作，不再调用模型，也不会增加分钟级等待。
                    yield {"type": "reset"}
                    yield {
                        "type": "progress",
                        "data": f"已按锁定骨架自动修正 {repaired} 个 Day 标题...",
                    }
                    yield {"type": "content", "data": full_content}

                ok, reason = validate_day_sequence(full_content, day_plan)
                if ok:
                    logger.info(
                        "stage=report_draft elapsed=%.2fs result=ok",
                        time.monotonic() - attempt_started,
                    )
                else:
                    logger.warning("锁定骨架校验未完全通过，保留完整草稿继续输出: %s", reason)
                    logger.info(
                        "stage=report_draft elapsed=%.2fs result=accepted_with_warning reason=%s",
                        time.monotonic() - attempt_started,
                        reason,
                    )
                    yield {
                        "type": "progress",
                        "data": "行程结构校验未完全通过，已保留完整草稿并继续生成文档...",
                    }
            else:
                label = "标准版" if self.mode == "standard" else "专业版"
                yield {"type": "progress", "data": f"AI 正在生成{label}攻略..."}
                sink = {}
                async for event in self._stream_events(
                    messages,
                    sink,
                    expected_days=self._expected_days_from_query(query),
                    validate_completion=True,
                ):
                    yield event

            yield {"type": "progress", "data": "正在生成精美文档..."}
            logger.info(
                "stage=report_generation mode=%s elapsed=%.2fs total_elapsed=%.2fs",
                self.mode,
                time.monotonic() - report_started,
                time.monotonic() - generation_started,
            )

        except LLMClientError as e:
            yield {"type": "error", "data": str(e)}
        except Exception as e:
            yield {"type": "error", "data": f"未知错误: {str(e)}"}
