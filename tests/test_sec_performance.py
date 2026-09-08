"""Regression tests for the request-count and search work that make profiles slow."""

import asyncio

import pytest

from app import sec_client
from app.sec_client import (
    filing_archive_base,
    name_variants,
    rank_bulk_candidates,
    raw_ownership_document,
    scan_cik_lookup,
)


def test_bulk_search_bridges_public_and_edgar_legal_names():
    candidates = [{"name": "HUANG JEN HSUN", "cik": "1197649"}]
    result = rank_bulk_candidates("Jensen Huang", candidates, 12)
    assert result[0]["cik"] == "0001197649"
    assert result[0]["score"] >= 92


def test_archive_url_uses_registrant_not_accession_filing_agent():
    url = filing_archive_base("0001067983", "0000950123-25-000065")
    assert "/data/1067983/000095012325000065/" in url


def test_raw_ownership_document_strips_the_rendered_view_directory():
    # EDGAR's primaryDocument points at the XSLT-rendered HTML view; the parseable XML
    # is the same name at the accession root. Getting this right is what keeps a profile
    # to one request per filing instead of three.
    assert raw_ownership_document("xslF345X05/form4.xml") == "form4.xml"
    assert raw_ownership_document("xslF345X06/wk-form4_1759530830.xml") == "wk-form4_1759530830.xml"


def test_raw_ownership_document_leaves_plain_names_alone():
    assert raw_ownership_document("form4.xml") == "form4.xml"
    assert raw_ownership_document("primary_doc.xml") == "primary_doc.xml"
    assert raw_ownership_document(None) is None
    # Only the rendered-view prefix is stripped, never a real directory.
    assert raw_ownership_document("reports/form4.xml") == "reports/form4.xml"


def test_name_variants_cover_edgar_and_natural_orderings():
    # EDGAR files "COOK TIMOTHY D"; a proxy statement writes "Timothy D. Cook".
    assert "TIMOTHY D COOK" in name_variants("COOK TIMOTHY D")
    assert "COOK TIMOTHY D" in name_variants("COOK TIMOTHY D")
    assert "ANDREA JUNG" in name_variants("JUNG ANDREA")


def test_one_ownership_filing_costs_one_request(monkeypatch):
    requested: list[str] = []

    async def fake_get(url, *, as_json=False):
        requested.append(url)
        if as_json:
            raise AssertionError("index.json should not be fetched for a standard filing")
        return "<ownershipDocument><issuer></issuer></ownershipDocument>"

    monkeypatch.setattr(sec_client, "sec_get", fake_get)
    filing = {"accessionNumber": "0000320193-24-000001", "primaryDocument": "xslF345X05/form4.xml", "form": "4"}

    parsed = asyncio.run(sec_client.fetch_ownership_filing("0001214156", filing))

    assert parsed is not None
    assert len(requested) == 1
    assert requested[0].endswith("/form4.xml")
    assert "xslF345X05" not in requested[0]


def test_unusual_filing_still_falls_back_to_the_index(monkeypatch):
    requested: list[str] = []

    async def fake_get(url, *, as_json=False):
        requested.append(url)
        if as_json:
            return {"directory": {"item": [{"name": "ownership.xml"}]}}
        if url.endswith("ownership.xml"):
            return "<ownershipDocument><issuer></issuer></ownershipDocument>"
        return "<html>rendered view only</html>"

    monkeypatch.setattr(sec_client, "sec_get", fake_get)
    filing = {"accessionNumber": "0000320193-24-000002", "primaryDocument": "odd-name.htm", "form": "4"}

    parsed = asyncio.run(sec_client.fetch_ownership_filing("0001214156", filing))

    assert parsed is not None
    assert any(url.endswith("index.json") for url in requested)
    assert requested[-1].endswith("ownership.xml")


@pytest.fixture
def lookup_file(tmp_path):
    path = tmp_path / "cik-lookup-data.txt"
    path.write_text(
        "COOK TIMOTHY D:0001214156:\n"
        "COOK TIMOTHY PATRICK:0002088821:\n"
        "APPLE INC:0000320193:\n"
        "MUSK ELON:0001494730:\n"
        "HUANG JEN HSUN:0001197649:\n"
        "SOME UNRELATED TRUST:0000999999:\n",
        encoding="latin-1",
    )
    return path


def test_search_prefilter_keeps_every_matching_name(lookup_file):
    # The substring pre-filter exists purely to skip work, so it must not cost a hit.
    results = scan_cik_lookup(lookup_file, "Tim Cook", 12)
    assert {"0001214156", "0002088821"} <= {r["cik"] for r in results}


def test_search_prefilter_matches_regardless_of_name_order(lookup_file):
    ordered = scan_cik_lookup(lookup_file, "Cook Timothy", 12)
    reversed_query = scan_cik_lookup(lookup_file, "Timothy Cook", 12)
    assert {r["cik"] for r in ordered} == {r["cik"] for r in reversed_query}


def test_search_excludes_names_sharing_no_token(lookup_file):
    results = scan_cik_lookup(lookup_file, "Elon Musk", 12)
    names = {r["name"] for r in results}
    assert "MUSK ELON" in names
    assert "SOME UNRELATED TRUST" not in names


def test_fallback_search_bridges_public_and_edgar_legal_names(lookup_file):
    results = scan_cik_lookup(lookup_file, "Jensen Huang", 12)
    assert results[0]["cik"] == "0001197649"
    assert results[0]["score"] >= 92
