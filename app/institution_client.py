from __future__ import annotations

from . import snapshots

import asyncio
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from .sec_client import CACHE_DIR, filing_archive_base, get_submissions, normalize_name, sec_get

INSTITUTION_INDEX_CACHE = CACHE_DIR / "institution-13f-index.json"
TICKER_CACHE = CACHE_DIR / "sec-company-tickers.json"
CIK_RE = re.compile(r"^\d{1,10}$")
THIRTEEN_F_FORMS = {"13F-HR", "13F-HR/A"}


class InstitutionError(Exception):
    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def quarter_sequence(count: int = 8) -> list[tuple[int, int]]:
    now = time.gmtime()
    year, quarter = now.tm_year, (now.tm_mon - 1) // 3 + 1
    result = []
    for _ in range(count):
        result.append((year, quarter))
        quarter -= 1
        if quarter == 0:
            quarter = 4
            year -= 1
    return result


def parse_master_index(text: str) -> list[dict[str, Any]]:
    rows = []
    started = False
    for raw in text.splitlines():
        if raw.startswith("---"):
            started = True
            continue
        if not started:
            continue
        parts = raw.split("|", 4)
        if len(parts) != 5 or parts[2] not in THIRTEEN_F_FORMS:
            continue
        cik, name, form, filed, filename = parts
        rows.append({"cik": cik.zfill(10), "name": name.strip(), "form": form, "filingDate": filed, "filename": filename})
    return rows


def normalize_institution_index(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda item: item.get("filingDate") or ""):
        cik = row["cik"]
        current = grouped.setdefault(cik, {"cik": cik, "name": row["name"], "lastFilingDate": row["filingDate"], "filingCount": 0})
        current["filingCount"] += 1
        if row["filingDate"] >= current["lastFilingDate"]:
            current["name"] = row["name"]
            current["lastFilingDate"] = row["filingDate"]
    for item in grouped.values():
        item["_search"] = normalize_name(item["name"])
    return sorted(grouped.values(), key=lambda item: item["name"])


def public_institution(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if not key.startswith("_")}


def search_institutions(index: list[dict[str, Any]], query: str, limit: int = 12) -> list[dict[str, Any]]:
    q = normalize_name(query)
    if len(q) < 2:
        return []
    tokens = q.split()
    ranked = []
    for institution in index:
        target = institution.get("_search") or normalize_name(institution.get("name", ""))
        target_tokens = target.split()
        exact = q == target or q == institution.get("cik", "").lstrip("0")
        prefix = target.startswith(q) or all(any(token.startswith(part) for token in target_tokens) for part in tokens)
        score = max(fuzz.ratio(q, target), fuzz.token_sort_ratio(q, target))
        if exact or prefix or score >= 82:
            ranked.append((300 if exact else 200 + score if prefix else score, institution))
    ranked.sort(key=lambda row: (-row[0], row[1]["name"]))
    return [public_institution(item) for _, item in ranked[:limit]]


def rows_from_recent(submissions: dict[str, Any]) -> list[dict[str, Any]]:
    return submissions.get("_all_filings", [])


