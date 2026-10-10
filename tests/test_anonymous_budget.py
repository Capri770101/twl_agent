"""Deployed anonymous demo requires explicit shared budgets and fails closed on Redis loss."""
import pytest
from pydantic import ValidationError

from backend.config import Settings, settings
from agent.engine import budget


DEPLOY = {'APP_ENV': 'prod', 'DATABASE_URL': 'postgresql://u@localhost/db',
          'ALLOWED_ORIGINS': 'https://demo.example.com',
          'JWT_SECRET': '9fa60db4c860f61e4da8709448e14c2a7b61b2064c6a3468'}


def test_anonymous_deployment_requires_explicit_budget_and_ip_limit():
    with pytest.raises(ValidationError, match='匿名体验'):
        Settings(**DEPLOY, ANONYMOUS_LOGIN_ENABLED=True)
    s = Settings(**DEPLOY, ANONYMOUS_LOGIN_ENABLED=True,
                 REDIS_URL='redis://localhost:6379/0', RATE_LIMIT_IP_PER_MINUTE=20,
                 LLM_GLOBAL_DAILY_TOKEN_BUDGET=100000, LLM_USER_DAILY_TOKEN_BUDGET=1000)
    assert s.ANONYMOUS_LOGIN_ENABLED
    with pytest.raises(ValidationError, match='匿名体验'):
        Settings(**DEPLOY, ANONYMOUS_LOGIN_ENABLED=True, RATE_LIMIT_ENABLED=False,
                 REDIS_URL='redis://localhost:6379/0', RATE_LIMIT_IP_PER_MINUTE=20,
                 LLM_GLOBAL_DAILY_TOKEN_BUDGET=100000, LLM_USER_DAILY_TOKEN_BUDGET=1000)


def test_redis_outage_denies_deployed_anonymous_requests(monkeypatch):
    monkeypatch.setattr(settings, 'APP_ENV', 'prod')
    monkeypatch.setattr(settings, 'LLM_COST_ENABLED', True)
    monkeypatch.setattr(budget, '_make_client', lambda: None)
    assert budget.check('anon_random')[0] is False
    assert budget.check('customer_h5_owner')[0] is True
