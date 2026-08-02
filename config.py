"""
配置管理模块
读取环境变量，提供 LLM 和服务配置对象
"""

import os
from dotenv import load_dotenv

load_dotenv()


class LLMConfig:
    """LLM API 配置"""

    def __init__(self):
        self.base_url = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.model = os.getenv("LLM_MODEL", "deepseek-chat")
        # 16384:实测 deepseek-v4 接受;上限给足可避免 12 天以上长行程输出被
        # 8192 截断后触发自动续写(续写会让生成总时长接近翻倍)。
        # max_tokens 只是截断上限,按实际生成量计费,调大无额外成本
        self.max_tokens = int(os.getenv("LLM_MAX_TOKENS", "16384"))
        # 途经点抽取、停留天分配等结构化小任务用的快速模型；
        # 未配置时退回主模型。攻略正文始终用主模型生成
        self.fast_model = os.getenv("LLM_FAST_MODEL", "").strip() or self.model
        # 结构化长报告优先保证格式稳定；较低温度减少标题、表格和代码块的
        # 随机变体，内容差异由用户需求和实时数据决定。
        self.temperature = float(os.getenv("LLM_TEMPERATURE", "0.2"))
        # 正文流的业务级时限。网关可能在 HTTP 连接正常时长时间不返回任何
        # token，单靠 httpx read timeout 会让页面看似无限等待。
        self.first_token_timeout = max(
            5.0, float(os.getenv("LLM_FIRST_TOKEN_TIMEOUT", "45"))
        )
        self.stream_timeout = max(
            self.first_token_timeout + 5.0,
            float(os.getenv("LLM_STREAM_TIMEOUT", "75")),
        )
        # 专业版上下文和输出明显更长，使用独立时限；期间仍会每 10 秒推送进度。
        self.professional_first_token_timeout = max(
            self.first_token_timeout,
            float(os.getenv("LLM_PRO_FIRST_TOKEN_TIMEOUT", "75")),
        )
        self.professional_stream_timeout = max(
            self.professional_first_token_timeout + 5.0,
            float(os.getenv("LLM_PRO_STREAM_TIMEOUT", "180")),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_key != "your-deepseek-api-key-here")


class AppConfig:
    """应用配置"""

    def __init__(self):
        self.host = os.getenv("HOST", "0.0.0.0")
        self.port = int(os.getenv("PORT", "8080"))
        self.static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
        self.templates_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
        self.guide_cache_ttl = int(os.getenv("GUIDE_CACHE_TTL", "86400"))  # 攻略缓存有效期（秒），默认 24 小时
        self.wendao_cache_ttl = int(os.getenv("WENDAO_CACHE_TTL", "43200"))  # 携程问道查询缓存有效期（秒），默认 12 小时
        self.route_plan_cache_ttl = int(os.getenv("ROUTE_PLAN_CACHE_TTL", "7200"))
        # 限制昂贵的长耗时任务，超出的请求在服务端排队，避免多人同时提交时
        # 打满模型/外部数据接口和线程池。至少保留 1 个并发槽位。
        self.generation_max_concurrency = max(
            1, int(os.getenv("GENERATION_MAX_CONCURRENCY", "2"))
        )
        self.export_max_concurrency = max(
            1, int(os.getenv("EXPORT_MAX_CONCURRENCY", "1"))
        )

        # 专业版报告在发布前使用主模型进行独立审核，并在发现主要问题时
        # 最多自动修复一次。标准版不进入该流程，因而不会增加标准版耗时。
        review_mode = os.getenv("PRO_REVIEW_MODE", "repair").strip().lower()
        if review_mode not in {"off", "shadow", "audit", "repair"}:
            review_mode = "repair"
        self.pro_review_mode = review_mode
        self.pro_review_timeout = max(
            10.0, float(os.getenv("PRO_REVIEW_TIMEOUT", "60"))
        )
        self.pro_review_max_tokens = max(
            256, int(os.getenv("PRO_REVIEW_MAX_TOKENS", "2500"))
        )
        self.pro_review_total_timeout = max(
            self.pro_review_timeout,
            float(os.getenv("PRO_REVIEW_TOTAL_TIMEOUT", "420")),
        )
        # 当前修复流程只允许重写一次，避免模型在审核与重写之间形成循环。
        self.pro_rewrite_max_attempts = min(
            1, max(0, int(os.getenv("PRO_REWRITE_MAX_ATTEMPTS", "1")))
        )

        # CORS 允许的来源，逗号分隔；未配置时默认仅允许本机同端口访问（本地前端为同源，不受 CORS 影响）
        origins_env = os.getenv("ALLOWED_ORIGINS", "").strip()
        if origins_env:
            self.allowed_origins = [o.strip() for o in origins_env.split(",") if o.strip()]
        else:
            self.allowed_origins = [
                f"http://localhost:{self.port}",
                f"http://127.0.0.1:{self.port}",
            ]


llm_config = LLMConfig()
app_config = AppConfig()
