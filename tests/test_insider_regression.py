from fastapi.testclient import TestClient

from app.main import app
from app.main import congress_client


def test_existing_insider_search_route_still_works(monkeypatch):
    async def fake_search(query, limit):
        return [{"name": "HUANG JEN HSUN", "cik": "0001045810", "score": 100}]

    monkeypatch.setattr("app.main.search_cik", fake_search)
    response = TestClient(app).get("/api/search?q=Jensen%20Huang")
    assert response.status_code == 200
    assert response.json()["results"][0]["cik"] == "0001045810"


def test_existing_insider_profile_route_still_works(monkeypatch):
    async def fake_profile(cik, max_filings, include_photo):
        return {"person": {"name": "Test Person", "cik": cik}, "summary": {}, "coverage": {}}

    monkeypatch.setattr("app.main.build_profile", fake_profile)
    response = TestClient(app).get("/api/profile/123")
    assert response.status_code == 200
    assert response.json()["person"]["name"] == "Test Person"


def test_missing_congress_key_is_safe_and_server_only(monkeypatch):
    monkeypatch.setattr(congress_client, "api_key", "")
    congress_client._memory.clear()
    response = TestClient(app).get("/api/politicians/P000197")
    assert response.status_code == 503
    assert response.json() == {"detail": "Congress.gov access is not configured."}
    assert "CONGRESS_API_KEY" not in response.text
