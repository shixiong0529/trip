# 路小仙 · AI 旅行攻略生成器

输入目的地和旅行偏好，约 2 分钟生成详细行程。路小仙基于携程问道、高德 Web 服务、12306 等实时数据与 DeepSeek AI 编排，生成可直接照着走的旅行攻略，支持 HTML / PDF / DOCX 三格式下载。

## 功能

- **对话式输入** — 自然语言描述需求，无需填表单。如"武汉出发自驾西藏15天，2人，预算15000元"
- **多源数据** — 携程问道查询机票/酒店/景点门票，高德补充定位/POI/天气/路线，12306 补充火车余票参考，统一注入 LLM 上下文
- **结构化输出** — 概览统计、天气穿搭、交通、住宿、逐日行程表、预算拆解、预约清单、避坑提示、行前物品、知识图谱
- **多格式下载** — 网页预览（精美 HTML 样式） + PDF 下载 + DOCX 下载
- **行程记忆** — SQLite 本地存储历史行程，支持查看、删除
- **并发保护** — 多人同时提交时按服务器容量并行生成，超出容量的任务自动排队，报告数据彼此隔离
- **无需登录** — 打开即用，API Key 仅配置在服务端 `.env`，不会发送或保存在浏览器中

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.10+ + FastAPI + uvicorn |
| LLM | DeepSeek API（兼容 OpenAI 接口） |
| 实时数据 | 携程问道 API + 高德 Web 服务 + 12306 |
| 文档生成 | Jinja2 + WeasyPrint (PDF) + python-docx (DOCX) |
| 存储 | SQLite（行程记忆 + 攻略缓存，默认保留 24 小时） |
| 前端 | 纯 HTML/CSS/JS（零依赖） |
| 部署 | 本地运行 / Linux + systemd + Nginx |

## 快速开始

### 1. 配置环境

```bash
cp .env.example .env
# 编辑 .env，填入 DeepSeek API Key
vim .env
```

```ini
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-your-key-here
LLM_MODEL=deepseek-chat

# 同时生成报告的最大数量，超出的任务自动排队
GENERATION_MAX_CONCURRENCY=2

# PDF/DOCX 转换较耗 CPU 和内存，建议保持为 1
EXPORT_MAX_CONCURRENCY=1
```

> 携程问道 API Key 通过环境变量 `WENDAO_API_KEY` 注入。如果已在 WorkBuddy 连接器中配置，无需额外操作。高德 Web 服务 Key 可填入 `AMAP_WEB_SERVICE_KEY`，用于目的地定位、POI、天气和路线距离参考。

模型地址、模型名称和 API Key 只从服务器 `.env` 读取。`POST /api/generate` 不接受客户端覆盖这些配置，避免服务端密钥被转发到非预期地址。

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 启动

```bash
./start.sh
```

或手动启动：

```bash
python app.py
```

打开 `http://localhost:8080`

### PDF 支持

PDF 生成依赖 WeasyPrint，需要系统级库：

```bash
# macOS
brew install pango cairo glib

# Ubuntu/Debian
apt install -y libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgobject-2.0-0 libgdk-pixbuf-2.0-0 shared-mime-info fontconfig fonts-noto-cjk
```

如果系统库不可用，PDF 功能会禁用，HTML 和 DOCX 不受影响。

### 部署与 CORS

跨域部署（前端与后端不同源，如反向代理/CDN 分离场景）时，需通过环境变量 `ALLOWED_ORIGINS` 显式声明允许的来源（逗号分隔，如 `ALLOWED_ORIGINS=https://your-domain.com,https://admin.your-domain.com`）；未配置时默认仅允许 `http://localhost:{PORT}` 和 `http://127.0.0.1:{PORT}` 访问，本地同源前端不受影响。

### 并发与排队

- `GENERATION_MAX_CONCURRENCY` 控制同时执行的数据采集、LLM 生成和 HTML 渲染任务数，默认 `2`。超出的请求保持 SSE 连接并显示排队提示。
- `EXPORT_MAX_CONCURRENCY` 控制同时执行的 PDF/DOCX 转换数，默认 `1`，用于避免 WeasyPrint 在小内存服务器上并发占用过多资源。
- 每份报告使用独立的 128 位 UUID（32 个十六进制字符），并持久化到 SQLite；SQLite 已启用 WAL 和写锁等待，避免并发提交时报告覆盖、丢失或串线。
- 以上并发上限按单个 uvicorn 进程计算。当前 `python app.py` 和 systemd 部署均为单进程模式。

阿里云小内存实例建议保持默认值。如果服务器资源充足，可在 `/opt/trip/.env` 调高后重启 `trip` 服务。

### 阿里云部署

