from contextlib import contextmanager
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.routers import metrics
from backend.config import settings


def test_customer_metrics_auth_window_and_grouping(monkeypatch):
    monkeypatch.setattr(settings, 'DASHBOARD_API_KEY', 'test-dashboard-key')
    calls = []
    class Connection:
        def execute(self, sql, args):
            calls.append((sql, args))
            return self
        def fetchall(self):
            return [{'operation':'orders','result_code':'AUTH_REQUIRED','calls':2,'avg_latency_ms':5}]
    @contextmanager
    def transaction(): yield Connection()
    monkeypatch.setattr(metrics, 'transaction', transaction)
    app = FastAPI()
    app.include_router(metrics.router)
    with TestClient(app) as client:
        assert client.get('/api/metrics/customer-service').status_code == 401
        assert not calls
        headers = {'X-Dashboard-Key':'test-dashboard-key'}
        assert client.get('/api/metrics/customer-service?hours=0', headers=headers).status_code == 422
        response = client.get('/api/metrics/customer-service?hours=12', headers=headers)
        assert response.json()['rows'][0]['result_code'] == 'AUTH_REQUIRED'
        assert calls[0][1] == (12,)
        assert 'GROUP BY operation, result_code' in calls[0][0]
