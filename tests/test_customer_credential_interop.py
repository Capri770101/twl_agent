"""真实 H5 Node 签发代码与 Python 验签互通；不依赖线上或真实密钥。"""
import json
from pathlib import Path
import shutil
import subprocess
import pytest
from fastapi import HTTPException
from backend.config import settings
from backend.customer_identity import customer_subject, bound_customer_credential


def test_node_issued_credential_accepted_by_python(monkeypatch):
    module = Path(__file__).resolve().parents[2] / 'H5/server/src/customerCredential.js'
    if not shutil.which('node') or not module.exists():
        pytest.skip('requires adjacent H5 repo and Node')
    secret = 'interop-test-only-key-' * 3
    code = f"import {{customerCredentials}} from {json.dumps(module.as_uri())}; console.log(JSON.stringify(customerCredentials({json.dumps(secret)}).issue(7)));"
    result = subprocess.run(['node', '--input-type=module', '-e', code], capture_output=True, text=True, check=True, timeout=10)
    credential = json.loads(result.stdout)['access_token']
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_ENABLED', True)
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_SECRET', secret)
    subject = customer_subject(credential)
    assert subject.startswith('customer_h5_')
    assert bound_customer_credential(credential, subject) == credential
    with pytest.raises(HTTPException) as error:
        bound_customer_credential(credential, 'ordinary-public-agent-user')
    assert error.value.status_code == 403