线上部署使用 systemd 常驻运行、Nginx 反向代理、`acme.sh + DNS-01` 自动续期证书。详细步骤见 [DEPLOYMENT.md](./DEPLOYMENT.md)。

服务器更新：

```bash
cd /opt/trip
git pull origin master
source .venv/bin/activate
pip install -r requirements.txt
systemctl restart trip
sleep 5
systemctl status trip --no-pager | head -30
curl https://trip.moyu.in/api/health
```

## API

| 接口 | 说明 |
|------|------|
| `GET /` | 首页（SPA） |
| `GET /api/health` | 健康检查，返回模型、外部服务和生成队列状态 |
| `POST /api/generate` | 生成攻略（SSE 流式），请求体 `{"query": "..."}`，最多 2000 字 |
| `GET /api/download/{guide_id}` | 下载攻略，`?format=pdf\|docx\|html` |
| `POST /api/trips` | 保存行程 |
| `GET /api/trips` | 列出历史行程 |
| `GET /api/trips/{id}` | 行程详情 |
| `DELETE /api/trips/{id}` | 删除行程 |
| `GET /api/flight/track` | 航班实时追踪，`?callsign=MU5100&date=YYYY-MM-DD` 或 `?airport=ZBAA`。按航班号查询时优先飞猪航班动态（需配置 `FLIGGY_APP_KEY/SECRET`），未配置回退 OpenSky |
| `GET /api/weather/aviation` | 航空气象 METAR/TAF |
| `GET /api/train/tickets` | 12306 余票查询，`?from_station&to_station&date` |
| `GET /api/flights/search` | 国际机票查询（Google Flights），`?origin&destination&date&nonstop&passengers` |

健康接口中的生成队列状态示例：

```json
{
  "status": "ok",
  "model": "deepseek-v4-flash",
  "generation": {
    "capacity": 2,
    "active": 1,
    "waiting": 3
  }
}
```

`capacity` 是并发上限，`active` 是正在生成的数量，`waiting` 是排队数量。

## 架构

```
用户浏览器
    │
    ▼
FastAPI 服务 (app.py)
    ├── 并发闸门             ← 生成任务排队 + PDF/DOCX 转换限流
    ├── orchestrator.py     ← AI 编排层（数据采集 → LLM 生成 → 流式输出）
    ├── prompts.py          ← System Prompt（角色、SOP、输出格式、安全约束）
    ├── generator.py        ← 文档生成（Markdown → HTML/PDF/DOCX）
    ├── config.py           ← 配置管理
    ├── services/
    │   ├── ctrip_client.py      ← 携程问道 API
    │   ├── data_collector.py    ← 多源并行数据采集
    │   ├── amap_client.py       ← 高德 Web 服务：定位/POI/天气/路线
    │   ├── flight_search.py     ← Google Flights 国际机票
    │   ├── flight_tracker.py    ← OpenSky 航班追踪
    │   ├── train_service.py     ← 12306 查票
    │   ├── weather_service.py   ← 航空气象
    │   └── trip_store.py        ← SQLite 行程记忆
    ├── static/              ← 前端（SPA）
    └── templates/           ← Jinja2 攻略 HTML 模板
```

## 数据流

```
用户输入 "西藏15日自驾"
    │
    ▼
生成并发闸门（有空闲槽位则执行，否则排队）
    │
    ▼
多源接口并行采集
  ├── 交通数据（机票/火车票）
  ├── 酒店推荐
  ├── 景点门票
  └── 实用贴士
    │
    ▼
System Prompt 注入实时数据 → DeepSeek 生成 Markdown
    │
    ▼
Markdown 解析 → Jinja2 渲染 → 精美 HTML
    │
    ▼
报告写入 SQLite → 浏览器通过独立下载地址加载 HTML
    │
    ▼
用户浏览 + 下载（PDF/DOCX 转换单独限流）
    │
    ▼
可选保存到 SQLite 行程记忆
```

## 依赖

```
fastapi
uvicorn
jinja2
httpx
python-dotenv
weasyprint       ← PDF 生成（需系统库）
python-docx      ← DOCX 生成
requests         ← 12306 查票客户端依赖
```

## 测试

安装开发依赖并运行完整测试：

```bash
pip install -r requirements-dev.txt
pytest -q
```

测试覆盖生成接口、SSE 结果、多人并发上限、报告隔离、SQLite 并发写入以及 HTML/PDF/DOCX 相关逻辑；测试数据库使用临时目录，不会修改本地 `travel_data.db`。

## 项目规划

详细架构设计、TripStar Agent 对齐状态、迭代历史、已知限制 → [PROJECT_PLAN.md](./PROJECT_PLAN.md)

## 许可

MIT
