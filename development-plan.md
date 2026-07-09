# TripStar Agent 全功能实现 · 开发步骤方案

## 资源盘点

| TripStar 原 Skill | 我们的替代方案 | 难度 | 备注 |
|---|---|---|---|
| flyai（飞猪一站式搜索） | 携程问道 API（已连接） | 低 | 覆盖机票/酒店/景点/火车票 80% 能力 |
| 12306-train-assistant（高铁） | 嵌入 client.py（查票功能） | 中 | 不下单，只做余票+经停站+中转查询 |
| flights-search（国际机票） | fast-flights Python 库 | 低 | 国际机票比价，无需 API Key |
| flight-tracker（航班追踪） | OpenSky REST API | 低 | 免费 400次/天 |
| aviation-weather（航空气象） | FAA aviationweather.gov API | 低 | 无需认证 |
| globepilot（出境信息） | System Prompt + WebSearch | 低 | 签证/货币/文化/紧急联系 |
| travel-planning（行程记忆） | SQLite 本地数据库 | 中 | 替代文件系统 Markdown |
| airbnb（民宿搜索） | **跳过**（一期） | — | 国内出行使用携程酒店覆盖 |
| meituan-coupon（优惠券） | **跳过** | — | 需要美团商业合作伙伴资质 |

---

## 方案架构：五层数据管道

```
┌─────────────────────────────────────────────────────────────┐
│                    前端 (已有)                                │
│              Chat 输入 → SSE 流式 → 攻略展示                  │
└──────────────────────────┬──────────────────────────────────┘
                           │ POST /api/generate
┌──────────────────────────▼──────────────────────────────────┐
│               Orchestrator 编排器 (升级)                      │
│                                                              │
│  Phase 1: 需求解析 → 从用户输入提取 [目的地/天数/偏好/...]    │
│  Phase 2: 并行数据采集 (8路并发)                              │
│           ├─ ctrip_wendao("机票: {出发地}→{目的地}")          │
│           ├─ ctrip_wendao("酒店: {目的地} {区域}")            │
│           ├─ ctrip_wendao("景点: {目的地} 门票")              │
│           ├─ ctrip_wendao("火车: {出发地}→{目的地}")          │
│           ├─ train_12306("余票: {出发地}→{目的地}")           │
│           ├─ google_flights("{出发地}→{目的地}")   [国际]      │
│           ├─ opensky_track("{机场代码}")            [可选]     │
│           └─ aviation_weather("{机场代码}")         [可选]     │
│  Phase 3: 数据聚合 → 拼接为 LLM System Prompt 上下文          │
│  Phase 4: LLM 生成 → 基于实时数据生成结构化攻略                │
│  Phase 5: 输出 → Markdown → HTML/PDF/DOCX                     │
└──────────────────────────────────────────────────────────────┘
```

---

## 开发步骤（5 个阶段，11 个步骤）

### 阶段一：携程问道数据管道（Step 1-2）

#### Step 1：ctrip_client.py — 携程问道 API 客户端
- 新建 `services/ctrip_client.py`
- 封装异步调用携程问道 API：POST https://externalcallback.ctrip.com/skills/api/crew/qclaw/searchInfo
- 支持多 Query 并行请求（asyncio.gather）
- 每次请求返回纯文本 Markdown 结果
- Token 从环境变量 WENDAO_API_KEY 读取
- 预期代码量：~60 行

#### Step 2：data_collector.py — 并行数据采集器
- 新建 `services/data_collector.py`
- 根据用户输入智能拆分为多个查询（机票/酒店/景点/火车票）
- 并行调用携程问道，收集所有实时数据
- 返回结构化数据字典：{ "flights": "...", "hotels": "...", "attractions": "...", "trains": "..." }
- 预期代码量：~80 行

---

### 阶段二：专项数据服务（Step 3-5）

#### Step 3：train_service.py — 12306 高铁查票
- 新建 `services/train_service.py`
- 将 TripStar 的 client.py 嵌入（复制到 services/12306-client.py）
- 只暴露查询接口：余票查询(left-ticket)、经停站(route)、中转换乘(transfer-ticket)
- 不实现登录/下单/支付（太复杂且安全风险高）
- 输入：出发站/到达站/日期 → 输出：车次/座位类型/余票状态
- 预期代码量：~120 行（wrapper 层）

#### Step 4：flight_search.py — 国际机票比价
- 新建 `services/flight_search.py`
- 调用 fast-flights Python 库（Google Flights 数据）
- 支持：多机场自动搜索、直飞筛选、时段过滤、舱位选择
- 输入：出发城市/到达城市/日期 → 输出：航班/价格/航司/链接
- 预期代码量：~50 行

#### Step 5：flight_tracker.py + weather_service.py — 航班追踪与气象
- 新建 `services/flight_tracker.py`
  - 调用 OpenSky Network REST API
  - 输入：机场代码或呼号 → 输出：实时位置/速度/高度
  - 预期代码量：~40 行
- 新建 `services/weather_service.py`
  - 调用 FAA aviationweather.gov API
  - 输入：机场代码 → 输出：METAR/TAF 数据
  - 预期代码量：~40 行

---

### 阶段三：行程记忆系统（Step 6-7）

