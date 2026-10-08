"""运行接口与发布文件使用同一版本来源。"""
from pathlib import Path

VERSION = (Path(__file__).resolve().parents[1] / 'VERSION').read_text(encoding='utf-8').strip()
