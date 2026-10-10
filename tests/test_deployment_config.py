"""Deployment configuration fails closed for signing and browser origins."""
import pytest
from pydantic import ValidationError

from backend.config import Settings


BASE = {'DATABASE_URL': 'postgresql://u@localhost/db', 'ALLOWED_ORIGINS': 'https://h5.example.com',
        'JWT_SECRET': '9fa60db4c860f61e4da8709448e14c2a7b61b2064c6a3468'}


@pytest.mark.parametrize('env', ['prod', 'production', 'staging', 'pre', 'uat', 'accidental-typo'])
def test_nonlocal_requires_authentication(env):
    s = Settings(APP_ENV=env, AUTH_REQUIRED=False, ANONYMOUS_LOGIN_ENABLED=False, **BASE)
    assert s.AUTH_REQUIRED is True
    assert s.is_deployed_environment


@pytest.mark.parametrize('secret', ['', 'short', 'change-this-to-a-random-secret-at-least-32-chars',
                                   'REPLACE_WITH_RANDOM_KEY_123456789012345678'])
def test_reject_missing_or_public_signing_key(secret):
    with pytest.raises(ValidationError, match='JWT_SECRET'):
        Settings(APP_ENV='prod', **{**BASE, 'JWT_SECRET': secret})


@pytest.mark.parametrize('origins', ['', '   ', '*', 'https://ok.example.com,*'])
def test_reject_empty_or_wildcard_origins(origins):
    with pytest.raises(ValidationError, match='ALLOWED_ORIGINS'):
        Settings(APP_ENV='staging', **{**BASE, 'ALLOWED_ORIGINS': origins})


def test_local_can_use_explicitly_insecure_configuration():
    s = Settings(APP_ENV='dev', AUTH_REQUIRED=False, ALLOWED_ORIGINS='*')
    assert s.AUTH_REQUIRED is False
