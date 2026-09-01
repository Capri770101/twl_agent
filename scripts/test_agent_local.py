"""本地智能体效果测试入口。

用途：不接小程序/H5，直接在命令行和智能体对话，观察回复、卡片和工具调用。

运行：
    python scripts/test_agent_local.py
"""
from __future__ import annotations

import asyncio
import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


async def main(message: str | None = None) -> None:
    from agent.agent import ReActAgent
    from backend.storage.db import init_db

    init_db()

    agent = ReActAgent()
    user_id = "local_test_user"
    session_id = None

    if message is not None:
        result = await agent.arun(user_id, message, session_id=session_id)
        print("智能体回复:")
        print(result.reply)
        print(f"ui = {result.ui}")
        if result.data:
            print("data =")
            print(json.dumps(result.data, ensure_ascii=False, indent=2))
        if result.tool_calls:
            print("tool_calls =")
            for tc in result.tool_calls:
                print(f"- {tc.name} [{tc.status}]")
        return

    print("输入内容后回车，输入 exit 退出。")
    while True:
        message = input("你: ").strip()
        if not message:
            continue
        if message.lower() in {"exit", "quit"}:
            break

        result = await agent.arun(user_id, message, session_id=session_id)
        session_id = result.session_id or session_id
        print("\n智能体回复:")
        print(result.reply)
        print(f"ui = {result.ui}")
        if result.data:
            print("data =")
            print(json.dumps(result.data, ensure_ascii=False, indent=2))
        if result.tool_calls:
            print("tool_calls =")
            for tc in result.tool_calls:
                print(f"- {tc.name} [{tc.status}]")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--message", "-m", help="一次性发送的测试消息")
    args = parser.parse_args()
    asyncio.run(main(args.message))
