import httpx
import pytest

from app.whitehouse_client import (
    WhiteHouseClient,
    parse_administration_index,
    parse_whitehouse_profile,
    search_whitehouse,
)


INDEX_HTML = """
<html><body><main>
  <a href="/administration/donald-j-trump/">President Donald J. Trump</a>
  <a href="/administration/jd-vance/">Vice President JD Vance</a>
  <a href="/administration/the-cabinet/">The Cabinet</a>
</main></body></html>
"""

PROFILE_HTML = """
<html><head>
  <meta property="article:modified_time" content="2025-06-02T20:41:25+00:00">
</head><body><main>
  <h1>Donald J. Trump</h1>
  <p>45th &amp; 47th President of the United States</p>
  <img alt="President Donald J. Trump Official Presidential Portrait" src="/portrait.png">
  <p>Official biography paragraph with enough text to be included in the normalized profile response.</p>
</main></body></html>
"""


def test_administration_index_and_search():
    index = parse_administration_index(INDEX_HTML)
    assert [person["name"] for person in index] == ["Donald J. Trump", "JD Vance"]
    result = search_whitehouse(index, "Donald Trump")
    assert result[0]["id"] == "whitehouse:donald-j-trump"
    assert result[0]["profileType"] == "executive"


def test_whitehouse_profile_normalization():
    profile = parse_whitehouse_profile(PROFILE_HTML, "donald-j-trump", "https://www.whitehouse.gov/administration/donald-j-trump/")
    assert profile["name"] == "Donald J. Trump"
    assert profile["role"] == "45th & 47th President of the United States"
    assert profile["imageUrl"] == "https://www.whitehouse.gov/portrait.png"
    assert profile["source"]["name"] == "WhiteHouse.gov"
    assert len(profile["biography"]) == 1


@pytest.mark.asyncio
async def test_whitehouse_client_only_allows_indexed_official_profiles(monkeypatch, tmp_path):
    def handler(request: httpx.Request):
        if request.url.path == "/administration/":
            return httpx.Response(200, text=INDEX_HTML)
        if request.url.path == "/administration/donald-j-trump/":
            return httpx.Response(200, text=PROFILE_HTML)
        return httpx.Response(404)

    monkeypatch.setattr("app.whitehouse_client.INDEX_CACHE", tmp_path / "whitehouse.json")
    client = WhiteHouseClient(transport=httpx.MockTransport(handler))
    profile = await client.profile("donald-j-trump")
    assert profile["id"] == "whitehouse:donald-j-trump"
    assert (tmp_path / "whitehouse.json").exists()


@pytest.mark.asyncio
async def test_family_profiles_are_public_relationships_not_officials():
    client = WhiteHouseClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    profile = await client.profile("ivanka-trump")
    assert profile["profileType"] == "family"
    assert profile["currentOfficial"] is False
    assert profile["role"] == "Child of Donald J. Trump"
    assert profile["family"][0]["relationship"] == "Father"


@pytest.mark.asyncio
async def test_whitehouse_search_includes_named_family_profiles(monkeypatch, tmp_path):
    monkeypatch.setattr("app.whitehouse_client.INDEX_CACHE", tmp_path / "whitehouse.json")
    (tmp_path / "whitehouse.json").write_text('[]')
    client = WhiteHouseClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    results = await client.search("Ivanka Trump")
    assert results[0]["id"] == "whitehouse:ivanka-trump"
    assert results[0]["sourceType"] == "family"
