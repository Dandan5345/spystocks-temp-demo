import io
import zipfile

from app.sec_bulk import BulkOwnershipStore, quarter_url


FILES = {
    "SUBMISSION.tsv": "ACCESSION_NUMBER\tFILING_DATE\tPERIOD_OF_REPORT\tDOCUMENT_TYPE\tISSUERCIK\tISSUERNAME\tISSUERTRADINGSYMBOL\tREMARKS\n"
    "0000000001-26-000001\t30-JUN-2026\t29-JUN-2026\t4\t320193\tAPPLE INC\tAAPL\tplan trade\n",
    "REPORTINGOWNER.tsv": "ACCESSION_NUMBER\tRPTOWNERCIK\tRPTOWNERNAME\tRPTOWNER_RELATIONSHIP\tRPTOWNER_TITLE\tRPTOWNER_TXT\tRPTOWNER_CITY\tRPTOWNER_STATE\tRPTOWNER_STATE_DESC\n"
    "0000000001-26-000001\t1214156\tCOOK TIMOTHY D\tDirector Officer\tChief Executive Officer\t\tCUPERTINO\tCA\tUNITED STATES\n",
    "NONDERIV_TRANS.tsv": "ACCESSION_NUMBER\tNONDERIV_TRANS_SK\tSECURITY_TITLE\tTRANS_DATE\tTRANS_CODE\tTRANS_ACQUIRED_DISP_CD\tTRANS_SHARES\tTRANS_PRICEPERSHARE\tSHRS_OWND_FOLWNG_TRANS\tDIRECT_INDIRECT_OWNERSHIP\tNATURE_OF_OWNERSHIP\n"
    "0000000001-26-000001\t1\tCommon Stock\t29-JUN-2026\tS\tD\t100\t200.50\t900\tD\t\n",
    "NONDERIV_HOLDING.tsv": "ACCESSION_NUMBER\tNONDERIV_HOLDING_SK\tSECURITY_TITLE\tSHRS_OWND_FOLWNG_TRANS\tDIRECT_INDIRECT_OWNERSHIP\tNATURE_OF_OWNERSHIP\n",
    "DERIV_TRANS.tsv": "ACCESSION_NUMBER\tDERIV_TRANS_SK\tSECURITY_TITLE\tTRANS_DATE\tTRANS_CODE\tTRANS_ACQUIRED_DISP_CD\tTRANS_SHARES\tTRANS_PRICEPERSHARE\tSHRS_OWND_FOLWNG_TRANS\tUNDLYNG_SEC_TITLE\tUNDLYNG_SEC_SHARES\tCONV_EXERCISE_PRICE\tEXPIRATION_DATE\n",
    "DERIV_HOLDING.tsv": "ACCESSION_NUMBER\tDERIV_HOLDING_SK\tSECURITY_TITLE\tSHRS_OWND_FOLWNG_TRANS\tUNDLYNG_SEC_TITLE\tUNDLYNG_SEC_SHARES\tCONV_EXERCISE_PRICE\tEXPIRATION_DATE\n",
}


def make_zip(path):
    with zipfile.ZipFile(path, "w") as archive:
        for name, contents in FILES.items():
            archive.writestr(name, contents)


def test_import_and_profile_query_are_compatible_with_summary(tmp_path):
    archive = tmp_path / "quarter.zip"
    make_zip(archive)
    store = BulkOwnershipStore(tmp_path / "ownership.sqlite3")

    counts = store.import_zip(archive, "2026q2")
    filings = store.profile_filings("1214156")

    assert counts["submissions"] == 1
    assert store.has_owner("0001214156")
    assert len(filings) == 1
    assert filings[0]["owner"]["is_director"] is True
    assert filings[0]["issuer"]["ticker"] == "AAPL"
    assert filings[0]["non_derivative"][0]["value"] == 20050.0
    assert filings[0]["filing_date"] == "2026-06-30"
    assert store.profile_header("1214156")["current_role"]["title"] == "Chief Executive Officer"
    assert store.profile_header("1214156")["stats"] == {
        "filings_parsed": 1,
        "companies": 1,
        "transactions": 1,
        "purchased_value": 0.0,
        "disposed_value": 20050.0,
    }
    assert store.top_transactions("1214156", limit=5)[0]["value"] == 20050.0
    assert "/data/1/000000000126000001/" in store.top_transactions("1214156", limit=5)[0]["sec_url"]
    assert store.search_owner_candidates(["TIM", "COOK"])[0]["cik"] == "0001214156"


def test_reimport_replaces_quarter_instead_of_duplicating(tmp_path):
    archive = tmp_path / "quarter.zip"
    make_zip(archive)
    store = BulkOwnershipStore(tmp_path / "ownership.sqlite3")
    store.import_zip(archive, "2026q2")
    store.import_zip(archive, "2026q2")

    assert len(store.profile_filings("1214156")) == 1


def test_quarter_url_validates_input():
    assert quarter_url("2026Q2").endswith("/2026q2_form345.zip")
    assert "/structureddata/" in quarter_url("2026q1")
    try:
        quarter_url("2026-2")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid quarter accepted")
