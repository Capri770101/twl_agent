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

    # ── LLM 行为开关 ──
    # hy 兜底：默认关闭，避免跨厂商风格跳变（曾致答非所问）。需要兜底时显式置 true。
    LLM_HY_FALLBACK_ENABLED: bool = False
    # Qwen 思考链：默认关闭以提速（qwen3.x 为推理型，开启会先输出 reasoning_content 显著变慢）。
    # 复杂推理场景可置 true 重新开启。
    LLM_ENABLE_THINKING: bool = False

    # ── LLM token 成本护栏（供 agent/engine/budget.py 消费，防 token 计费失控）──
    # 生效条件（三者需同时满足）：LLM_COST_ENABLED=true、REDIS_URL 已配置、对应日预算 > 0；
    # 缺任一条件则**静默放行**（故障不影响主链路，仅失去预算约束）。
    # 计量维度：全局（所有用户合计）+ 单用户，按自然日重置。
    # 注意：开关默认开启（安全默认）；未配 Redis 或预算为 0 时不产生实际限制。
    LLM_COST_ENABLED: bool = True
    LLM_GLOBAL_DAILY_TOKEN_BUDGET: int = 0  # 0 = 不限制；建议按实际预算设正数（如 5_000_000）
    LLM_USER_DAILY_TOKEN_BUDGET: int = 0    # 0 = 不限制

    # ── 记忆自动固化（L2 自我进化路径）──
    # 对话结束后异步提炼用户明确表达的偏好写入长期记忆。默认开启；
    # 按 MEMORY_CONSOLIDATE_EVERY 条新消息节流一次，关闭则退化为「靠 LLM 主动调 save_memory」。
    MEMORY_CONSOLIDATE_ENABLED: bool = True
    MEMORY_CONSOLIDATE_EVERY: int = 4

    # ── 图像生成 ──
    # 已实现：mock / qwen（阿里云百炼 Qwen-Image，原生 multimodal-generation 接口）/ hy
    # flux / dall-e / kling / comfyui 仅为占位，未实现会自动回落 mock
    IMAGE_PROVIDER: str = "mock"
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

    # ── 运维 / 接入期工具开关（默认关闭，见 agent/toolkit.py）──
    # 关闭时，8 个平台接入工具（platform_db_discover / _test_connection / _sample_table
    # 与 platform_mapping_draft / _save_draft / _list_drafts / _set_status / _get_active）
    # **不暴露给 C 端会话且不可被调用**；仅在部署接入期临时置 true，
    # 供配置方通过对话完成结构与映射接入。日常运营保持 false。
    ENABLE_OPS_TOOLS: bool = False

    # ── 监控面板（/api/metrics + 独立 dashboard 容器）──
    # 生产必填：未配置时 /api/metrics 一律 503，避免面板被裸奔暴露。
    # 建议用 `openssl rand -hex 32` 生成高强度随机串，与业务 JWT_SECRET 区分。
    DASHBOARD_API_KEY: str = ""
    ZHIPU_API_KEY: str = ""
    VISION_ALLOWED_ROOT: str = str(BASE_DIR / 'data' / 'generated')
    VISION_MAX_IMAGE_BYTES: int = 10 * 1024 * 1024

    # ── 平台自有下单 API（智能体不直写任何库，下单一律调平台侧能力）──
    # 由部署方 / 平台方申请开通并配置；未配置时 create_order 明确报错，不静默、不落本地订单表。
    PLATFORM_ORDER_API_URL: str = ""  # 例 https://api.flower-platform.com/v1/orders
    PLATFORM_ORDER_API_KEY: str = ""  # 可选，下单 API 鉴权凭据（如 Bearer token）

    # ── 学习回调鉴权（平台真实订单 → 智能体 proven 域；L2 成交即学）──
    # 平台在真实订单落定时回调 POST /api/learning/order，带此密钥（X-Learning-Key 头）。
    # 留空则该端点返回 503（fail-closed，禁止裸奔写入 proven 域）。
    LEARNING_WEBHOOK_SECRET: str = ""

    # ── 腾讯地图（可选）──
    TENCENT_MAP_KEY: str = ""

    # ── Redis（可选）──
    REDIS_URL: str = ""

    # ── 请求限流（进程内固定窗口，防刷 / 防跑量；backend/rate_limit.py 消费）──
    # 默认开启：单用户每分钟最多 RATE_LIMIT_PER_MINUTE 次 /chat 调用。
    # 多实例部署时各实例独立计数（如需全局精确限流需换 Redis 后端）。
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_PER_MINUTE: int = 30

    # ── 检索缺口日志（Tier 1.3 观测：记录零结果/低分查询以发现知识库盲区）──
    # 默认关闭，避免每次检索都写盘 + 隐私面；运营观测盲区时再打开。
    RAG_GAP_LOG_ENABLED: bool = False
    RAG_GAP_LOG_PATH: str = ""  # 留空 → 仓库内 data/eval/retrieval_gaps.jsonl

    # ── 向量检索（Tier 1 · 真实 embedding 升级；默认关，回退纯 TF-IDF）──
    # provider: ""=关（纯 TF-IDF）| dashscope（百炼 OpenAI 兼容 /embeddings）| mock（仅测试）
    # 开启前请先用评测集标定（scripts/eval_retrieval.py）；开启后任何失败自动回退 TF-IDF。
    EMBEDDING_PROVIDER: str = ""
    EMBEDDING_API_KEY: str = ""   # 留空回退 LLM_API_KEY（同厂商复用）
    EMBEDDING_BASE_URL: str = ""  # 留空回退 LLM_BASE_URL（compatible-mode）
    EMBEDDING_MODEL: str = "text-embedding-v3"
    EMBEDDING_DIM: int = 1024

    # ── DIY 需求抽取（L2：LLM 结构化 + 正则兜底）──
    # 在同一次设计调用里让模型顺带输出结构化需求，用于补正则漏抽的槽位（口语预算、
    # 中文数字支数、模糊风格等）。**只补空、不覆盖**规则已抽到的值，失败自动回退纯规则，
    # 故默认开启；置 false 可一键回到纯正则。
    DIY_LLM_REQUIREMENT_ENABLED: bool = True

    # ── 并发护栏（P2：防"一个慢请求拖垮全服务"）──
    # AGENT_MAX_CONCURRENCY：同时在跑的智能体轮次上限；超出**快速失败(503)**而非无限排队。
    # IMAGE_TASK_MAX_WORKERS：并发生图线程数；IMAGE_TASK_QUEUE_MAX：排队上限，超出直接判失败。
    AGENT_MAX_CONCURRENCY: int = 8
    IMAGE_TASK_MAX_WORKERS: int = 2
    IMAGE_TASK_QUEUE_MAX: int = 50

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
        # 中文自然语言问句经字符 n-gram 向量化后相似度普遍偏低（实测相关条目多在
        # 0.07-0.19 区间），原阈值 0.18 会把相关条目挡在外面（如「怎么让花开久一点」
        # 与「深水醒花」仅 0.15）。下调至 0.10，兼顾召回与噪声。
        return 0.10

    @property
    def rag_top_k(self) -> int:
        return 8

    @property
    def embedding_enabled(self) -> bool:
        """是否启用真实 embedding 语义通道（provider 非空且非 off/none/false）。"""
        return (self.EMBEDDING_PROVIDER or '').strip().lower() not in ('', 'off', 'none', 'false', '0')

    @property
    def embedding_base_url(self) -> str:
        # 未单配则复用 LLM 的 compatible-mode base（同厂商）
        return (self.EMBEDDING_BASE_URL or self.LLM_BASE_URL or '').strip()

    @property
    def embedding_api_key(self) -> str:
        # 未单配则复用 LLM_API_KEY（百炼 key 通用）
        return (self.EMBEDDING_API_KEY or self.LLM_API_KEY or '').strip()

    @property
    def embedding_min_score(self) -> float:
        # embedding 余弦量纲 ≠ TF-IDF：相关中文短句多在 0.35-0.75，无关常 <0.3
        return 0.35

    @property
    def embedding_weight(self) -> float:
        # 与 TF-IDF 分数融合时的权重（1.0 = 取二者较大者）
        return 1.0

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
        """token 成本护栏总开关（env: LLM_COST_ENABLED，默认开启）。"""
        return self.LLM_COST_ENABLED

    @property
    def llm_global_daily_token_budget(self) -> int:
        """全局日 token 预算；0 = 不限制。"""
        return self.LLM_GLOBAL_DAILY_TOKEN_BUDGET

    @property
    def llm_user_daily_token_budget(self) -> int:
        """单用户日 token 预算；0 = 不限制。"""
        return self.LLM_USER_DAILY_TOKEN_BUDGET

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
