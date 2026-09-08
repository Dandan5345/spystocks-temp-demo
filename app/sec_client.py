from __future__ import annotations

import asyncio
import json
import mmap
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from . import cache
from .sec_bulk import DEFAULT_DB as BULK_DB, ownership_store

SEC_ROOT = "https://www.sec.gov"
DATA_ROOT = "https://data.sec.gov"
CIK_LOOKUP_URL = f"{SEC_ROOT}/Archives/edgar/cik-lookup-data.txt"
CACHE_DIR = Path(__file__).resolve().parents[1] / "data"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CIK_CACHE = CACHE_DIR / "cik-lookup-data.txt"

FORM_TYPES = {"3", "3/A", "4", "4/A", "5", "5/A"}

# An accepted filing never changes (a correction is filed as its own accession), so a
# parsed filing is cached indefinitely. Submissions history does change, so it expires.
SUBMISSIONS_TTL = 15 * 60
# Proxy statements are filed annually, so yesterday's scan is still the right answer.
PROXY_TTL = 24 * 60 * 60
# Typing re-issues near-identical queries; the underlying lookup file only changes daily.
SEARCH_TTL = 60 * 60
# A completed profile is safe to reuse briefly. Individual parsed filings remain cached
# forever below; this cache also avoids re-summarising them and re-running proxy lookups
# when a user revisits a profile or several clients request the same person together.
PROFILE_TTL = 15 * 60


def user_agent() -> str:
    return os.getenv("SEC_USER_AGENT", "Information Check System contact@example.com")


class SecRateLimiter:
    """Simple shared limiter kept below the SEC's 10 requests/sec ceiling."""

    def __init__(self, rate: float = 7.5):
        self.interval = 1.0 / rate
        self.lock = asyncio.Lock()
        self.last = 0.0

    async def wait(self):
        async with self.lock:
            now = time.monotonic()
            sleep_for = self.interval - (now - self.last)
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            self.last = time.monotonic()


limiter = SecRateLimiter()

_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def get_client() -> httpx.AsyncClient:
    """A shared, connection-pooling client.

    A fresh AsyncClient per request pays a new TCP+TLS handshake every time; with the
    profile builder issuing hundreds of sequential SEC requests, that handshake cost
    dwarfed the actual transfer time. Reusing one client lets httpx keep-alive connections
    to www.sec.gov / data.sec.gov across the whole run.
    """
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                _client = httpx.AsyncClient(timeout=30, follow_redirects=True)
    return _client


async def sec_get(url: str, *, as_json: bool = False) -> Any:
    await limiter.wait()
    headers = {
        "User-Agent": user_agent(),
        "Accept-Encoding": "gzip, deflate",
        "Host": httpx.URL(url).host,
    }
    client = await get_client()
    response = await client.get(url, headers=headers)
    response.raise_for_status()
    return response.json() if as_json else response.text


def normalize_name(name: str) -> str:
    s = name.upper().strip()
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    suffixes = {"JR", "SR", "II", "III", "IV", "MD", "PHD"}
    parts = [p for p in s.split() if p not in suffixes]
    return " ".join(parts)


def name_variants(name: str) -> list[str]:
    n = normalize_name(name)
    parts = n.split()
    variants = {n}
    if len(parts) >= 2:
        variants.add(" ".join(reversed(parts)))
        variants.add(f"{parts[-1]} {' '.join(parts[:-1])}")
        variants.add(f"{' '.join(parts[:-1])} {parts[-1]}")
        # EDGAR files people as "COOK TIMOTHY D"; everywhere else they are written
        # "TIMOTHY D COOK". Without this rotation any name carrying a middle initial
        # failed to match its own proxy statement.
        variants.add(" ".join(parts[1:] + parts[:1]))
    return [v for v in variants if v]


async def ensure_cik_lookup() -> Path:
    max_age = int(os.getenv("CIK_CACHE_HOURS", "24")) * 3600
    if CIK_CACHE.exists() and time.time() - CIK_CACHE.stat().st_mtime < max_age:
        return CIK_CACHE
    text = await sec_get(CIK_LOOKUP_URL)
    CIK_CACHE.write_text(text, encoding="latin-1")
    return CIK_CACHE


