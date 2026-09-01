"""skills/__init__.py —— 技能自动发现与注册。

导入本包时自动扫描 skills/ 目录下所有子模块，触发其模块级 @register_tool
完成自注册。新增技能只要丢一个 .py 文件进来即可，无需改任何装配代码。
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path

logger = logging.getLogger("skills")

_DISCOVERED: list[str] = []


def autodiscover() -> list[str]:
    """扫描并导入所有技能模块，返回已加载的模块名列表。"""
    if _DISCOVERED:
        return _DISCOVERED
    pkg_dir = Path(__file__).resolve().parent
    for module_info in pkgutil.iter_modules([str(pkg_dir)]):
        if module_info.name in ("__init__",):
            continue
        importlib.import_module(f"agent.skills.{module_info.name}")
        _DISCOVERED.append(module_info.name)
        logger.info("[skills] 已加载技能模块: %s", module_info.name)
    return _DISCOVERED


# 导入本包即自动发现（幂等：重复导入不会重复注册）
autodiscover()