#### Step 6：trip_store.py — SQLite 行程记忆
- 新建 `services/trip_store.py`
- 使用 Python 内置 sqlite3，零额外依赖
- 数据模型（4 张表）：
  - trips: 行程元数据（目的地/日期/天数/人数/预算/状态）
  - trip_items: 行程条目（Day编号/时段/活动/耗时/费用/备注）
  - user_prefs: 用户偏好（旅行风格/预算偏好/常驻城市）
  - packing_lists: 打包清单模板
- 参照 travel-planning 的 Markdown 文件结构设计 Schema
- 提供完整的 CRUD API
- 预期代码量：~200 行

#### Step 7：行程管理 API 路由
- 在 app.py 中新增路由组：
  - POST /api/trips — 保存行程
  - GET /api/trips — 列出历史行程
  - GET /api/trips/{id} — 查看行程详情
  - DELETE /api/trips/{id} — 删除行程
- 前端增加"我的行程"tab
- 预期代码量：~80 行（路由） + ~100 行（前端）

---

### 阶段四：AI 编排升级（Step 8-9）

#### Step 8：orchestrator 升级为 Function Calling 架构
- 改造 `orchestrator.py`
- 新增"两阶段生成"模式：
  - 阶段 1：需求解析 → 提取关键参数（目的地/天数/出发地/预算）
  - 阶段 2：并行数据采集 → 调用 data_collector
  - 阶段 3：LLM 生成 → System Prompt 注入实时数据上下文
- 支持技能路由（国内机票用携程、国际用 Google Flights、高铁用 12306）
- 保留原有的 SSE 流式输出
- 预期代码量：~150 行（重写约 60%）

#### Step 9：prompts.py 全面升级
- 融入 trip-planner.md 的完整 SOP（327行→精简为 ~200行 System Prompt）
- 新增"实时数据上下文"占位符：`{ctrip_flights} {ctrip_hotels} {ctrip_attractions} {train_data}`
- 升级输出格式规范（纳入所有 TripStar 的模板字段）
- 新增数据标注规则："{数据来源：携程实时查询}" vs "{数据来源：AI推算}"
- 新增出境游引导（签证/货币/文化/紧急联系）
- 预期代码量：~250 行（重写 prompts.py）

---

### 阶段五：前端增强与收尾（Step 10-11）

#### Step 10：前端 UI 升级
- **生成进度展示**：显示"正在查询携程酒店数据...""正在查询高铁余票..."等步骤
- **数据来源标注**：攻略中标注"携程实时价" vs "AI估算"
- **我的行程页**：简单的历史行程列表 + 详情查看
- **下载增强**：PDF 支持分页（WeasyPrint），DOCX 支持表格样式
- 预期代码量：~200 行

#### Step 11：PDF 修复与端到端测试
- 解决 WeasyPrint 系统依赖（brew install pango cairo glib）
- 或改用替代方案：pdfkit + wkhtmltopdf
- 完整端到端测试：输入 → 携程查数据 → LLM生成 → 展示 → HTML/PDF/DOCX下载 → 保存行程
- 预期代码量：~30 行

---

## 文件变更清单

```
新增文件（7个）：
  services/__init__.py
  services/ctrip_client.py        # 携程问道 API 封装
  services/data_collector.py      # 并行数据采集器
  services/train_service.py       # 12306 查票
  services/flight_search.py       # Google Flights 国际比价
  services/flight_tracker.py      # OpenSky 航班追踪
  services/weather_service.py     # 航空气象
  services/trip_store.py          # SQLite 行程记忆
  services/12306-client.py        # 从 TripStar 复制的原始客户端

修改文件（5个）：
  app.py                          # 新增路由 + 数据服务集成
  orchestrator.py                 # 重写为两阶段生成 + Function Calling
  prompts.py                      # 全面升级 System Prompt
  static/app.js                   # 前端增强
  static/index.html               # "我的行程"Tab

保持不变（5个）：
  config.py
  generator.py
  static/style.css（微调）
  templates/guide.html（微调）
  start.sh（更新依赖列表）
```

---

## 代码量估算

| 阶段 | 新增 | 修改 | 合计 |
|------|------|------|------|
| 阶段一（携程管道） | ~140行 | 0 | ~140行 |
| 阶段二（专项服务） | ~250行 | ~120行（12306拷贝） | ~370行 |
| 阶段三（行程记忆） | ~280行 | ~180行 | ~460行 |
| 阶段四（AI编排） | 0 | ~400行 | ~400行 |
| 阶段五（前端+测试） | ~200行 | ~30行 | ~230行 |
| **总计** | **~870行** | **~730行** | **~1600行** |

---

## 执行顺序与依赖关系

```
Step 1 (ctrip_client) ──┬──→ Step 2 (data_collector)
                         │
Step 3 (train_service) ──┤
Step 4 (flight_search) ──┼──→ Step 8 (orchestrator 升级)
Step 5 (tracker+weather) ─┤                         │
                         │                          ↓
Step 6 (trip_store) ─────┤                    Step 9 (prompts 升级)
                         │                          │
                         │                          ↓
Step 7 (trip API) ───────┘                    Step 10 (前端增强)
                                                   │
                                                   ↓
                                             Step 11 (测试收尾)
```

Step 1-5 完全独立，可并行开发。Step 6-7 独立于其他步骤。Step 8-9 依赖 Step 1-7 的接口。Step 10-11 依赖 Step 8-9。
