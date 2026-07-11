"""
FastAPI 应用主入口
- 静态文件服务
- API 路由定义
- SSE 流式响应
"""

import uuid
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
    FileResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from config import llm_config, app_config

# ---------- 创建应用 ----------
app = FastAPI(
    title="AI 旅行攻略生成器",
    description="输入目的地，秒出详细行程。支持 HTML/PDF/DOCX 三格式下载。",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=app_config.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class _ConcurrencyGate:
    """为长耗时任务提供有界并发，并暴露轻量运行状态。"""

    def __init__(self, capacity: int):
        self.capacity = max(1, capacity)
        self.active = 0
        self.waiting = 0
        self._semaphore = asyncio.Semaphore(self.capacity)

    @asynccontextmanager
    async def slot(self):
        self.waiting += 1
        try:
            await self._semaphore.acquire()
        except BaseException:
            self.waiting -= 1
            raise

        self.waiting -= 1
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1
            self._semaphore.release()


_generation_gate = _ConcurrencyGate(app_config.generation_max_concurrency)
_export_gate = _ConcurrencyGate(app_config.export_max_concurrency)

# ---------- 攻略缓存（SQLite 持久化，见 services/trip_store.py） ----------

def _clean_cache_sync():
    """清理过期缓存（攻略 + 携程问道查询缓存）"""
    from services import trip_store
    trip_store.clean_expired_guides(app_config.guide_cache_ttl)
    trip_store.clean_expired_wendao_cache(app_config.wendao_cache_ttl)


async def _clean_cache():
    # SQLite 遇到短暂写锁时可能等待，不能让它阻塞所有异步请求。
    await asyncio.to_thread(_clean_cache_sync)


# ---------- 健康检查 ----------
@app.get("/api/health")
async def health_check():
    import os
    from generator import _weasyprint_available
    from services import fliggy_flight
    ctrip_ready = bool(os.getenv("WENDAO_API_KEY", "").strip())
    return {
        "status": "ok",
        "llm_configured": llm_config.is_configured,
        "model": llm_config.model,
        "ctrip_ready": ctrip_ready,
        "pdf_ready": _weasyprint_available,
        "fliggy_ready": fliggy_flight.is_configured(),
        "generation": {
            "capacity": _generation_gate.capacity,
            "active": _generation_gate.active,
            "waiting": _generation_gate.waiting,
        },
    }


# ---------- 生成攻略（SSE 流式） ----------
@app.post("/api/generate")
async def generate_guide(request: Request):
    from orchestrator import TravelGuideOrchestrator, LLMClientError

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求内容必须是有效的 JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求内容必须是 JSON 对象")

    raw_query = body.get("query", "")
    if not isinstance(raw_query, str):
        raise HTTPException(status_code=400, detail="旅行需求必须是文本")
    query = raw_query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="请输入旅行需求")
    if len(query) > 2000:
        raise HTTPException(status_code=400, detail="旅行需求不能超过 2000 字")

    try:
        # 模型地址和密钥只能来自服务器环境变量。绝不接受公网请求覆盖，
        # 否则攻击者可把服务端 API Key 转发到其控制的地址。
        orchestrator = TravelGuideOrchestrator(
            base_url=llm_config.base_url,
            api_key=llm_config.api_key,
            model=llm_config.model,
        )
    except LLMClientError as e:
        raise HTTPException(status_code=400, detail=str(e))

    def _sse_line(text: str) -> str:
        # SSE 的 data 字段不允许包含裸换行，压成单行
        return " ".join(str(text).splitlines())

    async def event_stream():
        full_markdown = ""
        try:
            if _generation_gate.active >= _generation_gate.capacity:
                yield "event: progress\ndata: 当前生成任务较多，已进入队列等待...\n\n"

            async with _generation_gate.slot():
                async for event in orchestrator.generate(query):
                    if event["type"] == "content":
                        full_markdown += event["data"]
                        # SSE 多行格式：数据含 \n 时拆为多个 data: 行
                        data = event["data"]
                        if "\n" in data:
                            lines = "\n".join(f"data: {line}" for line in data.split("\n"))
                        else:
                            lines = f"data: {data}"
                        yield f"event: content\n{lines}\n\n"
                    elif event["type"] == "progress":
                        yield f"event: progress\ndata: {_sse_line(event['data'])}\n\n"
                    elif event["type"] == "error":
                        yield f"event: error\ndata: {_sse_line(event['data'])}\n\n"
                        return

                if not full_markdown.strip():
                    yield "event: error\ndata: 模型未返回有效内容，请重试\n\n"
                    return

                # 使用完整 UUID，避免高并发下 8 位短 ID 碰撞后覆盖其他人的报告。
                guid = uuid.uuid4().hex
                from generator import TravelGuideGenerator
                gen = TravelGuideGenerator(app_config.templates_dir)
                html_content = await asyncio.to_thread(gen.to_html, full_markdown, guid)

                await _clean_cache()
                from services import trip_store
                await asyncio.to_thread(
                    trip_store.save_guide, guid, html_content, full_markdown
                )

                # 前端通过下载地址加载 HTML，无需在 SSE 中重复传输整份文档。
                import json
                result_data = json.dumps({"guide_id": guid}, ensure_ascii=False)
                yield f"event: result\ndata: {result_data}\n\n"
                yield "event: done\ndata: {}\n\n"

        except Exception as e:
            yield f"event: error\ndata: {_sse_line(e)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------- 下载攻略 ----------
