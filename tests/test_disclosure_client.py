import pytest

from app.disclosure_client import (
    DisclosureClient,
    explicit_ticker,
    normalize_amount,
    parse_house_ocr,
    parse_senate_report,
    summarize,
)


def test_house_ocr_is_normalized_from_official_columns():
    observations = [
        {"page": 1, "x": .10, "y": .57, "text": "SP"},
        {"page": 1, "x": .16, "y": .57, "text": "Apple Inc. - Common Stock (AAPL)"},
        {"page": 1, "x": .42, "y": .57, "text": "S (partial)"},
        {"page": 1, "x": .53, "y": .57, "text": "10/22/2025 10/22/2025"},
        {"page": 1, "x": .73, "y": .57, "text": "$100,001-"},
        {"page": 1, "x": .16, "y": .55, "text": "[ST]"},
        {"page": 1, "x": .73, "y": .55, "text": "$250,000"},
        {"page": 1, "x": .03, "y": .45, "text": "INITIAL PUBLIC OFFERINGS"},
    ]
    rows = parse_house_ocr(observations, "20033337", "https://disclosures-clerk.house.gov/example.pdf")
    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAPL"
    assert rows[0]["transactionType"] == "Sale"
    assert rows[0]["owner"] == "Spouse"
    assert rows[0]["transactionDate"] == "2025-10-22"
    assert rows[0]["amount"] == {"label": "$100,001–$250,000", "min": 100001, "max": 250000}
    assert rows[0]["source"]["badge"] == "OFFICIAL HOUSE DISCLOSURE"


def test_senate_html_uses_same_model_and_only_official_ticker():
    html = """
      <table><thead><tr><th>Transaction Date</th><th>Owner</th><th>Ticker</th>
      <th>Asset Name</th><th>Asset Type</th><th>Type</th><th>Amount</th><th>Comment</th></tr></thead>
      <tbody><tr><td>04/02/2025</td><td>Self</td><td>MSFT</td><td>Microsoft Corporation</td>
      <td>Stock</td><td>Purchase</td><td>$15,001 - $50,000</td><td></td></tr></tbody></table>
    """
    rows = parse_senate_report(html, "abc", "https://efdsearch.senate.gov/search/view/ptr/abc/", "04/15/2025")
    assert rows[0]["asset"] == "Microsoft Corporation (MSFT)"
    assert rows[0]["ticker"] == "MSFT"
    assert rows[0]["filingDate"] == "2025-04-15"
    assert rows[0]["source"]["badge"] == "OFFICIAL SENATE DISCLOSURE"


def test_ticker_is_never_inferred_from_company_name():
    assert explicit_ticker("The Walt Disney Company") is None
    assert explicit_ticker("The Walt Disney Company (DIS)") == "DIS"
    assert explicit_ticker("Municipal bond (general obligation)") is None


def test_summary_counts_and_sums_range_bounds_without_false_precision():
    rows = [
        {"transactionType": "Purchase", "owner": "Self", "transactionDate": "2025-04-02", "filingDate": None, "amount": normalize_amount("$15,001-$50,000")},
        {"transactionType": "Purchase", "owner": "Spouse", "transactionDate": "2025-03-01", "filingDate": None, "amount": normalize_amount("$1,001-$15,000")},
        {"transactionType": "Sale", "owner": "Spouse", "transactionDate": "2025-02-01", "filingDate": None, "amount": normalize_amount("not disclosed")},
    ]
    result = summarize(rows)
    assert result["totalTrades"] == 3
    assert result["purchases"] == 2
    assert result["sales"] == 1
    assert result["spouseTrades"] == 2
    assert result["purchaseRange"] == {"min": 16002, "max": 65000}
    assert result["saleRange"] is None


@pytest.mark.asyncio
async def test_chamber_selects_only_its_official_source(monkeypatch):
    client = DisclosureClient()
    called = []

    async def house(profile):
        called.append(("house", profile["bioguideId"]))
        return {"sourceType": "house"}

    async def senate(profile):
        called.append(("senate", profile["bioguideId"]))
        return {"sourceType": "senate"}

    monkeypatch.setattr(client, "house", house)
    monkeypatch.setattr(client, "senate", senate)
    assert await client.disclosures({"currentChamber": "House", "bioguideId": "P000197"}) == {"sourceType": "house"}
    assert await client.disclosures({"currentChamber": "Senate", "bioguideId": "T000278"}) == {"sourceType": "senate"}
    assert called == [("house", "P000197"), ("senate", "T000278")]


def test_future_annual_report_shape_is_present():
    data = DisclosureClient._response("house", [], [{"reportType": "ANNUAL"}])
    assert data["annualData"] == {"assets": [], "liabilities": [], "positions": [], "income": []}
    assert len(data["annualReports"]) == 1
