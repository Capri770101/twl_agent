"""花艺智能体服务入口。

部署方式:
    # 直接运行
    python -m uvicorn main:app --host 0.0.0.0 --port 8000

    # Docker
    docker-compose up -d
"""

import os
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# 加载 .env
env_path = Path(__file__).parent / '.env'
if env_path.exists():
    load_dotenv(env_path)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.config import settings
from backend.storage.db import init_db
from backend.routers.chat import router as chat_router
from backend.routers.auth import router as auth_router

logging.basicConfig(
    level=logging.DEBUG if settings.APP_ENV == 'dev' else logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(name)s | %(message)s'
)
logger = logging.getLogger('agent')


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时初始化数据库
    try:
        init_db()
    except Exception as exc:
        logger.exception('数据库初始化失败：请确认 DATABASE_URL、数据库权限，或先执行 migrations/001_image_tasks.sql')
        raise RuntimeError(
            '数据库初始化失败。请确认 DATABASE_URL 可访问且账号有建表权限；'
            '若由 DBA 管理数据库，请先执行 migrations/001_image_tasks.sql。'
        ) from exc
    from backend.storage.tasks import check_task_storage, recover_incomplete_tasks
    try:
        check_task_storage()
    except Exception as exc:
        logger.exception('image_tasks 表自检失败：请执行 migrations/001_image_tasks.sql')
        raise RuntimeError('image_tasks 表不可用，请先执行 migrations/001_image_tasks.sql 或检查数据库权限。') from exc
    recover_incomplete_tasks()
    logger.info('花艺智能体服务启动 | env=%s | port=%s', settings.APP_ENV, settings.PORT)
    yield
    logger.info('花艺智能体服务关闭')


app = FastAPI(
    title='花艺智能体 API',
    description='基于 ReAct 的花艺顾问 AI，支持微信小程序接入',
    version='1.0.0',
    lifespan=lifespan
)

# CORS
origins = [o.strip() for o in settings.ALLOWED_ORIGINS.split(',') if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*']
)

# 注册路由
app.include_router(chat_router)
app.include_router(auth_router)

# 生图结果静态托管：/generated/{task_id}.png（数据存 data/generated/）
generated_dir = Path(settings.DB_PATH).parent / 'generated'
generated_dir.mkdir(parents=True, exist_ok=True)
app.mount('/generated', StaticFiles(directory=str(generated_dir)), name='generated')


@app.get('/health')
async def health():
    return {
        'status': 'ok',
        'service': 'flora-agent',
        'version': '1.0.0',
        'env': settings.APP_ENV
    }
