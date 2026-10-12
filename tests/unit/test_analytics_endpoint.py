from starlette.testclient import TestClient
from agy_cli_manager.proxy.fastapi_app import app


def test_dashboard_contains_daily_analytics_ui():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert "Daily Token Consumption" in html
    assert "barsWrapper" in html
    assert "dailyTableBody" in html
    assert "analyticsCard" in html
    assert "range-selector" in html


def test_analytics_tokens_endpoint():
    client = TestClient(app)
    resp = client.get("/api/analytics/tokens?range_days=7")
    assert resp.status_code == 200
    data = resp.json()
    assert data["range_days"] == 7
    assert "summary" in data
    assert "timeline" in data
    assert "by_account" in data
    assert "by_model" in data
    assert len(data["timeline"]) == 7
    for item in data["timeline"]:
        assert "date" in item
        assert "requests" in item
        assert "prompt_tokens" in item
        assert "completion_tokens" in item
        assert "total_tokens" in item
