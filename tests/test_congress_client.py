import json
from urllib.parse import parse_qs

import httpx
import pytest

from app.congress_client import (
    CongressClient,
    CongressError,
    MissingCongressApiKey,
    normalize_index_member,
    normalize_legislation,
    normalize_member_detail,
    normalize_name,
    search_members,
)


DETAIL = {
    "member": {
        "bioguideId": "P000197",
        "directOrderName": "Nancy Pelosi",
        "firstName": "Nancy",
        "lastName": "Pelosi",
        "birthYear": "1940",
        "currentMember": True,
        "district": 11,
        "state": "California",
        "partyHistory": [{"partyAbbreviation": "D", "partyName": "Democratic", "startYear": 1987}],
        "terms": [
            {"chamber": "House of Representatives", "congress": 118, "district": 11, "startYear": 2023, "endYear": 2025, "stateName": "California"},
            {"chamber": "House of Representatives", "congress": 119, "district": 11, "startYear": 2025, "stateName": "California"},
        ],
        "sponsoredLegislation": {"count": 199},
        "cosponsoredLegislation": {"count": 5093},
        "updateDate": "2026-09-05T07:40:24Z",
    }
}


def member(name: str, bioguide: str, party: str = "Democratic"):
    return normalize_index_member({
        "name": name,
        "bioguideId": bioguide,
        "partyName": party,
        "state": "California",
        "terms": {"item": [{"chamber": "House of Representatives", "startYear": 2025}]},
    })


def test_normalization_handles_member_shape_and_missing_image():
    profile = normalize_member_detail(DETAIL)
    assert profile["name"] == "Nancy Pelosi"
    assert profile["currentChamber"] == "House"
    assert profile["currentParty"] == "Democratic"
    assert profile["currentDistrict"] == 11
    assert profile["totalTerms"] == 2
    assert profile["imageUrl"] is None
    assert profile["sponsoredLegislation"]["total"] == 199


def test_name_normalization_is_case_and_punctuation_tolerant():
    assert normalize_name("  Alexandria Ocasio-Cortez ") == "alexandria ocasio cortez"
    assert normalize_name("PELOSI, Nancy") == "pelosi nancy"


def test_search_exact_partial_reversed_and_multiple_matches():
    index = [
        member("Pelosi, Nancy", "P000197"),
        member("Smith, John", "S000001", "Republican"),
        member("Smith, John A.", "S000002"),
        member("Sanders, Bernard", "S000033", "Independent"),
    ]
    assert search_members(index, "Nancy Pelosi")[0]["bioguideId"] == "P000197"
    assert search_members(index, "Pelo")[0]["bioguideId"] == "P000197"
    assert search_members(index, "PELOSI, NANCY")[0]["bioguideId"] == "P000197"
    assert len(search_members(index, "John Smith")) == 2
    assert search_members(index, "zzzxqv") == []


def test_search_rejects_weak_shared_first_name_matches():
    index = [member("McEachin, A. Donald", "M001200"), member("Bailey, Donald A.", "B000044")]
    assert search_members(index, "Donald Trump") == []


@pytest.mark.asyncio
async def test_member_index_follows_pagination(monkeypatch, tmp_path):
    pages = {
        0: {"members": [{"name": "Pelosi, Nancy", "bioguideId": "P000197"}], "pagination": {"count": 2, "next": "https://api.congress.gov/v3/member?offset=1&limit=1&format=json"}},
        1: {"members": [{"name": "Sanders, Bernard", "bioguideId": "S000033"}], "pagination": {"count": 2}},
    }
    calls = []

    def handler(request: httpx.Request):
        query = parse_qs(request.url.query.decode())
        offset = int(query.get("offset", [0])[0])
        calls.append(offset)
        assert request.headers["X-Api-Key"] == "test-key"
        return httpx.Response(200, json=pages[offset])

    monkeypatch.setattr("app.congress_client.MEMBER_INDEX_CACHE", tmp_path / "index.json")
    client = CongressClient("test-key", transport=httpx.MockTransport(handler))
    result = await client.member_index(force=True)
    assert calls == [0, 1]
    assert [row["bioguideId"] for row in result] == ["P000197", "S000033"]


@pytest.mark.asyncio
async def test_client_parses_profile_and_never_places_key_in_url():
    seen_url = None

    def handler(request: httpx.Request):
        nonlocal seen_url
        seen_url = str(request.url)
        return httpx.Response(200, json=DETAIL)

    client = CongressClient("super-secret-test-key", transport=httpx.MockTransport(handler))
    result = await client.profile("P000197")
    assert result["bioguideId"] == "P000197"
    assert "super-secret-test-key" not in seen_url
    assert "super-secret-test-key" not in json.dumps(result)


@pytest.mark.asyncio
async def test_overview_deep_link_fetches_one_member_not_full_index(monkeypatch):
    client = CongressClient("test-key")

    async def fake_profile(bioguide):
        return {"bioguideId": bioguide, "name": "Jane Doe"}

    async def index_must_not_run(*args, **kwargs):
        raise AssertionError("deep link loaded the full member directory")

    monkeypatch.setattr(client, "profile", fake_profile)
    monkeypatch.setattr(client, "member_index", index_must_not_run)
    result = await client.overview("D000001")
    assert result["name"] == "Jane Doe"
    assert result["status"] == "ready"


def test_legislation_parsing_uses_true_pagination_total():
    result = normalize_legislation({
        "pagination": {"count": 199, "next": "https://api.congress.gov/v3/member/P000197/sponsored-legislation?offset=1"},
        "sponsoredLegislation": [{"congress": 118, "type": "HRES", "number": "742", "title": "A resolution", "latestAction": {"actionDate": "2023-09-29", "text": "Introduced"}}],
    }, "sponsored")
    assert result["total"] == 199
    assert result["hasMore"] is True
    assert result["items"][0]["officialUrl"].endswith("/house-resolution/742")


def test_legislation_marks_enacted_law_from_official_latest_action():
    result = normalize_legislation({
        "pagination": {"count": 1},
        "sponsoredLegislation": [{
            "congress": 117, "type": "HR", "number": "3325", "title": "A law",
            "latestAction": {"actionDate": "2021-08-05", "text": "Became Public Law No: 117-32."},
        }],
    }, "sponsored")
    assert result["items"][0]["enacted"] is True


@pytest.mark.asyncio
async def test_missing_api_key():
    client = CongressClient("")
    with pytest.raises(MissingCongressApiKey):
        await client.profile("P000197")


@pytest.mark.asyncio
async def test_congress_api_errors_are_sanitized():
    def handler(request: httpx.Request):
        return httpx.Response(429, headers={"Retry-After": "30"}, json={"error": "rate limit"})

    client = CongressClient("secret", transport=httpx.MockTransport(handler))
    with pytest.raises(CongressError) as caught:
        await client.profile("P000197")
    assert caught.value.status_code == 503
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_invalid_bioguide_id_does_not_call_api():
    client = CongressClient("secret")
    with pytest.raises(CongressError) as caught:
        await client.profile("../etc/passwd")
    assert caught.value.status_code == 400
