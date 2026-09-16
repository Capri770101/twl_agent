#!/usr/bin/env python3
"""从生产 `.env` 派生演示栈配置 `.env.demo`（客户体验沙箱）。

用途：一条命令把演示栈所需的独立密钥生成好，同时把 LLM / 生图 凭据从生产
      `.env` 复制过去（避免手抄出错），并**显式不含任何生产库连接信息**。

用法（在项目根目录）：
    python3 scripts/gen_demo_env.py            # 生成 .env.demo 并把 DEMO_POSTGRES_PASSWORD 写入 .env
    python3 scripts/gen_demo_env.py --force    # 覆盖已存在的 .env.demo（**沿用**已有库密码）

设计约束：
- 演示库（flora_demo）与生产库完全隔离，本脚本**不会**把 DATABASE_URL 带过去。
- JWT_SECRET / 平台密钥每次生成都是新的随机值。
- 只读 `.env`，不修改其除 DEMO_POSTGRES_PASSWORD 之外的任何内容。
- ⚠️ 重生成时**默认沿用**已有 .env.demo 的数据库密码：Postgres 的密码只在数据卷初始化
  时生效，换密码而不重建数据卷会让演示后端连不上库。确需更换用 `--new-db-password`
  （之后必须 `docker compose --profile demo down -v` 清掉 pgdata_demo 再起）。
"""
from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / '.env'
DEMO_FILE = ROOT / '.env.demo'

# 从生产 .env 原样复制过来的键（凭据 + 模型选择）
COPY_KEYS = (
    'LLM_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL',
    'IMAGE_API_KEY', 'IMAGE_BASE_URL', 'IMAGE_MODEL',
    'IMAGE_WIDTH', 'IMAGE_HEIGHT',
)

TEMPLATE = """# ══════════════════════════════════════════════════════════════════════
# 客户体验演示栈配置（由 scripts/gen_demo_env.py 生成，请勿手工重排）
# 与生产完全隔离：独立数据库 flora_demo、独立 JWT / 平台密钥。
# ══════════════════════════════════════════════════════════════════════
APP_ENV=prod
HOST=0.0.0.0
PORT=8000

# ── 演示专用数据库（compose 服务 postgres-demo）──
POSTGRES_PASSWORD={db_pw}
DATABASE_URL=postgresql://flora:{db_pw}@postgres-demo:5432/flora_demo

# ── LLM（复制自生产 .env）──
LLM_API_KEY={llm_key}
LLM_BASE_URL={llm_base}
LLM_MODEL={llm_model}
LLM_MAX_ITERATIONS=8
LLM_MAX_TOKENS=3000
LLM_TEMPERATURE=0.7
LLM_REQUEST_TIMEOUT=120
LLM_ENABLE_THINKING=false
LLM_HY_FALLBACK_ENABLED=false

# ── 生图（复制自生产 .env；若担心被刷成本可把 IMAGE_PROVIDER 改成 mock）──
IMAGE_PROVIDER=qwen
IMAGE_API_KEY={img_key}
IMAGE_BASE_URL={img_base}
IMAGE_MODEL={img_model}
IMAGE_WIDTH={img_w}
IMAGE_HEIGHT={img_h}
IMAGE_PUBLIC_BASE_URL=

# ── 鉴权：每次访问都是全新匿名访客，互不串数据 ──
JWT_SECRET={jwt}
JWT_EXPIRE_HOURS=24
AUTH_REQUIRED=true
ANONYMOUS_LOGIN_ENABLED=true
PLATFORM_API_KEYS=demo={platform_key}

# 与接口同源（nginx /demo/ 与 /demo-api/），无需通配 CORS
ALLOWED_ORIGINS={origins}

# ── 成本与并发护栏（演示收紧，防被刷）──
LLM_COST_ENABLED=true
RATE_LIMIT_ENABLED=true
RATE_LIMIT_PER_MINUTE=20
# 演示页每次访问都领全新匿名身份 → 只按 user_id 限流挡不住刷量，必须再补一道 IP 维度
RATE_LIMIT_IP_PER_MINUTE=30
AGENT_MAX_CONCURRENCY=4
IMAGE_TASK_MAX_WORKERS=2
IMAGE_TASK_QUEUE_MAX=20

# ── 演示不需要的，关掉省成本 ──
MEMORY_CONSOLIDATE_ENABLED=false
LEARNING_WEBHOOK_SECRET=
EMBEDDING_PROVIDER=
RAG_GAP_LOG_ENABLED=false
DIY_LLM_REQUIREMENT_ENABLED=true
AGENT_PAGE_ENABLED=true

# ── 平台只读数据源（平台侧接口，非我们的数据库）：让演示能查真实商品/店铺 ──
PLATFORM_API_AISTORE_URL={aistore}
# 体验版只走「生成方案 + 给建议」：不开放店铺查询 → schema 不暴露 shop、执行层拒绝、
# prompt 换 full_platform_plan_only 变体、不产店铺卡、不引导下单。
PLATFORM_ALLOWED_ENTITIES=plan
"""


def parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding='utf-8').splitlines():
        s = line.strip()
        if not s or s.startswith('#') or '=' not in s:
            continue
        k, v = s.split('=', 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description='生成演示栈 .env.demo')
    ap.add_argument('--force', action='store_true', help='覆盖已存在的 .env.demo')
    ap.add_argument('--new-db-password', action='store_true',
                    help='强制生成新的演示库密码（⚠️ 必须同时重建 postgres-demo 数据卷，否则连不上）')
    args = ap.parse_args()

    if DEMO_FILE.exists() and not args.force:
        print(f'✗ {DEMO_FILE.name} 已存在；如需重建请加 --force', file=sys.stderr)
        return 1

    # ⚠️ 关键：PostgreSQL 的 POSTGRES_PASSWORD 只在**数据卷初始化时**生效。
    # 若在演示库已初始化后重新生成一个随机密码，容器里的库仍是旧密码 → 演示后端连不上。
    # 因此默认**沿用**已有 .env.demo 里的数据库密码，只有显式 --new-db-password 才换新。
    db_pw = ''
    reused_db_pw = False
    if DEMO_FILE.exists() and not args.new_db_password:
        db_pw = parse_env(DEMO_FILE).get('POSTGRES_PASSWORD', '')
        if db_pw:
            reused_db_pw = True
            print('· 沿用已有演示库密码（如需更换请加 --new-db-password，并重建 postgres-demo 数据卷）')
    if not db_pw:
        db_pw = secrets.token_urlsafe(18)

    env = parse_env(ENV_FILE)
    missing = [k for k in ('LLM_API_KEY',) if not env.get(k)]
    if missing:
        print(f'✗ 生产 .env 缺少必需项：{", ".join(missing)}', file=sys.stderr)
        return 1

    jwt = secrets.token_urlsafe(48)
    platform_key = secrets.token_urlsafe(24)

    def pick(key: str, default: str = '') -> str:
        """优先复制生产的同名值；LLM_BASE_URL 为空时回落阿里云兼容模式。"""
        val = env.get(key) or ''
        if key == 'LLM_BASE_URL' and not val:
            return 'https://dashscope.aliyuncs.com/compatible-mode/v1'
        return val or default

    content = TEMPLATE.format(
        db_pw=db_pw,
        llm_key=env.get('LLM_API_KEY', ''),
        llm_base=pick('LLM_BASE_URL'),
        llm_model=pick('LLM_MODEL', 'qwen3.8-flash'),
        img_key=pick('IMAGE_API_KEY') or env.get('LLM_API_KEY', ''),
        img_base=pick('IMAGE_BASE_URL', 'https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation'),
        img_model=pick('IMAGE_MODEL', 'qwen-image-3.0'),
        img_w=pick('IMAGE_WIDTH', '768'),
        img_h=pick('IMAGE_HEIGHT', '1024'),
        jwt=jwt,
        platform_key=platform_key,
        origins=env.get('ALLOWED_ORIGINS') or 'https://api.tiaowulan.com',
        aistore='https://aistore.xiangbinmeigui.com',
    )
    DEMO_FILE.write_text(content, encoding='utf-8', newline='\n')   # 强制 LF，避免 CRLF 掺进密钥

    # compose 的变量插值读的是项目根 .env，故把演示库密码也写进去
    text = ENV_FILE.read_text(encoding='utf-8')
    if re.search(r'^DEMO_POSTGRES_PASSWORD=', text, re.M):
        text = re.sub(r'^DEMO_POSTGRES_PASSWORD=.*$', f'DEMO_POSTGRES_PASSWORD={db_pw}', text, flags=re.M)
    else:
        text = text.rstrip('\n') + f'\n\n# 演示栈（profile=demo）专用数据库密码\nDEMO_POSTGRES_PASSWORD={db_pw}\n'
    ENV_FILE.write_text(text, encoding='utf-8', newline='\n')

    print('✓ 已生成 .env.demo（LF 换行）')
    print(f'  LLM_MODEL      = {pick("LLM_MODEL", "qwen3.8-flash")}')
    print(f'  LLM key 长度   = {len(env.get("LLM_API_KEY", ""))}')
    print(f'  IMAGE_MODEL    = {pick("IMAGE_MODEL", "qwen-image-3.0")}')
    print(f'  IMAGE key 长度 = {len(pick("IMAGE_API_KEY") or env.get("LLM_API_KEY", ""))}')
    print(f'  DB 密码        = {"沿用已有（未更换）" if reused_db_pw else "已随机生成"}，'
          f'同时写入 .env 的 DEMO_POSTGRES_PASSWORD')
    print(f'  JWT_SECRET 长度= {len(jwt)}')
    print('✓ .env 已写入 DEMO_POSTGRES_PASSWORD（其余内容未改动）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
