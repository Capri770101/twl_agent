"""pytest 共用配置与 fixture。

测试设计原则
------------
- **只测纯逻辑与结构**：不连数据库、不调 LLM、不发网络请求，保证离线秒级跑完。
- **回归导向**：每个测试文件对应一类历史踩过的坑（见各文件 docstring），
  防止「修好一处、弄坏另一处」。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 保证可从仓库根导入 agent / backend（不依赖安装为包）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def ops_disabled():
    """确保 ENABLE_OPS_TOOLS 关闭（默认态），用例结束后恢复原值。"""
    from backend.config import settings

    original = settings.ENABLE_OPS_TOOLS
    settings.ENABLE_OPS_TOOLS = False
    yield
    settings.ENABLE_OPS_TOOLS = original


@pytest.fixture
def ops_enabled():
    """临时开启 ENABLE_OPS_TOOLS（模拟部署接入期），用例结束后恢复原值。"""
    from backend.config import settings

    original = settings.ENABLE_OPS_TOOLS
    settings.ENABLE_OPS_TOOLS = True
    yield
    settings.ENABLE_OPS_TOOLS = original
