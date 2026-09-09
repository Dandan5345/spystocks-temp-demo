import httpx
import pytest

from app.fec_client import FECClient, _latest_reports


def test_latest_reports_keeps_newest_amendment_and_period():
    rows = [
        {"committee_id": "C1", "coverage_end_date": "2026-03-31", "receipt_date": "2026-04-10"},
        {"committee_id": "C1", "coverage_end_date": "2026-03-31", "receipt_date": "2026-04-12"},
        {"committee_id": "C1", "coverage_end_date": "2026-06-30", "receipt_date": "2026-07-10"},
    ]
    assert _latest_reports(rows) == [rows[2]]


@pytest.mark.asyncio
async def test_fec_key_stays_in_request_and_not_response(monkeypatch, tmp_path):
    async def no_cache(_key, producer, ttl=None):
        return await producer()

    monkeypatch.setattr("app.fec_client.cache.cached", no_cache)

    def handler(request: httpx.Request):
        assert request.url.params.get("api_key") == "private-test-key"
        path = request.url.path
        if "/candidate/" in path:
            return httpx.Response(200, json={"results": [{"committee_id": "C1", "name": "Friends"}]})
        if "/reports/" in path:
            return httpx.Response(200, json={"results": [{
                "committee_id": "C1", "coverage_end_date": "2026-06-30", "receipt_date": "2026-07-15",
                "total_receipts_ytd": 100, "total_disbursements_ytd": 40,
            }]})
        if "/filings/" in path:
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"results": []})

    result = await FECClient("private-test-key", transport=httpx.MockTransport(handler)).campaign_finance(["H0CA00001"], 2026)
    assert result["available"] is True
    assert result["totals"]["raised"] == 100
    assert "private-test-key" not in str(result)
