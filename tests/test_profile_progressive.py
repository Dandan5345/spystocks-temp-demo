import asyncio

from fastapi.testclient import TestClient

from app import sec_client
from app.main import app
from app.wikipedia_client import select_candidate


def test_overview_uses_submissions_without_fetching_ownership_xml(monkeypatch):
    async def fake_submissions(cik):
        return {
            "name": "DOE JANE",
            "formerNames": [],
            "addresses": {"business": {"city": "Austin", "stateOrCountry": "TX"}},
            "_all_filings": [
                {"form": "4", "filingDate": "2026-08-01"},
                {"form": "8-K", "filingDate": "2026-08-02"},
            ],
        }

    async def ownership_must_not_run(*args, **kwargs):
        raise AssertionError("overview fetched ownership XML")

    monkeypatch.setattr(sec_client, "get_submissions", fake_submissions)
    monkeypatch.setattr(sec_client, "get_ownership_filing", ownership_must_not_run)
    overview = asyncio.run(sec_client.build_profile_overview("123"))

    assert overview["person"]["name"] == "DOE JANE"
    assert overview["person"]["cik"] == "0000000123"
    assert overview["coverage"]["ownership_filings_found"] == 1


def test_overview_route_rejects_non_numeric_cik():
    response = TestClient(app).get("/api/profile/not-a-cik/overview")
    assert response.status_code == 400


def test_wikipedia_candidate_requires_exact_sec_name_and_lead_evidence():
    pages = [
        {"title": "Jane Doe", "extract": "Jane Doe is an American executive.", "thumbnail": {"source": "https://example.test/jane.jpg"}},
        {"title": "Jane Doe (journalist)", "extract": "Jane Doe is a journalist."},
    ]
    result = select_candidate("DOE JANE", [], pages)
    assert result is not None
    assert result["title"] == "Jane Doe"
    assert result["confidence"] == "high"
    assert len(result["evidence_signals"]) == 2


def test_wikipedia_candidate_rejects_fuzzy_namesake_and_disambiguation():
    pages = [
        {"title": "Jane A. Doe", "extract": "Jane A. Doe is an executive."},
        {"title": "Jane Doe", "extract": "Jane Doe may refer to several people.", "pageprops": {"disambiguation": ""}},
    ]
    assert select_candidate("DOE JANE", [], pages) is None


def test_wikipedia_candidate_accepts_public_short_name_when_legal_name_is_in_lead():
    pages = [{
        "title": "Tim Cook",
        "extract": "Timothy Donald Cook is an American business executive.",
        "thumbnail": {"source": "https://example.test/tim.jpg"},
    }]
    result = select_candidate("COOK TIMOTHY D", [], pages)
    assert result is not None
    assert result["title"] == "Tim Cook"


def test_wikipedia_candidate_accepts_public_alias_when_full_sec_name_is_in_lead():
    pages = [{
        "title": "Jensen Huang",
        "extract": "Jen-Hsun Huang, commonly known as Jensen Huang, is a business executive.",
    }]
    result = select_candidate("HUANG JEN HSUN", [], pages)
    assert result is not None
    assert result["title"] == "Jensen Huang"
