"""Quick verification script."""
import sys
sys.path.insert(0, '.')

print("Checking dependencies...")

try:
    from backend.config import settings, setup_logging
    print("[OK] backend.config")
except Exception as e:
    print(f"[FAIL] backend.config: {e}")

try:
    from backend.storage.db import init_db
    print("[OK] backend.storage.db")
except Exception as e:
    print(f"[FAIL] backend.storage.db: {e}")

try:
    from backend.storage.memory import get_or_create_session
    print("[OK] backend.storage.memory")
except Exception as e:
    print(f"[FAIL] backend.storage.memory: {e}")

try:
    from backend.storage.repository import repo
    print("[OK] backend.storage.repository")
except Exception as e:
    print(f"[FAIL] backend.storage.repository: {e}")

try:
    from backend.routers.chat import router
    print("[OK] backend.routers.chat")
except Exception as e:
    print(f"[FAIL] backend.routers.chat: {e}")

try:
    from agent.agent import ReActAgent
    print("[OK] agent.agent")
except Exception as e:
    print(f"[FAIL] agent.agent: {e}")

print("\nVerification complete!")