def parse_cik_line(line: str) -> tuple[str, str] | None:
    # SEC format: NAME:CIK: (names can contain punctuation, so split from the right)
    line = line.strip()
    if not line or ":" not in line:
        return None
    match = re.match(r"^(.*):(\d+):$", line)
    if not match:
        return None
    return match.group(1).strip(), match.group(2).zfill(10)


def _name_match_score(query: str, name: str) -> float:
    """Score both ordinary and SEC legal-name orderings."""
    qnorm = normalize_name(query)
    qparts = qnorm.split()
    norm = normalize_name(name)
    nparts = norm.split()
    if not qparts or not nparts:
        return 0.0
    score = max((fuzz.WRatio(variant, norm) for variant in name_variants(query)), default=0.0)
    overlap = sum(any(part.startswith(query_part) for part in nparts) for query_part in set(qparts))
    score += min(12, overlap * 6)

    # A public name can differ from EDGAR's legal name, e.g. Jensen Huang is filed
    # as HUANG JEN HSUN. The surname anchors the match while the given names are
    # allowed to be a prefix or a close legal-name variant.
    public_legal_match = False
    if len(qparts) >= 2 and len(nparts) >= 2 and qparts[-1] == nparts[0]:
        public_first = "".join(qparts[:-1])
        legal_first = "".join(nparts[1:])
        given_similarity = fuzz.ratio(public_first, legal_first)
        prefix_match = (
            min(len(public_first), len(legal_first)) >= 3
            and (public_first.startswith(legal_first) or legal_first.startswith(public_first))
        )
        if prefix_match or given_similarity >= 74:
            public_legal_match = True
            score += 25 + (given_similarity - 62) * 0.25
    if overlap < len(set(qparts)) and not public_legal_match:
        score = min(score, 86)
    return min(score, 100)


def scan_cik_lookup(path: Path, query: str, limit: int) -> list[dict[str, Any]]:
    scored: list[tuple[float, str, str]] = []
    q_tokens = set(normalize_name(query).split())
    if not q_tokens:
        return []

    # The fallback lookup has over a million lines. Iterating through all of them in
    # Python took several seconds on a small production instance. mmap.find performs
    # the broad token scan in native code, after which Python only scores matching
    # lines. This retains the complete SEC lookup without loading a huge object graph.
    with path.open("rb") as file:
        with mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as data:
            line_starts: set[int] = set()
            for token in q_tokens:
                needle = token.encode("ascii")
                position = 0
                while True:
                    found = data.find(needle, position)
                    if found < 0:
                        break
                    line_starts.add(data.rfind(b"\n", 0, found) + 1)
                    position = found + len(needle)

            for start in line_starts:
                end = data.find(b"\n", start)
                if end < 0:
                    end = len(data)
                line = data[start:end].decode("latin-1", errors="ignore")
                parsed = parse_cik_line(line)
                if not parsed:
                    continue
                name, cik = parsed
                norm = normalize_name(name)
                if not norm:
                    continue
                n_tokens = set(norm.split())
                overlap = sum(any(part.startswith(token) for part in n_tokens) for token in q_tokens)
                if overlap == 0:
                    continue
                score = _name_match_score(query, name)
                if score >= 55:
                    scored.append((score, name, cik))

    scored.sort(reverse=True, key=lambda x: (x[0], x[1]))
    seen = set()
    out = []
    for score, name, cik in scored:
        key = (name, cik)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "cik": cik, "score": round(min(score, 100), 1)})
        if len(out) >= limit:
            break
    return out


