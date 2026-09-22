"""使知识库文件质量检查进入默认 pytest，无网络、数据库或模型调用。"""
import subprocess
import sys
from pathlib import Path


def test_repository_knowledge_quality_gate():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, '-X', 'utf8', 'scripts/validate_knowledge.py'],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'knowledge domains valid:' in result.stdout
