# AI 旅行攻略生成器 — 项目最终状态

> 启动方案 (2026-07-09) → MVP → TripStar 全功能补齐 → GLM 审查修复 → 当前版本
> 项目目录: `/Users/shixiong/Developer/trip`

## 一、整体架构

### 1.1 技术选型

| 层次 | 技术 | 说明 |
|------|------|------|
| 前端 | 纯 HTML/CSS/JS (Vanilla) | 零依赖 SPA |
| 后端 | Python 3.13 + FastAPI + uvicorn | 异步高性能 |
| LLM | DeepSeek API (OpenAI 兼容) | deepseek-chat, max_tokens=8192 |
| 实时数据 | 携程问道 API | 机票/酒店/景点/贴士四路并行采集 |
| 模板 | Jinja2 + HTML Report Generator 设计系统 | day-card 组件、Hero 区、stats-grid |
| PDF | WeasyPrint（需系统库 pango/cairo/glib） | 不可用时优雅降级 |
| DOCX | python-docx | 原生生成 |
| 存储 | SQLite（行程记忆）+ 内存缓存（攻略 1h TTL） | 无需数据库服务 |
| 环境 | .env + python-dotenv | API key/base_url 从环境变量读取 |

### 1.2 架构分层

```
用户浏览器（SSE 流式）
    │
    ▼
FastAPI (app.py)
    ├── orchestrator.py      ← 两阶段生成（数据采集 → LLM）
    ├── prompts.py           ← System Prompt v4（TripStar SOP + GLM 修复 + 安全约束）
    ├── generator.py         ← Markdown → HTML/PDF/DOCX
    ├── config.py
    ├── services/
    │   ├── ctrip_client.py       ← 携程问道 API 封装
    │   ├── data_collector.py     ← 四路并行采集
    │   ├── flight_search.py      ← Google Flights 国际机票
    │   ├── flight_tracker.py     ← OpenSky 实时航班
    │   ├── train_service.py      ← 12306 查票（嵌入 4679 行 client.py）
    │   ├── weather_service.py    ← FAA METAR/TAF 航空气象
    │   ├── trip_store.py         ← SQLite 行程 CRUD
    │   └── 12306_client.py       ← TripStar 原始客户端
    ├── static/              ← 前端（index.html + app.js + style.css）
    └── templates/
        ├── guide.html            ← 攻略 HTML 模板（日报设计系统）
        └── html-report-design-system.md ← 设计参考
```

### 1.3 TripStar Agent 9 个 Skill 对齐状态

| TripStar 原 Skill | 本项目实现 | 状态 |
|---|---|---|
| flyai | 携程问道 API（80%覆盖） | ✅ |
| 12306-train-assistant | train_service.py（查票，不下单） | ✅ |
| flights-search | flight_search.py（fast-flights） | ✅ |
| flight-tracker | flight_tracker.py（OpenSky） | ✅ |
| aviation-weather | weather_service.py（FAA） | ✅ |
| globepilot | System Prompt 出境指南 | ✅ |
| travel-planning | trip_store.py（SQLite） | ✅ |
| airbnb | 跳过（携程酒店覆盖） | — |
| meituan-coupon | 跳过（需企业资质） | — |

## 二、完成的迭代

| 版本 | 内容 |
|------|------|
| v1.0 MVP | FastAPI + DeepSeek + 基础 HTML 生成 |
| v2.0 | 携程问道数据管道 + 7 个 services + 12306/lib |
| v3.0 | System Prompt 对齐 TripStar（emoji 标题、示例行、写作原则、收尾互动） |
| v4.0 | GLM 审查 14 个问题修复（安全驾驶硬约束、事实准确、数据一致、禁 COVID 用语） |
| v4.1 | HTML Report Generator 设计系统（Hero/stats/day-card/tree 组件） |
| v4.2 | SSE 缓冲聚合修复（解决内容截断导致后续 section 丢失） |
| v4.3 | 行程记忆 + token 预算约束 |

## 三、关键设计决策

1. **Markdown → HTML** — LLM 输出结构化 Markdown，后端 Jinja2 渲染。样式统一可控
2. **SSE 流式** — 前端实时展示生成进度，携程数据采集步骤可视化
3. **不登录** — 无需注册，API Key 可配在 .env 或浏览器 localStorage
4. **内存缓存** — 攻略 UUID 索引，1 小时 TTL，服务重启丢失
5. **两阶段生成** — 先并行采集携程数据，再注入 LLM 上下文生成
6. **PDF 降级** — WeasyPrint 不可用时返回明确错误，HTML/DOCX 不受影响

## 四、已知限制

| 限制 | 说明 |
|------|------|
| max_tokens=8192 | 15 天以上行程知识图谱可能截断，已通过 Prompt 精简策略缓解 |
| PDF 依赖系统库 | macOS/Linux 需额外安装 pango/cairo/glib |
| LLM 幻觉 | 事实准确性依赖 Prompt 约束，无法完全消除。关键信息标注"请核实" |
| 12306 不下单 | 风控 + 支付复杂度高，仅做查票 |
| 数据一致性 | LLM 不擅长精确求和，预算/里程可能有偏差 |

## 五、文件清单

```
trip/
├── app.py                    ← FastAPI 主入口
├── orchestrator.py           ← AI 编排层（缓冲聚合 + 两阶段生成）
├── prompts.py                ← System Prompt v4（TripStar SOP + GLM 修复）
├── generator.py              ← 文档生成（Hero/stats/day-card/tree 组件）
├── config.py                 ← 配置管理
├── requirements.txt          ← Python 依赖
├── .env.example              ← 环境变量模板
├── .env                      ← 实际配置（不入版本控制）
├── start.sh                  ← 一键启动脚本
├── README.md                 ← 项目文档
├── development-plan.md       ← 初始开发方案
├── services/
│   ├── __init__.py
│   ├── ctrip_client.py       ← 携程问道 API
│   ├── data_collector.py     ← 并行采集
│   ├── flight_search.py      ← Google Flights
│   ├── flight_tracker.py     ← OpenSky
│   ├── train_service.py      ← 12306 wrapper
│   ├── weather_service.py    ← METAR/TAF
│   ├── trip_store.py         ← SQLite 存储
│   └── 12306_client.py       ← TripStar 原始客户端（4679 行）
├── static/
│   ├── index.html            ← SPA 主页面
│   ├── style.css             ← 全局样式
│   └── app.js                ← 前端逻辑（SSE/下载/配置/行程管理）
├── templates/
│   ├── guide.html            ← 攻略 HTML 模板
│   └── html-report-design-system.md ← 设计参考
└── travel_data.db            ← SQLite 数据库（运行时生成）
```