def rank_bulk_candidates(query: str, candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Rank recently active owners, including common public/legal-name differences."""
    ranked: list[tuple[float, str, str]] = []
    for candidate in candidates:
        name = candidate.get("name") or ""
        cik = candidate.get("cik") or ""
        score = _name_match_score(query, name)
        if score >= 70:
            ranked.append((score, name, cik))
    ranked.sort(reverse=True, key=lambda row: (row[0], row[1]))
    return [
        {"name": name, "cik": str(cik).zfill(10), "score": round(score, 1), "active": True}
        for score, name, cik in ranked[:limit]
    ]


async def search_cik(query: str, limit: int = 15) -> list[dict[str, Any]]:
    path = await ensure_cik_lookup()
    normalized = normalize_name(query)
    if not normalized:
        return []

    if BULK_DB.exists():
        candidates = await asyncio.to_thread(
            ownership_store.search_owner_candidates, normalized.split(), 1500
        )
        active = rank_bulk_candidates(query, candidates, limit)
        if active and active[0]["score"] >= 92:
            return active

    async def run() -> list[dict[str, Any]]:
        # Reading 38MB and scoring it is blocking CPU work; keeping it off the event
        # loop means one person's search doesn't stall everyone else's requests.
        return await asyncio.to_thread(scan_cik_lookup, path, query, limit)

    return await cache.cached(f"search:{normalized}:{limit}", run, ttl=SEARCH_TTL)


def rows_from_columnar(recent: dict[str, list]) -> list[dict[str, Any]]:
    if not recent:
        return []
    keys = list(recent.keys())
    length = max((len(recent.get(k, [])) for k in keys), default=0)
    rows = []
    for i in range(length):
        row = {}
        for k in keys:
            arr = recent.get(k, [])
            row[k] = arr[i] if i < len(arr) else None
        rows.append(row)
    return rows


async def fetch_submissions(cik: str) -> dict[str, Any]:
    base = await sec_get(f"{DATA_ROOT}/submissions/CIK{cik}.json", as_json=True)
    filings = rows_from_columnar(base.get("filings", {}).get("recent", {}))

    # SEC provides older history in additional JSON files when the filer has many filings.
    extra_files = base.get("filings", {}).get("files", []) or []
    for item in extra_files:
        name = item.get("name")
        if not name:
            continue
        try:
            extra = await sec_get(f"{DATA_ROOT}/submissions/{name}", as_json=True)
            filings.extend(rows_from_columnar(extra))
        except Exception:
            # A missing archival shard should not make the whole profile fail.
            continue

    filings.sort(key=lambda x: x.get("filingDate") or "", reverse=True)
    base["_all_filings"] = filings
    return base


async def get_submissions(cik: str) -> dict[str, Any]:
    """Submissions history for a CIK, cached briefly.

    A profile build asks for the same issuer's history more than once (proxy text and
    portrait scans), and each miss costs one request per archival shard. The TTL is short
    because this is the one SEC document here that legitimately changes when a new filing
    lands.
    """
    cik = str(cik).zfill(10)
    return await cache.cached(
        f"submissions:{cik}",
        lambda: fetch_submissions(cik),
        ttl=SUBMISSIONS_TTL,
    )


def filing_archive_base(cik: str, accession: str) -> str:
    # The accession prefix may identify a filing agent, not the EDGAR registrant.
    cik_plain = str(int(cik))
    accession_plain = accession.replace("-", "")
    return f"{SEC_ROOT}/Archives/edgar/data/{cik_plain}/{accession_plain}/"


async def filing_index(cik: str, accession: str) -> dict[str, Any]:
    return await sec_get(urljoin(filing_archive_base(cik, accession), "index.json"), as_json=True)


def choose_ownership_xml(index_data: dict[str, Any]) -> str | None:
    items = index_data.get("directory", {}).get("item", []) or []
    # Ownership XML commonly has .xml and the form document type/description.
    xmls = [i for i in items if (i.get("name") or "").lower().endswith(".xml")]
    preferred = [i for i in xmls if "form" in (i.get("name") or "").lower() or "ownership" in (i.get("name") or "").lower()]
    pick = (preferred or xmls)
    return pick[0].get("name") if pick else None


def text(node, path: str, default: str | None = None):
    current = node
    for tag in path.split("/"):
        current = current.find(tag) if current else None
    return current.get_text(strip=True) if current and current.get_text(strip=True) else default


def parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.replace(",", ""))
    except Exception:
        return None


def parse_ownership_xml(xml: str, filing_meta: dict[str, Any]) -> dict[str, Any]:
    soup = BeautifulSoup(xml, "xml")
    issuer = soup.find("issuer")
    owner = soup.find("reportingOwner")
    rel = owner.find("reportingOwnerRelationship") if owner else None
    owner_id = owner.find("reportingOwnerId") if owner else None
    owner_address = owner.find("reportingOwnerAddress") if owner else None

    out: dict[str, Any] = {
        "accession": filing_meta.get("accessionNumber"),
        "form": filing_meta.get("form"),
        "filing_date": filing_meta.get("filingDate"),
        "report_date": filing_meta.get("reportDate"),
        "issuer": {
            "cik": text(issuer, "issuerCik"),
            "name": text(issuer, "issuerName"),
            "ticker": text(issuer, "issuerTradingSymbol") or text(issuer, "issuerForeignTradingSymbol"),
        },
        "owner": {
            "cik": text(owner_id, "rptOwnerCik"),
            "name": text(owner_id, "rptOwnerName"),
            "is_director": text(rel, "isDirector") == "1",
            "is_officer": text(rel, "isOfficer") == "1",
            "is_ten_percent_owner": text(rel, "isTenPercentOwner") == "1",
            "is_other": text(rel, "isOther") == "1",
            "title": text(rel, "officerTitle"),
            "other_text": text(rel, "otherText"),
            "location": {
                "city": text(owner_address, "rptOwnerCity"),
                "state": text(owner_address, "rptOwnerState") or text(owner_address, "rptOwnerNonUSStateTerritory"),
                "country": text(owner_address, "rptOwnerCountry"),
            },
        },
        "remarks": text(soup, "remarks"),
        "non_derivative": [],
        "derivative": [],
    }

    for tx in soup.find_all("nonDerivativeTransaction"):
        shares = parse_float(text(tx, "transactionAmounts/transactionShares/value"))
        price = parse_float(text(tx, "transactionAmounts/transactionPricePerShare/value"))
        acquired = text(tx, "transactionAmounts/transactionAcquiredDisposedCode/value")
        after = parse_float(text(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value"))
        code = text(tx, "transactionCoding/transactionCode")
        out["non_derivative"].append({
            "security": text(tx, "securityTitle/value"),
            "date": text(tx, "transactionDate/value"),
            "code": code,
            "acquired_disposed": acquired,
            "shares": shares,
            "price": price,
            "value": round(shares * price, 2) if shares is not None and price is not None else None,
            "shares_after": after,
            "ownership": text(tx, "ownershipNature/directOrIndirectOwnership/value"),
            "nature": text(tx, "ownershipNature/natureOfOwnership/value"),
        })

    for holding in soup.find_all("nonDerivativeHolding"):
        out["non_derivative"].append({
            "security": text(holding, "securityTitle/value"),
            "date": filing_meta.get("reportDate"),
            "code": "HOLDING",
            "acquired_disposed": None,
            "shares": None,
            "price": None,
            "value": None,
            "shares_after": parse_float(text(holding, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")),
            "ownership": text(holding, "ownershipNature/directOrIndirectOwnership/value"),
            "nature": text(holding, "ownershipNature/natureOfOwnership/value"),
        })

    for tx in soup.find_all("derivativeTransaction"):
        shares = parse_float(text(tx, "transactionAmounts/transactionShares/value"))
        price = parse_float(text(tx, "transactionAmounts/transactionPricePerShare/value"))
        out["derivative"].append({
            "security": text(tx, "securityTitle/value"),
            "date": text(tx, "transactionDate/value"),
            "code": text(tx, "transactionCoding/transactionCode"),
            "acquired_disposed": text(tx, "transactionAmounts/transactionAcquiredDisposedCode/value"),
            "shares": shares,
            "price": price,
            "value": round(shares * price, 2) if shares is not None and price is not None else None,
            "underlying_security": text(tx, "underlyingSecurity/underlyingSecurityTitle/value"),
            "underlying_shares": parse_float(text(tx, "underlyingSecurity/underlyingSecurityShares/value")),
            "exercise_price": parse_float(text(tx, "conversionOrExercisePrice/value")),
            "expiration_date": text(tx, "expirationDate/value"),
            "shares_after": parse_float(text(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")),
        })

    return out


def raw_ownership_document(primary: str | None) -> str | None:
    """Turn a submissions-JSON primaryDocument into the raw ownership XML name.

    EDGAR lists ownership forms as `xslF345X0N/form4.xml`, which is the XSLT-rendered
    HTML *view* of the filing (no parseable XML in it). The machine-readable XML is the
    same file name at the accession root. Deriving it here means one request per filing
    instead of an index.json round trip plus a wasted fetch of the rendered view.
    """
    if not primary:
        return None
    head, slash, tail = primary.partition("/")
    if slash and head.lower().startswith("xsl"):
        return tail
    return primary


async def parse_ownership_document(base: str, name: str, filing: dict[str, Any]) -> dict[str, Any] | None:
    try:
        content = await sec_get(urljoin(base, name))
    except Exception:
        return None
    if "ownershipDocument" not in content and "reportingOwner" not in content:
        return None
    parsed = parse_ownership_xml(content, filing)
    parsed["sec_url"] = urljoin(base, name)
    return parsed


async def fetch_ownership_filing(cik: str, filing: dict[str, Any]) -> dict[str, Any] | None:
    accession = filing.get("accessionNumber")
    base = filing_archive_base(cik, accession)

    derived = raw_ownership_document(filing.get("primaryDocument"))
    if derived:
        parsed = await parse_ownership_document(base, derived, filing)
        if parsed:
            return parsed

    # Only filings that don't follow the standard layout pay for the index lookup.
    try:
        xml_name = choose_ownership_xml(await filing_index(cik, accession))
    except Exception:
        return None
    if not xml_name or xml_name == derived:
        return None
    return await parse_ownership_document(base, xml_name, filing)


async def get_ownership_filing(cik: str, filing: dict[str, Any]) -> dict[str, Any] | None:
    accession = filing.get("accessionNumber")
    if not accession:
        return None
    return await cache.cached(
        f"ownership:{accession}",
        lambda: fetch_ownership_filing(cik, filing),
    )


def summarize_filings(details: list[dict[str, Any]]) -> dict[str, Any]:
    companies: dict[str, dict[str, Any]] = {}
    transactions: list[dict[str, Any]] = []
    roles: dict[str, dict[str, Any]] = {}
    latest_location = None

    for f in details:
        issuer = f.get("issuer") or {}
        key = issuer.get("cik") or issuer.get("name") or "unknown"
        if key not in companies:
            companies[key] = {
                "cik": issuer.get("cik"),
                "name": issuer.get("name"),
                "ticker": issuer.get("ticker"),
                "first_seen": f.get("filing_date"),
                "last_seen": f.get("filing_date"),
                "filings": 0,
            }
        c = companies[key]
        c["filings"] += 1
        d = f.get("filing_date")
        if d:
            c["first_seen"] = min(c["first_seen"] or d, d)
            c["last_seen"] = max(c["last_seen"] or d, d)

        owner = f.get("owner") or {}
        role_parts = []
        if owner.get("title"):
            role_parts.append(owner["title"])
        if owner.get("is_director"):
            role_parts.append("Director")
        if owner.get("is_ten_percent_owner"):
            role_parts.append("10% Owner")
        if owner.get("is_other") and owner.get("other_text"):
            role_parts.append(owner["other_text"])
        role = " · ".join(dict.fromkeys(role_parts)) or ("Officer" if owner.get("is_officer") else "Insider")
        role_key = f"{key}:{role}"
        rr = roles.setdefault(role_key, {
            "company": issuer.get("name"), "ticker": issuer.get("ticker"), "role": role,
            "first_seen": f.get("filing_date"), "last_seen": f.get("filing_date")
        })
        if d:
            rr["first_seen"] = min(rr["first_seen"] or d, d)
            rr["last_seen"] = max(rr["last_seen"] or d, d)

        loc = owner.get("location") or {}
        if any(loc.values()) and not latest_location:
            latest_location = loc

        for bucket, derivative in ((f.get("non_derivative", []), False), (f.get("derivative", []), True)):
            for tx in bucket:
                t = dict(tx)
                t.update({
                    "company": issuer.get("name"),
                    "ticker": issuer.get("ticker"),
                    "issuer_cik": issuer.get("cik"),
                    "form": f.get("form"),
                    "filing_date": f.get("filing_date"),
                    "accession": f.get("accession"),
                    "sec_url": f.get("sec_url"),
                    "derivative": derivative,
                })
                transactions.append(t)

    transactions.sort(key=lambda x: (x.get("date") or x.get("filing_date") or ""), reverse=True)
    companies_list = sorted(companies.values(), key=lambda x: x.get("last_seen") or "", reverse=True)
    roles_list = sorted(roles.values(), key=lambda x: x.get("last_seen") or "", reverse=True)

    buys = [t for t in transactions if t.get("acquired_disposed") == "A" and t.get("value") is not None]
    sells = [t for t in transactions if t.get("acquired_disposed") == "D" and t.get("value") is not None]
    purchased_value = sum(t["value"] for t in buys)
    disposed_value = sum(t["value"] for t in sells)

    return {
        "companies": companies_list,
        "roles": roles_list,
        "transactions": transactions,
        "latest_location": latest_location,
        "stats": {
            "filings_parsed": len(details),
            "companies": len(companies_list),
            "transactions": len([t for t in transactions if t.get("code") != "HOLDING"]),
            "purchased_value": round(purchased_value, 2),
            "disposed_value": round(disposed_value, 2),
        },
    }




def extract_proxy_facts(soup: BeautifulSoup, person_name: str) -> list[str]:
    """Text blocks from a proxy statement that actually name this person.

    This does not pretend to fully understand arbitrary proxy layouts. It collects only
    text blocks/rows in which the person's name actually appears.
    """
    variants = name_variants(person_name)
    target_last = normalize_name(person_name).split()[-1:]
    blocks: list[str] = []
    seen: set[str] = set()

    # Table rows often contain the cleanest director/executive facts.
    for node in soup.find_all(["tr", "p", "div", "td"]):
        raw = " ".join(node.stripped_strings)
        if not raw or len(raw) < 20 or len(raw) > 1600:
            continue
        norm = normalize_name(raw)
        match = any(v in norm for v in variants)
        if not match and target_last:
            # Require a strong fuzzy match when layout separates first/last names.
            match = fuzz.partial_ratio(normalize_name(person_name), norm) >= 91
        if not match:
            continue
        compact = re.sub(r"\s+", " ", raw).strip()
        key = compact[:350].upper()
        if key in seen:
            continue
        seen.add(key)
        blocks.append(compact[:1400])
        if len(blocks) >= 5:
            break
    return blocks


def extract_proxy_portrait(soup: BeautifulSoup, base: str, source_url: str, person_name: str) -> dict[str, Any] | None:
    """Best-effort SEC-only portrait detection inside one proxy document.

    We only accept images that are explicitly associated with the person's name in alt/title
    text or very close HTML context. Otherwise we return None rather than guess.
    """
    target_tokens = set(normalize_name(person_name).split())
    for img in soup.find_all("img"):
        context = " ".join(filter(None, [img.get("alt"), img.get("title")]))
        parent_text = img.parent.get_text(" ", strip=True)[:500] if img.parent else ""
        context = normalize_name(context + " " + parent_text)
        tokens = set(context.split())
        if target_tokens and len(target_tokens & tokens) >= max(2, len(target_tokens) - 1):
            src = img.get("src")
            if src and not src.lower().endswith((".gif",)):
                return {
                    "url": urljoin(base, src),
                    "source": source_url,
                    "confidence": "SEC filing context match",
                }
    return None


async def fetch_proxy_scan(person_name: str, issuer_cik: str, include_photo: bool) -> dict[str, Any]:
    """Read an issuer's recent proxy statements once, for both facts and portrait.

    These documents run to megabytes, so they are fetched and parsed a single time and
    both extractors run over the same soup rather than each pulling its own copy.
    """
    result: dict[str, Any] = {"enrichment": None, "photo": None}
    try:
        issuer = await get_submissions(issuer_cik)
    except Exception:
        return result
    proxies = [f for f in issuer.get("_all_filings", []) if f.get("form") in {"DEF 14A", "DEFA14A"}][:4]

    for position, filing in enumerate(proxies):
        accession = filing.get("accessionNumber")
        primary = filing.get("primaryDocument")
        if not accession or not primary:
            continue
        base = filing_archive_base(issuer_cik, accession)
        source_url = urljoin(base, primary)
        try:
            html = await sec_get(source_url)
        except Exception:
            continue
        soup = BeautifulSoup(html, "lxml")

        if result["enrichment"] is None:
            blocks = extract_proxy_facts(soup, person_name)
            if blocks:
                result["enrichment"] = {
                    "filing_date": filing.get("filingDate"),
                    "issuer": issuer.get("name"),
                    "source": source_url,
                    "facts": blocks,
                }
        # Portraits only ever came from the three most recent proxies.
        if include_photo and result["photo"] is None and position < 3:
            result["photo"] = extract_proxy_portrait(soup, base, source_url, person_name)

        if result["enrichment"] and (result["photo"] or not include_photo):
            break
    return result


async def scan_proxies(person_name: str, issuer_cik: str, include_photo: bool = True) -> dict[str, Any]:
    """Cached proxy scan, including the "found nothing" answer.

    A miss costs several multi-megabyte downloads, and for most filers the honest answer
    is that the proxies say nothing about them; remembering that is what keeps a second
    view of a profile cheap.
    """
    return await cache.cached(
        f"proxy:{normalize_name(person_name)}:{issuer_cik}:{int(include_photo)}",
        lambda: fetch_proxy_scan(person_name, issuer_cik, include_photo),
        ttl=PROXY_TTL,
    )


async def build_profile_overview(cik: str) -> dict[str, Any]:
    """Return the useful first paint from one submissions request.

    Ownership XML and proxy statements are intentionally not touched here. This makes it
    possible for the UI to identify the person and link to EDGAR while the expensive
    transaction history is assembled in the background.
    """
    cik = str(cik).zfill(10)
    if BULK_DB.exists():
        header = await asyncio.to_thread(ownership_store.profile_header, cik)
        if header:
            transactions = await asyncio.to_thread(ownership_store.top_transactions, cik, 20)
            companies = header["companies"]
            dates = [
                company.get(boundary)
                for company in companies
                for boundary in ("first_seen", "last_seen")
                if company.get(boundary)
            ]
            return {
                "person": {**header["person"], "aliases": [], "photo": None},
                "summary": {
                    "roles": header["roles"],
                    "companies": companies,
                    "transactions": transactions,
                    "stats": header["stats"],
                },
                "coverage": {
                    "ownership_filings_found": header["stats"]["filings_parsed"],
                    "ownership_filings_parsed": header["stats"]["filings_parsed"],
                    "loaded_limit": len(transactions),
                    "oldest_loaded": min(dates, default=None),
                    "newest_loaded": max(dates, default=None),
                    "mode": "quarterly_bulk",
                    "first_quarter": header["dataset"]["first_quarter"],
                    "last_quarter": header["dataset"]["last_quarter"],
                    "quarters": header["dataset"]["quarters"],
                },
                "source": "U.S. SEC EDGAR quarterly Insider Transactions Data Sets",
                "status": "ready",
            }
    submissions = await get_submissions(cik)
    all_filings = submissions.get("_all_filings", [])
    ownership_filings = [f for f in all_filings if f.get("form") in FORM_TYPES]
    addresses = submissions.get("addresses") or {}
    address = addresses.get("business") or addresses.get("mailing") or {}
    return {
        "person": {
            "name": submissions.get("name"),
            "cik": cik,
            "aliases": submissions.get("formerNames", []) or [],
            "sic": submissions.get("sic"),
            "sic_description": submissions.get("sicDescription"),
            "state_of_incorporation": submissions.get("stateOfIncorporation"),
            "fiscal_year_end": submissions.get("fiscalYearEnd"),
            "latest_location": {
                "city": address.get("city"),
                "state": address.get("stateOrCountryDescription") or address.get("stateOrCountry"),
                "country": address.get("stateOrCountry") if address.get("stateOrCountry") not in {"", "US"} else None,
            },
            "photo": None,
        },
        "coverage": {
            "ownership_filings_found": len(ownership_filings),
            "newest_available": max((f.get("filingDate") for f in ownership_filings if f.get("filingDate")), default=None),
        },
        "source": "U.S. SEC EDGAR",
        "status": "overview",
    }


async def _build_profile(cik: str, max_filings: int = 120, include_photo: bool = True) -> dict[str, Any]:
    cik = str(cik).zfill(10)
    submissions = await get_submissions(cik)
    all_filings = submissions.get("_all_filings", [])
    ownership_filings = [f for f in all_filings if f.get("form") in FORM_TYPES]

    # Most useful filings first. Caller can raise max_filings if desired.
    ownership_filings = ownership_filings[: max(1, min(max_filings, 500))]

    semaphore = asyncio.Semaphore(5)

    async def one(f):
        async with semaphore:
            return await get_ownership_filing(cik, f)

    results = await asyncio.gather(*(one(f) for f in ownership_filings))
    details = [r for r in results if r]
    summary = summarize_filings(details)

    person_name = submissions.get("name") or (details[0].get("owner", {}).get("name") if details else None)
    aliases = submissions.get("formerNames", []) or []

    photo = None
    proxy_enrichment = None
    if person_name:
        for company in summary["companies"][:3]:
            issuer_cik = company.get("cik")
            if not issuer_cik:
                continue
            scan = await scan_proxies(person_name, issuer_cik, include_photo)
            proxy_enrichment = proxy_enrichment or scan.get("enrichment")
            photo = photo or scan.get("photo")
            if proxy_enrichment and (photo or not include_photo):
                break

    latest_filing = ownership_filings[0] if ownership_filings else None
    return {
        "person": {
            "name": person_name,
            "cik": cik,
            "aliases": aliases,
            "sic": submissions.get("sic"),
            "sic_description": submissions.get("sicDescription"),
            "state_of_incorporation": submissions.get("stateOfIncorporation"),
            "fiscal_year_end": submissions.get("fiscalYearEnd"),
            "latest_location": summary.get("latest_location"),
            "photo": photo,
        },
        "summary": summary,
        "proxy_enrichment": proxy_enrichment,
        "filings": [
            {
                "form": f.get("form"),
                "filing_date": f.get("filingDate"),
                "report_date": f.get("reportDate"),
                "accession": f.get("accessionNumber"),
                "primary_document": f.get("primaryDocument"),
                "sec_url": urljoin(filing_archive_base(cik, f.get("accessionNumber")), f.get("primaryDocument") or "") if f.get("accessionNumber") else None,
            }
            for f in ownership_filings
        ],
        "coverage": {
            "ownership_filings_found": len([f for f in all_filings if f.get("form") in FORM_TYPES]),
            "ownership_filings_parsed": len(details),
            "loaded_limit": len(ownership_filings),
            "oldest_loaded": min((f.get("filingDate") for f in ownership_filings if f.get("filingDate")), default=None),
            "newest_loaded": max((f.get("filingDate") for f in ownership_filings if f.get("filingDate")), default=None),
        },
        "source": "U.S. SEC EDGAR",
    }


async def build_profile(cik: str, max_filings: int = 120, include_photo: bool = True) -> dict[str, Any]:
    normalized_cik = str(cik).zfill(10)
    limit = max(1, min(max_filings, 500))
    return await cache.cached(
        f"profile:v1:{normalized_cik}:{limit}:{int(include_photo)}",
        lambda: _build_profile(normalized_cik, max_filings=limit, include_photo=include_photo),
        ttl=PROFILE_TTL,
    )
