"""配置 —— 从 .env 读取，所有可调项集中在此。"""

from __future__ import annotations
from pathlib import Path
from pydantic import AliasChoices, Field, model_validator
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
    DATABASE_URL: str = ""  # 生产必填 PostgreSQL；禁止 SQLite
    DB_PATH: str = str(BASE_DIR / "data" / "agent.db")

    @model_validator(mode='after')
    def validate_production_database(self) -> 'Settings':
        """生产环境必须使用外部关系型数据库，不允许 SQLite。"""
        if self.APP_ENV.lower() in {'prod', 'production'}:
            url = (self.DATABASE_URL or '').strip().lower()
            if not url or url.startswith('sqlite'):
                raise ValueError('生产环境必须配置 PostgreSQL DATABASE_URL，禁止使用 SQLite')
            if not url.startswith(('postgresql://', 'postgres://')):
                raise ValueError('当前生产数据库适配器要求 DATABASE_URL 使用 PostgreSQL')
        return self

    @model_validator(mode='after')
    def validate_production_auth(self) -> 'Settings':
        """生产环境必须启用鉴权并配置独立签名密钥。"""
        if self.APP_ENV.lower() in {'prod', 'production'}:
            if not self.JWT_SECRET or len(self.JWT_SECRET) < 32:
                raise ValueError('生产环境必须配置至少 32 位 JWT_SECRET')
            self.AUTH_REQUIRED = True
            # 匿名登录生产默认关闭；仅在 .env 显式声明 ANONYMOUS_LOGIN_ENABLED=true 时保留
            if 'ANONYMOUS_LOGIN_ENABLED' not in self.model_fields_set:
                self.ANONYMOUS_LOGIN_ENABLED = False
        return self

    @model_validator(mode='after')
    def validate_production_origins(self) -> 'Settings':
        """生产环境禁止通配 CORS，避免带凭证接口暴露给任意站点。"""
        if self.APP_ENV.lower() in {'prod', 'production'} and '*' in self.ALLOWED_ORIGINS:
            raise ValueError('生产环境必须配置明确的 ALLOWED_ORIGINS，不能使用 *')
        return self

    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_MAX_ITERATIONS: int = 8
    LLM_REQUEST_TIMEOUT: float = 120.0
    LLM_TEMPERATURE: float = 0.3
    LLM_MAX_TOKENS: int = 3000

    # ── hy大模型 ──
    HY_API_KEY: str = ""
    HY_BASE_URL: str = "https://tokenhub.tencentmaas.com/v1/responses"
    HY_LLM_MODEL: str = "hy3"
    HY_IMAGE_MODEL: str = "Hy-Image-3.0"

    # ── 图像生成 ──
    IMAGE_PROVIDER: str = "mock"  # mock / flux / dall-e / kling / comfyui / hy
    IMAGE_API_KEY: str = ""
    IMAGE_BASE_URL: str = ""
    IMAGE_MODEL: str = ""
    IMAGE_WIDTH: int = 768
    IMAGE_HEIGHT: int = 1024
    IMAGE_PUBLIC_BASE_URL: str = ""  # 生图结果公网前缀（如 https://cdn.example.com）；留空则用本服务 /generated 相对路径

    # ── 电子贺卡（模板合成，Pillow 渲染）──
    CARD_FONT_PATH: str = ""  # 中文字体文件路径；留空则按常见系统路径自动探测（Docker 镜像已装 fonts-noto-cjk）
    CARD_WIDTH: int = 900  # 贺卡画布宽(px)
    CARD_HEIGHT: int = 1200  # 贺卡画布高(px)

    # ── 微信小程序 ──
    WECHAT_APPID: str = Field(default="", validation_alias=AliasChoices('WECHAT_APPID', 'WX_APPID'))
    WECHAT_SECRET: str = Field(default="", validation_alias=AliasChoices('WECHAT_SECRET', 'WX_SECRET'))

    JWT_SECRET: str = ""
    JWT_EXPIRE_HOURS: int = 720
    AUTH_REQUIRED: bool = False
    # 多平台接入：平台级 API Key，格式 "platform_id=key"，多个用逗号或换行分隔。
    # 例如：PLATFORM_API_KEYS=wxmini=sk-abc123,h5app=sk-def456
    # 配置后，接入方后端通过 POST /auth/token + X-API-Key 为自己的用户换取智能体 token。
    PLATFORM_API_KEYS: str = ""
    # 匿名登录开关：开发联调用；生产环境默认关闭（未显式设置时强制 False）。
    ANONYMOUS_LOGIN_ENABLED: bool = True
    ZHIPU_API_KEY: str = ""
    VISION_ALLOWED_ROOT: str = str(BASE_DIR / 'data' / 'generated')
    VISION_MAX_IMAGE_BYTES: int = 10 * 1024 * 1024

    # ── 平台自有下单 API（智能体不直写任何库，下单一律调平台侧能力）──
    # 由部署方 / 平台方申请开通并配置；未配置时 create_order 明确报错，不静默、不落本地订单表。
    PLATFORM_ORDER_API_URL: str = ""  # 例 https://api.flower-platform.com/v1/orders
    PLATFORM_ORDER_API_KEY: str = ""  # 可选，下单 API 鉴权凭据（如 Bearer token）

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
        # 推理型模型（hy4 / glm-5.3 等）会先消耗大量 reasoning token，
        # 预算给小了会导致 content 为空，因此默认放宽到 3000 并支持环境变量覆盖。
        return self.LLM_MAX_TOKENS

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
