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
    from backend.data_gateway.external import discover_external, query_external_entity
    from backend.data_gateway.mapping_store import get_active_mapping
    from backend.data_gateway.mapper import generate_mapping_draft
    print("[OK] backend.data_gateway (platform_db)")
except Exception as e:
    print(f"[FAIL] backend.data_gateway: {e}")

try:
    from backend.routers.chat import router
    print("[OK] backend.routers.chat")
except Exception as e:
    print(f"[FAIL] backend.routers.chat: {e}")

try:
    from backend.routers.auth import router
    from backend.auth import require_platform_key, derive_platform_user_id
    print("[OK] backend.routers.auth (multi-platform)")
except Exception as e:
    print(f"[FAIL] backend.routers.auth: {e}")

try:
    from agent.agent import ReActAgent
    print("[OK] agent.agent")
except Exception as e:
    print(f"[FAIL] agent.agent: {e}")

print("\nVerification complete!")