@app.get("/api/download/{guide_id}")
async def download_guide(guide_id: str, format: str = Query("html")):
    from services import trip_store
    await _clean_cache()
    guide = await asyncio.to_thread(trip_store.get_guide, guide_id)
    if not guide:
        raise HTTPException(status_code=404, detail="攻略已过期或不存在，请重新生成")

    from generator import TravelGuideGenerator
    gen = TravelGuideGenerator(app_config.templates_dir)

    if format == "pdf":
        try:
            # WeasyPrint 渲染是重 CPU 同步操作，放线程池避免阻塞事件循环
            async with _export_gate.slot():
                pdf_bytes = await asyncio.to_thread(gen.to_pdf, guide["html"], guide_id)
            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={"Content-Disposition": f"attachment; filename=travel-guide-{guide_id}.pdf"},
            )
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))

    elif format == "docx":
        async with _export_gate.slot():
            docx_bytes = await asyncio.to_thread(gen.to_docx, guide["markdown"], guide_id)
        return Response(
            content=docx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f"attachment; filename=travel-guide-{guide_id}.docx"},
        )

    elif format == "html":
        return HTMLResponse(content=guide["html"])

    raise HTTPException(status_code=400, detail="format 仅支持 html、pdf 或 docx")


# ---------- 行程管理 ----------
@app.post("/api/trips")
async def save_trip(request: Request):
    """保存行程到本地数据库

    若请求体缺 destination/days/travelers/budget，尝试从 markdown 和
    destination（原始查询文本）中自动解析，解析失败则保留默认值。
    """
    from services import trip_store
    body = await request.json()
    markdown = body.get("markdown", "")
    raw_destination = body.get("destination", "")

    destination = raw_destination
    days = body.get("days")
    travelers = body.get("travelers")
    budget = body.get("budget")

    if not destination or days is None or travelers is None or budget is None:
        parsed = trip_store.parse_trip_fields(raw_destination, markdown)
        if not destination:
            destination = parsed["destination"] or "未知目的地"
        if days is None:
            days = parsed["days"] if parsed["days"] is not None else 0
        if travelers is None:
            travelers = parsed["travelers"] if parsed["travelers"] is not None else 1
        if budget is None:
            budget = parsed["budget"] if parsed["budget"] is not None else 0

    trip_id = trip_store.save_trip(
        destination=destination or "未知目的地",
        markdown=markdown,
        origin=body.get("origin", ""),
        start_date=body.get("start_date", ""),
        end_date=body.get("end_date", ""),
        days=days,
        travelers=travelers,
        budget=budget,
        preferences=body.get("preferences", ""),
    )
    return {"trip_id": trip_id, "status": "saved"}


@app.get("/api/trips")
async def list_trips():
    """列出历史行程"""
    from services import trip_store
    trips = trip_store.list_trips()
    return {"trips": trips}


@app.get("/api/trips/{trip_id}")
async def get_trip(trip_id: str):
    """获取行程详情"""
    from services import trip_store
    trip = trip_store.get_trip(trip_id)
    if not trip:
        raise HTTPException(status_code=404, detail="行程不存在")
    return trip


