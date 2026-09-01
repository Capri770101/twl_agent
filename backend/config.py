"""配置 —— 从 .env 读取，所有可调项集中在此。"""

from __future__ import annotations
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── 服务 ──
    APP_ENV: str = "dev"
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # ── 数据库 ──
    DATABASE_URL: str = ""  # 留空用 SQLite
    DB_PATH: str = str(BASE_DIR / "data" / "agent.db")

    # ── LLM ──
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_MAX_ITERATIONS: int = 8
    LLM_REQUEST_TIMEOUT: float = 120.0
    LLM_TEMPERATURE: float = 0.3

    # ── 图像生成 ──
    IMAGE_PROVIDER: str = "mock"  # mock / flux / dall-e / kling / comfyui
    IMAGE_API_KEY: str = ""
    IMAGE_BASE_URL: str = ""
    IMAGE_MODEL: str = ""
    IMAGE_WIDTH: int = 768
    IMAGE_HEIGHT: int = 1024

    # ── 微信小程序 ──
    WECHAT_APPID: str = ""
    WECHAT_SECRET: str = ""

    # ── 腾讯地图（可选）──
    TENCENT_MAP_KEY: str = ""

    # ── Redis（可选）──
    REDIS_URL: str = ""

    # ── CORS ──
    ALLOWED_ORIGINS: str = "*"

    # ── 智能体参数 ──
    MAX_ITERATIONS: int = 8
    HISTORY_LIMIT: int = 20
    REQUEST_TIMEOUT: float = 180.0

    # 兼容 agent 源码里的小写属性访问
    @property
    def app_env(self) -> str:
        return self.APP_ENV

    @property
    def history_limit(self) -> int:
        return self.HISTORY_LIMIT

    @property
    def max_iterations(self) -> int:
        return self.MAX_ITERATIONS

    @property
    def request_timeout(self) -> float:
        return self.REQUEST_TIMEOUT

    @property
    def rag_enabled(self) -> bool:
        return True

    @property
    def rag_keyword_boost(self) -> float:
        return 0.35

    @property
    def rag_min_score(self) -> float:
        return 0.18

    @property
    def rag_top_k(self) -> int:
        return 8

    @property
    def llm_base_url(self) -> str:
        return self.LLM_BASE_URL

    @property
    def llm_api_key(self) -> str:
        return self.LLM_API_KEY

    @property
    def llm_model(self) -> str:
        return self.LLM_MODEL

    @property
    def llm_providers(self) -> str:
        return ""

    @property
    def llm_circuit_breaker_enabled(self) -> bool:
        return True

    @property
    def llm_temperature(self) -> float:
        return self.LLM_TEMPERATURE

    @property
    def llm_max_tokens(self) -> int:
        return 1500

    @property
    def llm_timeout(self) -> float:
        return self.LLM_REQUEST_TIMEOUT

    @property
    def llm_cb_failure_threshold(self) -> int:
        return 5

    @property
    def llm_cb_open_seconds(self) -> float:
        return 30.0

    @property
    def llm_retry_max_attempts(self) -> int:
        return 3

    @property
    def llm_retry_base_delay(self) -> float:
        return 0.5

    @property
    def llm_retry_max_delay(self) -> float:
        return 8.0

    @property
    def llm_cost_enabled(self) -> bool:
        return False

    @property
    def llm_global_daily_token_budget(self) -> int:
        return 0

    @property
    def llm_user_daily_token_budget(self) -> int:
        return 0

    @property
    def redis_url(self) -> str:
        return self.REDIS_URL

    @property
    def redis_socket_timeout(self) -> float:
        return 5.0

    @property
    def pay_page_path(self) -> str:
        return "/pages/order/confirm"


settings = Settings()


def setup_logging():
    """配置日志格式。"""
    import logging
    level = logging.DEBUG if settings.APP_ENV == 'dev' else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s | %(levelname)-7s | %(name)s | %(message)s'
    )
