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
        self.max_tokens = int(os.getenv("LLM_MAX_TOKENS", "8192"))
        self.temperature = float(os.getenv("LLM_TEMPERATURE", "0.7"))

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
