from __future__ import annotations

import asyncio
import io
import json
import os
import platform
import re
import subprocess
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from .congress_client import normalize_name

HOUSE_ROOT = "https://disclosures-clerk.house.gov"
SENATE_ROOT = "https://efdsearch.senate.gov"
CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "disclosures"
VISION_OCR = Path(__file__).with_name("vision_ocr.swift")
DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
MONEY_RE = re.compile(r"\$\s*([\d,]+)\s*[-–—]\s*\$?\s*([\d,]+)")
TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,5})\)(?!.*\([A-Z][A-Z0-9.\-]{0,5}\))")
OWNER_MAP = {
    "": "Self", "self": "Self", "sp": "Spouse", "spouse": "Spouse",
    "dc": "Dependent Child", "dependent child": "Dependent Child",
    "jt": "Joint", "joint": "Joint",
}
TYPE_MAP = {
    "p": "Purchase", "purchase": "Purchase",
    "s": "Sale", "sale": "Sale", "sale full": "Sale", "sale partial": "Sale",
    "e": "Exchange", "exchange": "Exchange",
}


class DisclosureError(Exception):
    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _iso(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def normalize_owner(value: str | None) -> str:
    key = re.sub(r"[^a-z ]", "", (value or "").lower()).strip()
    return OWNER_MAP.get(key, value.strip() if value and value.strip() else "Self")


def normalize_transaction_type(value: str | None) -> str:
    raw = (value or "").strip()
    key = re.sub(r"[^a-z ]", " ", raw.lower())
    key = " ".join(key.split())
    for candidate, label in TYPE_MAP.items():
        if key == candidate or key.startswith(candidate + " "):
            return label
    return raw or "Other"


def normalize_amount(value: str | None) -> dict[str, Any]:
    label = " ".join((value or "Not disclosed").replace("—", "–").split())
    match = MONEY_RE.search(label)
    if not match:
        return {"label": label, "min": None, "max": None}
    low, high = (int(part.replace(",", "")) for part in match.groups())
    return {"label": f"${low:,}–${high:,}", "min": low, "max": high}


def explicit_ticker(asset: str) -> str | None:
    """Only accept a ticker explicitly printed in the official asset description."""
    match = TICKER_RE.search(asset or "")
    return match.group(1) if match else None


def _transaction(
    *, source_type: str, document_id: str, document_url: str, asset: str,
    owner: str | None, transaction_type: str | None, transaction_date: str | None,
    notification_date: str | None, amount: str | None, filing_date: str | None = None,
) -> dict[str, Any]:
    source_name = "House Financial Disclosures" if source_type == "house" else "Senate eFD"
    badge = "OFFICIAL HOUSE DISCLOSURE" if source_type == "house" else "OFFICIAL SENATE DISCLOSURE"
    asset = " ".join((asset or "Unknown asset").split())
    normalized_type = normalize_transaction_type(transaction_type)
    normalized_amount = normalize_amount(amount)
    return {
        "id": f"{source_type}:{document_id}:{transaction_date or ''}:{normalized_type}:{asset}:{normalized_amount['label']}",
        "reportType": "PTR",
        "asset": asset,
        "ticker": explicit_ticker(asset),
        "transactionType": normalized_type,
        "transactionDate": _iso(transaction_date),
        "notificationDate": _iso(notification_date),
        "filingDate": _iso(filing_date),
        "amount": normalized_amount,
        "owner": normalize_owner(owner),
        "source": {"name": source_name, "badge": badge, "documentId": document_id, "documentUrl": document_url},
    }


def summarize(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    purchases = [row for row in transactions if row["transactionType"] == "Purchase"]
    sales = [row for row in transactions if row["transactionType"] == "Sale"]

    def bounds(rows: list[dict[str, Any]]) -> dict[str, int] | None:
        amounts = [row["amount"] for row in rows if row["amount"]["min"] is not None]
        if not amounts:
            return None
        return {"min": sum(row["min"] for row in amounts), "max": sum(row["max"] for row in amounts)}

    return {
        "totalTrades": len(transactions),
        "purchases": len(purchases),
        "sales": len(sales),
        "mostRecentTrade": transactions[0]["transactionDate"] if transactions else None,
        "spouseTrades": sum(row["owner"] == "Spouse" for row in transactions),
        "purchaseRange": bounds(purchases),
        "saleRange": bounds(sales),
    }


def _group_ocr_rows(observations: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = []
    for item in sorted(observations, key=lambda row: (int(row.get("page", 1)), -float(row.get("y", 0)), float(row.get("x", 0)))):
        if rows and int(rows[-1][0].get("page", 1)) == int(item.get("page", 1)) and abs(float(rows[-1][0].get("y", 0)) - float(item.get("y", 0))) <= .008:
            rows[-1].append(item)
        else:
            rows.append([item])
    for row in rows:
        row.sort(key=lambda item: float(item.get("x", 0)))
    return rows


def parse_house_ocr(observations: list[dict[str, Any]], document_id: str, document_url: str) -> list[dict[str, Any]]:
    rows = _group_ocr_rows(observations)
    result: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def finish() -> None:
        nonlocal current
        if not current:
            return
        current["asset"] = current["asset"].strip()
        result.append(_transaction(source_type="house", document_id=document_id, document_url=document_url, **current))
        current = None

    for row in rows:
        joined = " ".join(str(item.get("text", "")) for item in row)
        upper = joined.upper()
        if any(marker in upper for marker in ("INITIAL PUBLIC OFFERINGS", "CERTIFICATION AND SIGNATURE", "* FOR THE COMPLETE LIST")):
            finish()
            continue
        dates = DATE_RE.findall(joined)
        # Official House PTR rows have transaction and notification dates on the same baseline.
        if len(dates) >= 2:
            finish()
            columns = {"owner": [], "asset": [], "type": [], "amount": []}
            for item in row:
                x, text = float(item.get("x", 0)), str(item.get("text", "")).strip()
                if not text or DATE_RE.search(text):
                    continue
                if .075 <= x < .15:
                    columns["owner"].append(text)
                elif .145 <= x < .41:
                    columns["asset"].append(text)
                elif .40 <= x < .53:
                    columns["type"].append(text)
                elif x >= .70:
                    columns["amount"].append(text)
            if not columns["asset"]:
                continue
            current = {
                "asset": " ".join(columns["asset"]), "owner": " ".join(columns["owner"]),
                "transaction_type": " ".join(columns["type"]), "transaction_date": dates[0],
                "notification_date": dates[1], "amount": " ".join(columns["amount"]), "filing_date": None,
            }
        elif current:
            for item in row:
                x, text = float(item.get("x", 0)), str(item.get("text", "")).strip()
                if text.upper().startswith("DESCRIPTION"):
                    if not current["transaction_type"]:
                        description = text.upper()
                        if "PURCHASED" in description:
                            current["transaction_type"] = "P"
                        elif "SOLD" in description or "SALE" in description:
                            current["transaction_type"] = "S"
                elif .145 <= x < .41 and not text.upper().startswith("FILING STATUS"):
                    current["asset"] += " " + text
                elif .70 <= x < .84:
                    current["amount"] += " " + text
    finish()
    return result


def parse_house_text(text: str, document_id: str, document_url: str) -> list[dict[str, Any]]:
    """Fallback for text-layer House PDFs; coordinates/OCR are preferred for scanned PDFs."""
    results = []
    line_re = re.compile(r"^(SP|DC|JT)?\s*(.+?)\s{2,}(P|S(?:\s*\([^)]*\))?|E)\s+(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}/\d{1,2}/\d{4})\s+(\$[\d,]+\s*[-–]\s*\$?[\d,]+)", re.I)
    for line in text.splitlines():
        match = line_re.search(line.strip())
        if match:
            owner, asset, kind, tx_date, notice_date, amount = match.groups()
            results.append(_transaction(source_type="house", document_id=document_id, document_url=document_url, asset=asset, owner=owner, transaction_type=kind, transaction_date=tx_date, notification_date=notice_date, amount=amount))
    return results


def parse_senate_report(html: str, document_id: str, document_url: str, filing_date: str | None = None) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    result = []
    for table in soup.select("table"):
        headers = [" ".join(cell.get_text(" ", strip=True).lower().split()) for cell in table.select("thead th")]
        if not headers or not any("transaction date" in cell for cell in headers):
            continue
        for tr in table.select("tbody tr"):
            cells = [" ".join(cell.get_text(" ", strip=True).split()) for cell in tr.select("td")]
            if len(cells) < 6:
                continue
            row = dict(zip(headers, cells))
            asset_key = next((key for key in headers if key in {"asset", "asset name"} or "asset name" in key), "asset")
            ticker_key = next((key for key in headers if "ticker" in key), "")
            asset = row.get(asset_key, "")
            official_ticker = row.get(ticker_key, "").strip().upper() if ticker_key else ""
            if official_ticker and official_ticker not in {"--", "N/A", "N/A."} and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,5}", official_ticker):
                asset = f"{asset} ({official_ticker})"
            result.append(_transaction(
                source_type="senate", document_id=document_id, document_url=document_url,
                asset=asset, owner=row.get("owner"),
                transaction_type=next((row[key] for key in headers if key == "type" or "transaction type" in key), None),
                transaction_date=next((row[key] for key in headers if "transaction date" in key), None),
                notification_date=next((row[key] for key in headers if "notification" in key), None),
                amount=next((row[key] for key in headers if "amount" in key), None), filing_date=filing_date,
            ))
    return result


class DisclosureClient:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport
        self._memory: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self.user_agent = os.getenv("DISCLOSURE_USER_AGENT", "Information Check System/1.0 (official public disclosures; contact: contact@example.com)")

    async def _throttle(self) -> None:
        delay = max(0, .35 - (time.monotonic() - self._last_request))
        if delay:
            await asyncio.sleep(delay)
        self._last_request = time.monotonic()

    async def _request(self, client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
        await self._throttle()
        try:
            response = await client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise DisclosureError("The official disclosure source is temporarily unavailable.") from error
        return response

    def _cached(self, key: str) -> Any | None:
        cached = self._memory.get(key)
        if cached and cached[0] > time.time():
            return cached[1]
        self._memory.pop(key, None)
        return None

    def _remember(self, key: str, value: Any, seconds: int = 21600) -> Any:
        self._memory[key] = (time.time() + seconds, value)
        return value

    async def _house_index(self, year: int, client: httpx.AsyncClient) -> list[dict[str, str]]:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = CACHE_DIR / f"house-{year}-index.txt"
        ttl = int(os.getenv("DISCLOSURE_INDEX_CACHE_HOURS", "24")) * 3600
        if path.exists() and time.time() - path.stat().st_mtime < ttl:
            raw = path.read_text(encoding="utf-8", errors="replace")
        else:
            response = await self._request(client, "GET", f"{HOUSE_ROOT}/public_disc/financial-pdfs/{year}FD.txt")
            if response.status_code == 404:
                return []
            response.raise_for_status()
            raw = response.text
            temp = path.with_suffix(".tmp")
            temp.write_text(raw, encoding="utf-8")
            temp.replace(path)
        lines = raw.splitlines()
        if not lines:
            return []
        headers = lines[0].lstrip("\ufeff").split("\t")
        return [dict(zip(headers, line.split("\t"))) for line in lines[1:] if line.strip()]

    @staticmethod
    def _house_match(row: dict[str, str], profile: dict[str, Any]) -> bool:
        if normalize_name(row.get("Last", "")) != normalize_name(profile.get("lastName", "")):
            return False
        first = normalize_name(row.get("First", ""))
        expected = normalize_name(profile.get("firstName", ""))
        if expected and first and first.split()[0] != expected.split()[0]:
            return False
        state_dst = row.get("StateDst", "")
        state = profile.get("currentStateCode") or profile.get("currentState") or ""
        if len(str(state)) == 2 and state_dst and not state_dst.startswith(str(state).upper()):
            return False
        return True

    async def _house_pdf_transactions(self, filing: dict[str, str], client: httpx.AsyncClient) -> list[dict[str, Any]]:
        year, doc_id = filing["Year"], filing["DocID"]
        url = f"{HOUSE_ROOT}/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        pdf_path = CACHE_DIR / f"house-{doc_id}.pdf"
        ocr_path = CACHE_DIR / f"house-{doc_id}.ocr.jsonl"
        if not pdf_path.exists():
            response = await self._request(client, "GET", url)
            if response.status_code != 200 or not response.content.startswith(b"%PDF"):
                return []
            temp = pdf_path.with_suffix(".tmp")
            temp.write_bytes(response.content)
            temp.replace(pdf_path)
        try:
            text = "\n".join(page.extract_text(extraction_mode="layout") or "" for page in PdfReader(str(pdf_path)).pages)
        except Exception:
            text = ""
        parsed = parse_house_text(text, doc_id, url) if text.strip() else []
        if parsed:
            for transaction in parsed:
                transaction["filingDate"] = _iso(filing.get("FilingDate"))
            return parsed
        observations: list[dict[str, Any]] = []
        if ocr_path.exists():
            observations = [json.loads(line) for line in ocr_path.read_text().splitlines() if line.strip()]
        elif platform.system() == "Darwin" and VISION_OCR.exists():
            try:
                process = await asyncio.to_thread(subprocess.run, ["swift", str(VISION_OCR), str(pdf_path)], capture_output=True, text=True, timeout=90, check=True)
                ocr_path.write_text(process.stdout, encoding="utf-8")
                observations = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
            except (OSError, subprocess.SubprocessError, ValueError):
                observations = []
        parsed = parse_house_ocr(observations, doc_id, url)
        for transaction in parsed:
            transaction["filingDate"] = _iso(filing.get("FilingDate"))
        return parsed

    async def house(self, profile: dict[str, Any]) -> dict[str, Any]:
        key = f"house:{profile.get('bioguideId')}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        years = range(date.today().year, max(2012, date.today().year - 4), -1)
        filings: list[dict[str, Any]] = []
        transactions: list[dict[str, Any]] = []
        async with self._lock, httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": self.user_agent}, transport=self.transport) as client:
            for year in years:
                for row in await self._house_index(year, client):
                    if not self._house_match(row, profile):
                        continue
                    is_ptr = row.get("FilingType", "").upper().startswith("P")
                    document_url = f"{HOUSE_ROOT}/public_disc/{'ptr-pdfs' if is_ptr else 'financial-pdfs'}/{row['Year']}/{row['DocID']}.pdf"
                    filing = {"documentId": row["DocID"], "reportType": "PTR" if is_ptr else "ANNUAL", "filingDate": _iso(row.get("FilingDate")), "year": int(row["Year"]), "documentUrl": document_url}
                    filings.append(filing)
                    if is_ptr:
                        transactions.extend(await self._house_pdf_transactions(row, client))
        transactions.sort(key=lambda row: row.get("transactionDate") or row.get("filingDate") or "", reverse=True)
        return self._remember(key, self._response("house", transactions, filings))

    async def senate(self, profile: dict[str, Any]) -> dict[str, Any]:
        key = f"senate:{profile.get('bioguideId')}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        headers = {"User-Agent": self.user_agent, "Referer": f"{SENATE_ROOT}/search/home/"}
        async with self._lock, httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers, transport=self.transport) as client:
            home = await self._request(client, "GET", f"{SENATE_ROOT}/search/home/")
            if home.status_code in {401, 403, 429}:
                return self._remember(key, self._unavailable("senate", "Senate eFD is currently blocking automated access from this server."), 1800)
            soup = BeautifulSoup(home.text, "html.parser")
            token = soup.select_one('input[name="csrfmiddlewaretoken"]')
            if not token:
                return self._remember(key, self._unavailable("senate", "Senate eFD did not provide its official search agreement."), 1800)
            accepted = await self._request(client, "POST", f"{SENATE_ROOT}/search/home/", data={"csrfmiddlewaretoken": token.get("value", ""), "prohibition_agreement": "1"})
            if accepted.status_code >= 400:
                return self._remember(key, self._unavailable("senate", "Senate eFD did not accept the public-search session."), 1800)
            data = {
                "draw": "1", "start": "0", "length": "100", "report_types": "11", "filer_types": "1",
                "first_name": profile.get("firstName") or "", "last_name": profile.get("lastName") or "",
                "submitted_start_date": "01/01/2012", "submitted_end_date": date.today().strftime("%m/%d/%Y"),
                "candidate_state": "", "senator_state": "", "office_id": "",
                "csrfmiddlewaretoken": client.cookies.get("csrftoken", ""),
            }
            rows: list[Any] = []
            start = 0
            while True:
                data["start"] = str(start)
                search = await self._request(client, "POST", f"{SENATE_ROOT}/search/report/data/", data=data, headers={**headers, "Referer": f"{SENATE_ROOT}/search/", "X-Requested-With": "XMLHttpRequest"})
                if search.status_code in {401, 403, 429}:
                    return self._remember(key, self._unavailable("senate", "Senate eFD is currently blocking automated access from this server."), 1800)
                try:
                    payload = search.json()
                    page_rows = payload.get("data", [])
                except ValueError:
                    page_rows, payload = [], {}
                rows.extend(page_rows)
                start += len(page_rows)
                total = int(payload.get("recordsFiltered") or payload.get("recordsTotal") or len(rows))
                if not page_rows or start >= total or start >= 1000:
                    break
            filings, transactions = [], []
            for row in rows:
                if not isinstance(row, list) or len(row) < 6:
                    continue
                first, last, _office, _state, filed, report_html = row[:6]
                if normalize_name(last) != normalize_name(profile.get("lastName", "")) or normalize_name(first).split()[:1] != normalize_name(profile.get("firstName", "")).split()[:1]:
                    continue
                link = BeautifulSoup(str(report_html), "html.parser").select_one("a[href]")
                if not link or "/ptr/" not in link.get("href", ""):
                    continue
                url = urljoin(SENATE_ROOT, link["href"])
                doc_id = url.rstrip("/").split("/")[-1]
                filings.append({"documentId": doc_id, "reportType": "PTR", "filingDate": _iso(filed), "documentUrl": url})
                report = await self._request(client, "GET", url, headers={**headers, "Referer": f"{SENATE_ROOT}/search/"})
                if report.status_code == 200:
                    transactions.extend(parse_senate_report(report.text, doc_id, url, filed))
        transactions.sort(key=lambda row: row.get("transactionDate") or row.get("filingDate") or "", reverse=True)
        return self._remember(key, self._response("senate", transactions, filings))

    @staticmethod
    def _response(source_type: str, transactions: list[dict[str, Any]], filings: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "available": True, "sourceType": source_type, "transactions": transactions,
            "summary": summarize(transactions), "filings": filings,
            "annualReports": [row for row in filings if row["reportType"] == "ANNUAL"],
            "annualData": {"assets": [], "liabilities": [], "positions": [], "income": []},
            "source": {"name": "House Financial Disclosures" if source_type == "house" else "Senate eFD", "officialUrl": f"{HOUSE_ROOT}/FinancialDisclosure" if source_type == "house" else f"{SENATE_ROOT}/search/"},
        }

    @staticmethod
    def _unavailable(source_type: str, message: str) -> dict[str, Any]:
        response = DisclosureClient._response(source_type, [], [])
        response.update({"available": False, "message": message})
        return response

    async def disclosures(self, profile: dict[str, Any]) -> dict[str, Any]:
        chamber = profile.get("currentChamber")
        if chamber == "House":
            return await self.house(profile)
        if chamber == "Senate":
            return await self.senate(profile)
        return self._unavailable("unknown", "No House or Senate chamber could be identified for this member.")


disclosure_client = DisclosureClient()
