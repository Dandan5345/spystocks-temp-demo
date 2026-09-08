from app.institution_client import (
    enrich_and_compare,
    normalize_institution_index,
    parse_information_table,
    parse_master_index,
    search_institutions,
    select_quarterly_filings,
)


MASTER = """Description: Master Index
CIK|Company Name|Form Type|Date Filed|Filename
--------------------------------------------------------------------------------
1067983|BERKSHIRE HATHAWAY INC|13F-HR|2026-08-14|edgar/data/1067983/example.txt
1234567|NOT A MANAGER|10-K|2026-08-14|edgar/data/1234567/example.txt
1423053|CITADEL ADVISORS LLC|13F-HR/A|2026-09-02|edgar/data/1423053/example.txt
"""

TABLE = """<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
<infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>1000</value><shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
<infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>500</value><shrsOrPrnAmt><sshPrnamt>5</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
</informationTable>"""


def test_master_index_builds_local_institution_index():
    rows = parse_master_index(MASTER)
    assert len(rows) == 2
    index = normalize_institution_index(rows)
    assert search_institutions(index, "Berkshire")[0]["cik"] == "0001067983"
    assert search_institutions(index, "Citadel Advisors")[0]["name"] == "CITADEL ADVISORS LLC"


def test_institution_search_rejects_weak_name_matches():
    index = normalize_institution_index(parse_master_index(MASTER))
    assert search_institutions(index, "Jade Citadel") == []


def test_information_table_aggregates_duplicate_security_rows():
    holdings = parse_information_table(TABLE)
    assert len(holdings) == 1
    assert holdings[0]["shares"] == 15
    assert holdings[0]["value"] == 1500


def test_quarterly_filing_selection_prefers_latest_amendment():
    filings = [
        {"form": "13F-HR", "reportDate": "2026-06-30", "filingDate": "2026-08-14"},
        {"form": "13F-HR/A", "reportDate": "2026-06-30", "filingDate": "2026-09-02"},
        {"form": "13F-HR", "reportDate": "2026-03-31", "filingDate": "2026-05-15"},
    ]
    selected = select_quarterly_filings(filings)
    assert [item["form"] for item in selected] == ["13F-HR/A", "13F-HR"]


def test_holdings_change_classification_and_ticker_enrichment():
    current = [
        {"issuer": "APPLE INC", "titleOfClass": "COM", "cusip": "A", "putCall": None, "shareType": "SH", "shares": 15, "value": 1500},
        {"issuer": "NEW CO", "titleOfClass": "COM", "cusip": "B", "putCall": None, "shareType": "SH", "shares": 2, "value": 200},
    ]
    previous = [
        {"issuer": "APPLE INC", "titleOfClass": "COM", "cusip": "A", "putCall": None, "shareType": "SH", "shares": 10, "value": 900},
        {"issuer": "OLD CO", "titleOfClass": "COM", "cusip": "C", "putCall": None, "shareType": "SH", "shares": 4, "value": 400},
    ]
    result = enrich_and_compare(current, previous, {"APPLE INC": "AAPL"})
    by_cusip = {item["cusip"]: item for item in result}
    assert by_cusip["A"]["status"] == "INCREASED"
    assert by_cusip["A"]["ticker"] == "AAPL"
    assert by_cusip["A"]["positionChangeValue"] == 500
    assert by_cusip["B"]["status"] == "NEW POSITION"
    assert by_cusip["C"]["status"] == "EXITED"