def select_quarterly_filings(filings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    relevant = [filing for filing in filings if filing.get("form") in THIRTEEN_F_FORMS and filing.get("reportDate")]
    relevant.sort(key=lambda item: (item.get("reportDate") or "", item.get("filingDate") or "", item.get("form") == "13F-HR/A"), reverse=True)
    selected = []
    seen = set()
    for filing in relevant:
        period = filing["reportDate"]
        if period in seen:
            continue
        seen.add(period)
        selected.append(filing)
    return selected


def parse_number(value: str | None) -> float:
    try:
        return float((value or "0").replace(",", ""))
    except ValueError:
        return 0.0


def child_text(node: Any, name: str) -> str | None:
    child = node.find(name)
    return child.get_text(strip=True) if child and child.get_text(strip=True) else None


def parse_information_table(xml: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(xml, "xml")
    aggregated: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in soup.find_all("infoTable"):
        issuer = child_text(row, "nameOfIssuer")
        cusip = child_text(row, "cusip")
        title = child_text(row, "titleOfClass")
        put_call = child_text(row, "putCall")
        if not issuer or not cusip:
            continue
        key = (cusip, title or "", put_call or "", child_text(row, "sshPrnamtType") or "")
        item = aggregated.setdefault(key, {
            "issuer": issuer,
            "titleOfClass": title,
            "cusip": cusip,
            "figi": child_text(row, "figi"),
            "putCall": put_call,
            "shareType": child_text(row, "sshPrnamtType"),
            "shares": 0.0,
            "value": 0.0,
        })
        item["shares"] += parse_number(child_text(row, "sshPrnamt"))
        item["value"] += parse_number(child_text(row, "value"))
    return list(aggregated.values())


def enrich_and_compare(current: list[dict[str, Any]], previous: list[dict[str, Any]], tickers: dict[str, str]) -> list[dict[str, Any]]:
    previous_map = {(item["cusip"], item.get("titleOfClass") or "", item.get("putCall") or "", item.get("shareType") or ""): item for item in previous}
    holdings = []
    current_keys = set()
    for item in current:
        key = (item["cusip"], item.get("titleOfClass") or "", item.get("putCall") or "", item.get("shareType") or "")
        current_keys.add(key)
        before = previous_map.get(key)
        previous_shares = before["shares"] if before else 0
        previous_value = before["value"] if before else 0
        share_change = item["shares"] - previous_shares
        value_change = item["value"] - previous_value
        position_change_value = share_change * (item["value"] / item["shares"]) if item["shares"] else -previous_value
        if before is None:
            status = "NEW POSITION"
        elif share_change > 0:
            status = "INCREASED"
        elif share_change < 0:
            status = "REDUCED"
        else:
            status = "UNCHANGED"
        holdings.append({**item, "ticker": tickers.get(normalize_name(item["issuer"])), "previousShares": previous_shares, "previousValue": previous_value, "shareChange": share_change, "valueChange": value_change, "positionChangeValue": position_change_value, "changePercent": (share_change / previous_shares * 100) if previous_shares else None, "status": status})
    for key, item in previous_map.items():
        if key in current_keys:
            continue
        holdings.append({**item, "ticker": tickers.get(normalize_name(item["issuer"])), "previousShares": item["shares"], "previousValue": item["value"], "shares": 0, "value": 0, "shareChange": -item["shares"], "valueChange": -item["value"], "positionChangeValue": -item["value"], "changePercent": -100.0, "status": "EXITED"})
    holdings.sort(key=lambda item: (item["value"], item["previousValue"]), reverse=True)
    return holdings


class InstitutionClient:
    def __init__(self):
        self._memory: dict[str, tuple[float, Any]] = {}
        self._index_lock = asyncio.Lock()

    def _cached(self, key: str) -> Any | None:
        cached = self._memory.get(key)
        if cached and cached[0] > time.time():
            return cached[1]
        self._memory.pop(key, None)
        return None

    def _remember(self, key: str, value: Any, ttl: int) -> Any:
        self._memory[key] = (time.time() + ttl, value)
        return value

    async def index(self, *, force: bool = False) -> list[dict[str, Any]]:
        if not force and (cached := self._cached("index")) is not None:
            return cached
        async with self._index_lock:
            if not force and (cached := self._cached("index")) is not None:
                return cached
            ttl = 24 * 3600
            if not force and INSTITUTION_INDEX_CACHE.exists():
                try:
                    data = json.loads(INSTITUTION_INDEX_CACHE.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        if time.time() - INSTITUTION_INDEX_CACHE.stat().st_mtime >= ttl:
                            snapshots.schedule("institution-index", lambda: self.index(force=True))
                        for item in data:
                            item["_search"] = normalize_name(item.get("name", ""))
                        return self._remember("index", data, ttl)
                except (OSError, ValueError):
                    pass
            rows = []
            for year, quarter in quarter_sequence(8):
                url = f"https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/master.idx"
                try:
                    rows.extend(parse_master_index(await sec_get(url)))
                except Exception:
                    continue
            if not rows:
                raise InstitutionError("We couldn't build the SEC institutional manager index right now. Please try again.")
            data = normalize_institution_index(rows)
            temp = INSTITUTION_INDEX_CACHE.with_suffix(".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            temp.replace(INSTITUTION_INDEX_CACHE)
            return self._remember("index", data, ttl)

    async def ticker_map(self) -> dict[str, str]:
        if (cached := self._cached("tickers")) is not None:
            return cached
        ttl = 24 * 3600
        if TICKER_CACHE.exists() and time.time() - TICKER_CACHE.stat().st_mtime < ttl:
            try:
                raw = json.loads(TICKER_CACHE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = None
        else:
            raw = None
        if raw is None:
            raw = await sec_get("https://www.sec.gov/files/company_tickers.json", as_json=True)
            temp = TICKER_CACHE.with_suffix(".tmp")
            temp.write_text(json.dumps(raw), encoding="utf-8")
            temp.replace(TICKER_CACHE)
        grouped: dict[str, set[str]] = defaultdict(set)
        for item in raw.values() if isinstance(raw, dict) else raw:
            if item.get("title") and item.get("ticker"):
                grouped[normalize_name(item["title"])].add(item["ticker"])
        mapping = {name: next(iter(values)) for name, values in grouped.items() if len(values) == 1}
        return self._remember("tickers", mapping, ttl)

    async def search(self, query: str, limit: int = 12) -> list[dict[str, Any]]:
        return search_institutions(await self.index(), query, limit)

    async def _information_table(self, cik: str, filing: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        accession = filing.get("accessionNumber")
        if not accession:
            raise InstitutionError("This 13F filing has no accession number.")
        base = filing_archive_base(cik, accession)
        index = await sec_get(base + "index.json", as_json=True)
        names = [item.get("name") for item in index.get("directory", {}).get("item", []) if str(item.get("name", "")).lower().endswith(".xml")]
        candidates = [name for name in names if name and "primary" not in name.lower()]
        candidates.sort(key=lambda name: ("info" not in name.lower() and "table" not in name.lower(), name))
        for name in candidates:
            try:
                xml = await sec_get(base + name)
                parsed = parse_information_table(xml)
                if parsed:
                    return parsed, base + name
            except Exception:
                continue
        raise InstitutionError("The SEC filing did not contain a readable 13F information table.")

    async def profile(self, cik: str) -> dict[str, Any]:
        if not CIK_RE.fullmatch(cik):
            raise InstitutionError("CIK must be numeric.", status_code=400)
        return await snapshots.get("institution:v2:" + cik.zfill(10), lambda: self._build_profile(cik))

    async def overview(self, cik: str) -> dict[str, Any]:
        if not CIK_RE.fullmatch(cik):
            raise InstitutionError("CIK must be numeric.", status_code=400)
        cik = cik.zfill(10)
        hit = await snapshots.read("institution:v2:" + cik)
        if hit:
            if hit["fetchedAt"] + 14400 < time.time():
                snapshots.schedule("institution:" + cik, lambda: self.profile(cik))
            return {**{k: v for k, v in hit["data"].items() if not k.startswith("_")},
                    "status": "ready", "fetchedAt": hit["fetchedAt"]}
        match = next((item for item in await self.index() if item["cik"] == cik), None)
        if not match:
            raise InstitutionError("No institutional manager found for that CIK.", status_code=404)
        return {**public_institution(match), "status": "overview", "source": {
            "name": "U.S. SEC EDGAR", "officialUrl": f"https://www.sec.gov/edgar/browse/?CIK={cik}"}}

    async def _build_profile(self, cik: str) -> dict[str, Any]:
        if not CIK_RE.fullmatch(cik):
            raise InstitutionError("CIK must be numeric.", status_code=400)
        cik = cik.zfill(10)
        key = f"profile:{cik}"
        try:
            submissions = await get_submissions(cik)
        except Exception:
            raise InstitutionError("We couldn't reach SEC EDGAR right now. Please try again.")
        quarterly = select_quarterly_filings(rows_from_recent(submissions))
        if not quarterly:
            raise InstitutionError("No Form 13F holdings were found for this filer.", status_code=404)
        current, current_source = await self._information_table(cik, quarterly[0])
        previous, previous_source = ([], None)
        if len(quarterly) > 1:
            try:
                previous, previous_source = await self._information_table(cik, quarterly[1])
            except InstitutionError:
                pass
        try:
            tickers = await self.ticker_map()
        except Exception:
            tickers = {}
        holdings = enrich_and_compare(current, previous, tickers)
        current_holdings = [item for item in holdings if item["status"] != "EXITED"]
        additions = sorted((item for item in holdings if item["status"] in {"NEW POSITION", "INCREASED"}), key=lambda item: item["positionChangeValue"], reverse=True)[:10]
        reductions = sorted((item for item in holdings if item["status"] in {"REDUCED", "EXITED"}), key=lambda item: item["positionChangeValue"])[:10]
        address = submissions.get("addresses", {}).get("business") or submissions.get("addresses", {}).get("mailing") or {}
        recent_filings = rows_from_recent(submissions)[:20]
        normalized_filings = [{
            "form": filing.get("form"), "filingDate": filing.get("filingDate"), "reportDate": filing.get("reportDate"),
            "accessionNumber": filing.get("accessionNumber"),
            "sourceUrl": filing_archive_base(cik, filing["accessionNumber"]) + (filing.get("primaryDocument") or "") if filing.get("accessionNumber") else None,
        } for filing in recent_filings]
        normalized_13f = [{
            "form": filing.get("form"), "filingDate": filing.get("filingDate"), "reportDate": filing.get("reportDate"),
            "accessionNumber": filing.get("accessionNumber"),
            "sourceUrl": filing_archive_base(cik, filing["accessionNumber"]) + (filing.get("primaryDocument") or "") if filing.get("accessionNumber") else None,
        } for filing in quarterly[:12]]
        profile = {
            "id": cik,
            "cik": cik,
            "name": submissions.get("name"),
            "formerNames": submissions.get("formerNames") or [],
            "address": {key: value for key, value in address.items() if value},
            "latestQuarter": quarterly[0].get("reportDate"),
            "previousQuarter": quarterly[1].get("reportDate") if len(quarterly) > 1 else None,
            "stats": {
                "totalPortfolioValue": sum(item["value"] for item in current_holdings),
                "numberOfHoldings": len(current_holdings),
                "newPositions": len([item for item in holdings if item["status"] == "NEW POSITION"]),
                "exitedPositions": len([item for item in holdings if item["status"] == "EXITED"]),
            },
            "holdings": {"total": len(holdings), "items": holdings[:100], "hasMore": len(holdings) > 100},
            "_allHoldings": holdings,
            "topHoldings": sorted(current_holdings, key=lambda item: item["value"], reverse=True)[:10],
            "biggestAdditions": additions,
            "biggestReductions": reductions,
            "recentFilings": normalized_filings,
            "thirteenFFilings": normalized_13f,
            "source": {"name": "U.S. SEC EDGAR", "currentInformationTable": current_source, "previousInformationTable": previous_source, "officialUrl": f"https://www.sec.gov/edgar/browse/?CIK={cik}"},
        }
        return self._remember(key, profile, 4 * 3600)

    async def holdings(self, cik: str, offset: int, limit: int) -> dict[str, Any]:
        profile = await self.profile(cik)
        full = profile.get("_allHoldings", profile["holdings"]["items"])
        return {"total": len(full), "offset": offset, "items": full[offset:offset + limit], "hasMore": offset + limit < len(full)}


institution_client = InstitutionClient()
