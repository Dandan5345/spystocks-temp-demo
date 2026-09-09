from __future__ import annotations

import asyncio
import csv
import io
import os
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from . import cache

FEC_ROOT = "https://api.open.fec.gov/v1"
FEC_TTL = 6 * 60 * 60


class FECError(Exception):
    pass


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _latest_reports(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the newest amendment for each committee/coverage period."""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("committee_id") or ""), str(row.get("coverage_end_date") or ""))
        if not key[0]:
            continue
        previous = latest.get(key)
        if previous is None or str(row.get("receipt_date") or "") > str(previous.get("receipt_date") or ""):
            latest[key] = row
    per_committee: dict[str, dict[str, Any]] = {}
    for row in latest.values():
        committee_id = str(row.get("committee_id"))
        previous = per_committee.get(committee_id)
        if previous is None or str(row.get("coverage_end_date") or "") > str(previous.get("coverage_end_date") or ""):
            per_committee[committee_id] = row
    return list(per_committee.values())


def _contribution(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row.get("contributor_name"),
        "amount": _number(row.get("contribution_receipt_amount")),
        "date": row.get("contribution_receipt_date"),
        "employer": row.get("contributor_employer"),
        "occupation": row.get("contributor_occupation"),
        "entityType": row.get("entity_type"),
        "committeeId": row.get("committee_id"),
        "sourceUrl": row.get("pdf_url"),
    }


class FECClient:
    def __init__(self, api_key: str | None = None, *, transport: httpx.AsyncBaseTransport | None = None):
        self.api_key = api_key if api_key is not None else os.getenv("FEC_API_KEY", "DEMO_KEY")
        self.transport = transport
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(18.0),
                follow_redirects=True,
                headers={"Accept": "application/json", "User-Agent": "Information Check System contact@example.com"},
                transport=self.transport,
            )
        return self._client

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._http().get(f"{FEC_ROOT}/{path.lstrip('/')}", params={"api_key": self.api_key, **params})
            if response.status_code == 429:
                raise FECError("FEC rate limit reached; the saved campaign record will refresh shortly.")
            response.raise_for_status()
            return response.json()
        except FECError:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise FECError("The official FEC record is temporarily unavailable.") from error

    async def _bundlers(self, filings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        async def read_filing(filing: dict[str, Any]) -> list[dict[str, Any]]:
            csv_url = str(filing.get("csv_url") or "")
            parsed = urlparse(csv_url)
            if parsed.scheme != "https" or parsed.netloc != "docquery.fec.gov":
                return []
            try:
                response = await self._http().get(csv_url)
                response.raise_for_status()
                rows = csv.reader(io.StringIO(response.text))
                result = []
                for row in rows:
                    if len(row) < 25 or row[0] != "SA3L":
                        continue
                    full_name = " ".join(value for value in (row[8], row[9], row[7], row[11]) if value).strip()
                    result.append({
                        "name": full_name,
                        "amount": _number(row[20]),
                        "employer": row[23] or None,
                        "occupation": row[24] or None,
                        "filedAt": filing.get("receipt_date"),
                        "committeeName": filing.get("committee_name"),
                        "sourceUrl": filing.get("pdf_url") or csv_url,
                    })
                return result
            except (httpx.HTTPError, csv.Error):
                return []

        pages = await asyncio.gather(*[read_filing(row) for row in filings[:4]], return_exceptions=True)
        unique: dict[tuple[str, float, str], dict[str, Any]] = {}
        for page in pages:
            if isinstance(page, Exception):
                continue
            for row in page:
                unique[(str(row.get("name")), _number(row.get("amount")), str(row.get("employer") or ""))] = row
        return sorted(unique.values(), key=lambda row: _number(row.get("amount")), reverse=True)[:30]

    async def campaign_finance(self, candidate_ids: list[str], cycle: int | None = None) -> dict[str, Any]:
        ids = sorted({str(value).upper() for value in candidate_ids if str(value).upper()[:1] in {"H", "S", "P"}})
        if not ids:
            return {"available": False, "message": "No FEC candidate identifier is linked to this profile."}
        year = cycle or time.gmtime().tm_year
        cycle = year if year % 2 == 0 else year + 1
        key = f"fec-campaign:v2:{cycle}:{','.join(ids)}"

        async def fetch() -> dict[str, Any]:
            committee_pages = await asyncio.gather(*[
                self._get(f"candidate/{candidate_id}/committees/", {"cycle": cycle, "per_page": 100})
                for candidate_id in ids
            ], return_exceptions=True)
            committee_rows: list[dict[str, Any]] = []
            for page in committee_pages:
                if not isinstance(page, Exception):
                    committee_rows.extend(page.get("results") or [])
            unique = {str(row.get("committee_id")): row for row in committee_rows if row.get("committee_id")}
            committees = list(unique.values())
            committee_ids = list(unique)
            if not committee_ids:
                return {"available": False, "cycle": cycle, "message": "No campaign committee was reported for this election cycle."}

            report_pages, donors_page, organizations_page, bundlers_page = await asyncio.gather(
                asyncio.gather(*[
                    self._get(f"committee/{committee_id}/reports/", {"cycle": cycle, "per_page": 20})
                    for committee_id in committee_ids
                ], return_exceptions=True),
                self._get("schedules/schedule_a/", {
                    "committee_id": committee_ids, "two_year_transaction_period": cycle,
                    "is_individual": True, "per_page": 24, "sort": "-contribution_receipt_amount",
                }),
                self._get("schedules/schedule_a/", {
                    "committee_id": committee_ids, "two_year_transaction_period": cycle,
                    "per_page": 100, "sort": "-contribution_receipt_amount",
                }),
                self._get("filings/", {"committee_id": committee_ids, "form_type": "F3L", "per_page": 20}),
                return_exceptions=True,
            )
            reports: list[dict[str, Any]] = []
            if not isinstance(report_pages, Exception):
                for page in report_pages:
                    if not isinstance(page, Exception):
                        reports.extend(page.get("results") or [])
            latest = _latest_reports(reports)
            totals = {
                "raised": sum(_number(row.get("total_receipts_ytd")) for row in latest),
                "contributions": sum(_number(row.get("total_contributions_ytd")) for row in latest),
                "spent": sum(_number(row.get("total_disbursements_ytd")) for row in latest),
                "cashOnHand": sum(_number(row.get("cash_on_hand_end_period")) for row in latest),
                "individualItemized": sum(_number(row.get("individual_itemized_contributions_ytd")) for row in latest),
                "otherCommittees": sum(_number(row.get("other_political_committee_contributions_ytd")) for row in latest),
            }
            donors = [] if isinstance(donors_page, Exception) else [
                _contribution(row) for row in donors_page.get("results") or []
                if row.get("contributor_name") and _number(row.get("contribution_receipt_amount")) > 0
            ]
            organization_types = {"PAC", "PTY", "ORG", "CCM", "COM"}
            organizations = [] if isinstance(organizations_page, Exception) else [
                _contribution(row) for row in organizations_page.get("results") or []
                if row.get("contributor_name") and row.get("entity_type") in organization_types
                and _number(row.get("contribution_receipt_amount")) > 0
            ][:24]
            bundler_filings = [] if isinstance(bundlers_page, Exception) else bundlers_page.get("results") or []
            bundlers = await self._bundlers(bundler_filings)
            return {
                "available": True,
                "cycle": cycle,
                "candidateIds": ids,
                "totals": totals,
                "coverageThrough": max((str(row.get("coverage_end_date") or "") for row in latest), default=None),
                "committees": [{
                    "id": row.get("committee_id"), "name": row.get("name"),
                    "designation": row.get("designation_full"), "type": row.get("committee_type_full"),
                    "website": row.get("website"), "treasurer": row.get("treasurer_name"),
                    "officialUrl": f"https://www.fec.gov/data/committee/{row.get('committee_id')}/?cycle={cycle}",
                } for row in committees],
                "individualDonors": donors,
                "pacAndOrganizationContributions": organizations,
                "bundlerDisclosures": bundlers,
                "source": {"name": "Federal Election Commission", "officialUrl": f"https://www.fec.gov/data/candidates/?election_year={cycle}"},
            }

        return await cache.cached(key, fetch, ttl=FEC_TTL)


fec_client = FECClient()
