import pytest

from app import legislator_directory as directory


@pytest.mark.asyncio
async def test_directory_joins_member_to_committee_and_person_record(monkeypatch):
    payloads = {
        "committee-membership-current": {"SSAF": [{"bioguide": "T000278", "rank": 7, "title": "Member"}]},
        "committees-current": [{"thomas_id": "SSAF", "name": "Agriculture", "type": "senate", "url": "https://example.test"}],
        "legislators-current": [{
            "id": {"bioguide": "T000278", "wikidata": "Q1"},
            "bio": {"birthday": "1954-09-18", "gender": "M"},
            "terms": [{"start": "2025-01-03", "office": "455 Russell", "phone": "202-555-0100"}],
        }],
    }

    async def fake_yaml(name):
        return payloads[name]

    monkeypatch.setattr(directory, "_yaml", fake_yaml)
    result = await directory.legislator_directory("T000278", True)
    assert result["birthday"] == "1954-09-18"
    assert result["committees"][0]["name"] == "Agriculture"
    assert result["committees"][0]["rank"] == 7