@app.get("/api/trips/{trip_id}/view")
async def view_trip(trip_id: str):
    """将保存的行程 markdown 渲染为 HTML 并返回"""
    from services import trip_store
    trip = trip_store.get_trip(trip_id)
    if not trip:
        raise HTTPException(status_code=404, detail="行程不存在")

    from generator import TravelGuideGenerator
    gen = TravelGuideGenerator(app_config.templates_dir)
    # to_html 含路线图同步 HTTP 调用，放线程池避免阻塞事件循环
    html_content = await asyncio.to_thread(gen.to_html, trip.get("markdown") or "", trip_id)
    return HTMLResponse(content=html_content)


@app.delete("/api/trips/{trip_id}")
async def delete_trip(trip_id: str):
    """删除行程"""
    from services import trip_store
    if trip_store.delete_trip(trip_id):
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="行程不存在")


def _validate_date(date: str) -> None:
    """严格校验日期：格式 + 语义（2026-13-99 之类拒绝），strptime 顺带杜绝尾部换行"""
    from datetime import datetime
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="date 格式应为 YYYY-MM-DD 的有效日期")


# ---------- 12306 火车票查询 ----------
@app.get("/api/train/tickets")
async def train_tickets(
    from_station: str = Query(""),
    to_station: str = Query(""),
    date: str = Query(""),
):
    """12306 余票查询"""
    if not from_station.strip() or not to_station.strip() or not date.strip():
        raise HTTPException(status_code=400, detail="from_station、to_station、date 均为必填参数")
    _validate_date(date)

    from services import train_service
    result = await asyncio.to_thread(train_service.query_tickets, from_station, to_station, date)
    text = await asyncio.to_thread(train_service.format_ticket_result, result)
    return {"data": text}


# ---------- 国际机票查询 ----------
@app.get("/api/flights/search")
async def flights_search(
    origin: str = Query(""),
    destination: str = Query(""),
    date: str = Query(""),
    nonstop: bool = Query(False),
    passengers: int = Query(1),
):
    """国际机票查询（Google Flights，经 fast-flights）"""
    if not origin.strip() or not destination.strip() or not date.strip():
        raise HTTPException(status_code=400, detail="origin、destination、date 均为必填参数")
    _validate_date(date)

    from services import flight_search
    text = await asyncio.to_thread(
        flight_search.search_flights, origin, destination, date, nonstop, passengers
    )
    return {"data": text}


# ---------- 实时服务 API ----------
@app.get("/api/flight/track")
async def track_flight(
    airport: str = Query(""),
    callsign: str = Query(""),
    date: str = Query(""),
):
    """实时航班追踪

    按航班号查询时优先走飞猪航班动态（需配置 FLIGGY_APP_KEY/SECRET，国内可达），
    未配置或按机场查询时回退 OpenSky。date 为可选起飞日期 YYYY-MM-DD（仅飞猪支持）。
    """
    from services import flight_tracker, fliggy_flight
    if date:
        _validate_date(date)

    if callsign:
        if fliggy_flight.is_configured():
            result = await fliggy_flight.query_flight_dynamic(
                flight_no=callsign, flight_date=date
            )
        else:
            result = await flight_tracker.track_by_callsign(callsign)
    elif airport:
        result = await flight_tracker.track_by_airport(airport)
    else:
        raise HTTPException(status_code=400, detail="请提供 airport 或 callsign 参数")
    return {"data": result}


@app.get("/api/weather/aviation")
async def aviation_weather(airport: str = Query(...)):
    """航空气象查询"""
    from services import weather_service
    metar = await weather_service.get_metar(airport)
    taf = await weather_service.get_taf(airport)
    return {"metar": metar, "taf": taf}


# ---------- 挂载静态文件 ----------
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ---------- 首页 ----------
@app.get("/")
async def index():
    index_path = static_dir / "index.html"
    if index_path.exists():
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>AI 旅行攻略生成器</h1><p>前端页面尚未创建。</p>")


@app.get("/info")
async def info_page():
    info_path = static_dir / "info.html"
    if info_path.exists():
        return HTMLResponse(content=info_path.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="信息页不存在")


# ---------- 启动入口 ----------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=app_config.host, port=app_config.port)
