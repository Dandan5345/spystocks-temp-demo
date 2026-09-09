from __future__ import annotations

from . import cache, snapshots

import asyncio
import json
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from rapidfuzz import fuzz

from .legislator_directory import legislator_directory

CONGRESS_ROOT = "https://api.congress.gov/v3"
CACHE_DIR = Path(__file__).resolve().parents[1] / "data"
MEMBER_INDEX_CACHE = CACHE_DIR / "congress-member-index.json"
BIOGUIDE_RE = re.compile(r"^[A-Z]\d{6}$", re.IGNORECASE)
PARTY_NAMES = {
    "D": "Democratic",
    "R": "Republican",
    "I": "Independent",
    "ID": "Independent Democrat",
    "L": "Libertarian",
}
BILL_TYPE_PATHS = {
    "HR": "house-bill",
    "S": "senate-bill",
    "HRES": "house-resolution",
    "SRES": "senate-resolution",
    "HJRES": "house-joint-resolution",
    "SJRES": "senate-joint-resolution",
    "HCONRES": "house-concurrent-resolution",
    "SCONRES": "senate-concurrent-resolution",
}


class CongressError(Exception):
    def __init__(self, message: str, *, status_code: int = 502, retry_after: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class MissingCongressApiKey(CongressError):
    def __init__(self):
        super().__init__("Congress.gov access is not configured.", status_code=503)


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(char for char in value if not unicodedata.combining(char)).lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def split_member_name(item: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None, str]:
    first = item.get("firstName")
    middle = item.get("middleName")
    last = item.get("lastName")
    suffix = item.get("suffixName") or item.get("suffix")
    direct = item.get("directOrderName")
    raw = direct or item.get("name") or item.get("invertedOrderName") or ""
    if (not first or not last) and "," in raw:
        last_part, given_part = (part.strip() for part in raw.split(",", 1))
        given = given_part.split()
        first = first or (given[0] if given else None)
        middle = middle or (" ".join(given[1:]) or None)
        last = last or last_part
    display = direct or " ".join(part for part in (first, middle, last, suffix) if part) or raw
    return first, middle, last, suffix, display.strip()


def term_items(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        items = raw.get("item", [])
        if isinstance(items, dict):
            return [items]
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def chamber_label(value: str | None) -> str | None:
    if not value:
        return None
    return "House" if "house" in value.lower() else "Senate" if "senate" in value.lower() else value


def infer_current_from_terms(terms: list[dict[str, Any]]) -> bool:
    if not terms:
        return False
    latest = max(terms, key=lambda term: int(term.get("startYear") or 0))
    return not bool(latest.get("endYear"))


def normalize_index_member(item: dict[str, Any]) -> dict[str, Any]:
    first, middle, last, suffix, display = split_member_name(item)
    terms = term_items(item.get("terms"))
    latest = max(terms, key=lambda term: int(term.get("startYear") or 0), default={})
    current = item.get("currentMember")
    if current is None:
        current = infer_current_from_terms(terms)
    return {
        "bioguideId": str(item.get("bioguideId") or "").upper(),
        "name": display,
        "firstName": first,
        "middleName": middle,
        "lastName": last,
        "suffix": suffix,
        "party": item.get("partyName") or PARTY_NAMES.get(item.get("party"), item.get("party")),
        "state": item.get("state") or latest.get("stateName"),
        "district": item.get("district", latest.get("district")),
        "chamber": chamber_label(latest.get("chamber")),
        "currentMember": bool(current),
        "termStart": latest.get("startYear"),
        "termEnd": latest.get("endYear"),
        "imageUrl": (item.get("depiction") or {}).get("imageUrl"),
        "imageAttribution": (item.get("depiction") or {}).get("attribution"),
        "updateDate": item.get("updateDate"),
        "_search": normalize_name(display),
    }


def public_search_member(member: dict[str, Any]) -> dict[str, Any]:
    public = {key: value for key, value in member.items() if not key.startswith("_")}
    public.update({"id": member.get("bioguideId"), "sourceType": "congress", "profileType": "legislator"})
    return public


def search_members(index: list[dict[str, Any]], query: str, limit: int = 12) -> list[dict[str, Any]]:
    q = normalize_name(query)
    if len(q) < 2:
        return []
    q_tokens = q.split()
    reversed_q = " ".join(reversed(q_tokens)) if len(q_tokens) > 1 else q
    ranked: list[tuple[float, str, dict[str, Any]]] = []
    for member in index:
        target = member.get("_search") or normalize_name(member.get("name", ""))
        target_tokens = target.split()
        if not target:
            continue
        exact = q == target or reversed_q == target
        token_prefix = all(any(target_token.startswith(query_token) for target_token in target_tokens) for query_token in q_tokens)
        prefix = target.startswith(q) or target.startswith(reversed_q) or token_prefix
        score = max(
            fuzz.ratio(q, target),
            fuzz.ratio(reversed_q, target),
            fuzz.token_sort_ratio(q, target),
        )
        if exact:
            rank = 300
        elif prefix:
            rank = 200 + score
        else:
            rank = score
        # A shared first name alone is not a useful match for a multi-token query.
        # Keep fuzzy typo support, but reject the weak results that make person
        # disambiguation actively misleading.
        threshold = 78 if len(q) >= 5 else 82
        if exact or prefix or score >= threshold:
            ranked.append((rank, member.get("name", ""), member))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return [public_search_member(row[2]) for row in ranked[:limit]]


def normalize_term(term: dict[str, Any], current_member: bool, is_latest: bool) -> dict[str, Any]:
    return {
        "congress": term.get("congress"),
        "chamber": chamber_label(term.get("chamber")),
        "role": "U.S. Senator" if "senate" in str(term.get("chamber", "")).lower() else "U.S. Representative",
        "memberType": term.get("memberType"),
        "state": term.get("stateName") or term.get("state"),
        "stateCode": term.get("stateCode"),
        "district": term.get("district"),
        "startYear": term.get("startYear"),
        "endYear": None if current_member and is_latest else term.get("endYear"),
    }


def normalize_member_detail(payload: dict[str, Any]) -> dict[str, Any]:
    item = payload.get("member") or payload
    first, middle, last, suffix, display = split_member_name(item)
    raw_terms = term_items(item.get("terms"))
    latest_raw = max(raw_terms, key=lambda term: int(term.get("startYear") or 0), default={})
    current = bool(item.get("currentMember"))
    terms = [normalize_term(term, current, term is latest_raw) for term in raw_terms]
    party_history = []
    for party in item.get("partyHistory") or []:
        party_history.append({
            "party": party.get("partyName") or PARTY_NAMES.get(party.get("partyAbbreviation"), party.get("partyAbbreviation")),
            "abbreviation": party.get("partyAbbreviation"),
            "startYear": party.get("startYear") or party.get("startDate"),
            "endYear": party.get("endYear") or party.get("endDate"),
        })
    party_history.sort(key=lambda row: int(str(row.get("startYear") or "0")[:4] or 0))
    party = party_history[-1]["party"] if party_history else item.get("partyName")
    for term in terms:
        year = int(term.get("startYear") or 0)
        matching = [row for row in party_history if int(str(row.get("startYear") or "0")[:4] or 0) <= year and (not row.get("endYear") or year <= int(str(row["endYear"])[:4]))]
        term["party"] = matching[-1]["party"] if matching else party
    intervals = sorted((int(term["startYear"]), int(term.get("endYear") or time.gmtime().tm_year)) for term in terms if term.get("startYear"))
    merged: list[list[int]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    years_in_congress = sum(max(0, end - start) for start, end in merged) if merged else None
    depiction = item.get("depiction") or {}
    bioguide = str(item.get("bioguideId") or "").upper()
    sponsored_count = (item.get("sponsoredLegislation") or {}).get("count")
    cosponsored_count = (item.get("cosponsoredLegislation") or {}).get("count")
    return {
        "id": bioguide,
        "bioguideId": bioguide,
        "profileType": "legislator",
        "sourceType": "congress",
        "name": display,
        "firstName": first,
        "middleName": middle,
        "lastName": last,
        "suffix": suffix,
        "imageUrl": depiction.get("imageUrl"),
        "imageAttribution": depiction.get("attribution"),
        "birthYear": item.get("birthYear"),
        "currentMember": current,
        "currentChamber": chamber_label(latest_raw.get("chamber")),
        "currentParty": party,
        "currentState": item.get("state") or latest_raw.get("stateName"),
        "currentStateCode": latest_raw.get("stateCode"),
        "currentDistrict": item.get("district", latest_raw.get("district")),
        "officialWebsiteUrl": item.get("officialWebsiteUrl"),
        "partyHistory": party_history,
        "terms": terms,
        "yearsInCongress": years_in_congress,
        "totalTerms": len(terms),
        "sponsoredLegislation": {"total": sponsored_count, "items": []},
        "cosponsoredLegislation": {"total": cosponsored_count, "items": []},
        "source": {
            "name": "Congress.gov",
            "description": "Official U.S. Government Data",
            "updatedAt": item.get("updateDate"),
            "officialUrl": f"https://www.congress.gov/member/{normalize_name(display).replace(' ', '-')}/{bioguide}",
        },
    }


def congress_bill_url(item: dict[str, Any]) -> str | None:
    if not item.get("congress") or not item.get("type") or not item.get("number"):
        return None
    bill_path = BILL_TYPE_PATHS.get(str(item["type"]).upper())
    if not bill_path:
        return None
    return f"https://www.congress.gov/bill/{item['congress']}th-congress/{bill_path}/{item['number']}"


def normalize_legislation(payload: dict[str, Any], kind: Literal["sponsored", "cosponsored"]) -> dict[str, Any]:
    key = "sponsoredLegislation" if kind == "sponsored" else "cosponsoredLegislation"
    items = []
    for item in payload.get(key) or []:
        action = item.get("latestAction") or {}
        items.append({
            "congress": item.get("congress"),
            "type": item.get("type"),
            "number": item.get("number"),
            "label": f"{item.get('type', '')} {item.get('number', '')}".strip(),
            "title": item.get("title"),
            "introducedDate": item.get("introducedDate"),
            "latestAction": {"date": action.get("actionDate"), "text": action.get("text")},
            "policyArea": (item.get("policyArea") or {}).get("name"),
            "officialUrl": congress_bill_url(item),
            "enacted": bool(re.search(r"became (?:public|private) law|signed by president", str(action.get("text") or ""), re.I)),
        })
    pagination = payload.get("pagination") or {}
    return {
        "total": pagination.get("count"),
        "offset": int((payload.get("request") or {}).get("offset") or 0),
        "items": items,
        "hasMore": bool(pagination.get("next")),
    }


class CongressClient:
    def __init__(self, api_key: str | None = None, *, transport: httpx.AsyncBaseTransport | None = None):
        self.api_key = api_key if api_key is not None else os.getenv("CONGRESS_API_KEY")
        self.transport = transport
        self._memory: dict[str, tuple[float, Any]] = {}
        self._index_lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        self._public_client: httpx.AsyncClient | None = None

    def _require_key(self) -> None:
        if not self.api_key:
            raise MissingCongressApiKey()

    def _get_client(self) -> httpx.AsyncClient:
        # Reused across calls so repeated Congress.gov requests (typeahead search,
        # legislation paging, etc.) keep-alive instead of paying a fresh TLS handshake each time.
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0),
                follow_redirects=True,
                headers={"X-Api-Key": self.api_key, "Accept": "application/json"},
                transport=self.transport,
            )
        return self._client

    async def _get(self, path_or_url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_key()
        if path_or_url.startswith("http"):
            parsed = urlparse(path_or_url)
            if parsed.scheme != "https" or parsed.netloc != "api.congress.gov":
                raise CongressError("Congress.gov returned an invalid pagination URL.")
            url = path_or_url
            query = params
        else:
            url = f"{CONGRESS_ROOT}/{path_or_url.lstrip('/')}"
            query = {"format": "json", **(params or {})}
        try:
            client = self._get_client()
            response = await client.get(url, params=query)
            if response.status_code == 404:
                raise CongressError("Congress.gov could not find that member.", status_code=404)
            if response.status_code == 429:
                raise CongressError("Congress.gov is rate limiting requests. Please try again shortly.", status_code=503, retry_after=response.headers.get("Retry-After"))
            if response.status_code in {401, 403}:
                raise CongressError("Congress.gov access is not configured correctly.", status_code=503)
            response.raise_for_status()
            return response.json()
        except CongressError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError):
            raise CongressError("We couldn't reach Congress.gov right now. Please try again.")
        except (httpx.HTTPStatusError, ValueError):
            raise CongressError("Congress.gov returned an unexpected response. Please try again.")

    async def _house_clerk_vote(self, source_url: str, bioguide: str) -> dict[str, Any] | None:
        parsed = urlparse(source_url)
        if parsed.scheme != "https" or parsed.netloc != "clerk.house.gov":
            return None
        if self._public_client is None:
            # Intentionally separate from the Congress.gov client: never send the API
            # key to the Clerk host.
            self._public_client = httpx.AsyncClient(
                timeout=httpx.Timeout(10.0), follow_redirects=True,
                headers={"Accept": "application/xml, text/xml;q=0.9, */*;q=0.8", "User-Agent": "Mozilla/5.0"},
            )
        try:
            response = await self._public_client.get(source_url)
            response.raise_for_status()
            root = ET.fromstring(response.content)
        except (httpx.HTTPError, ET.ParseError):
            return None
        for recorded in root.findall(".//recorded-vote"):
            legislator = recorded.find("legislator")
            if legislator is not None and legislator.attrib.get("name-id") == bioguide:
                return {
                    "vote": recorded.findtext("vote"),
                    "question": root.findtext(".//vote-question"),
                    "result": root.findtext(".//vote-result"),
                    "description": root.findtext(".//vote-desc"),
                }
        return None

    def _cached(self, key: str) -> Any | None:
        cached = self._memory.get(key)
        if cached and cached[0] > time.time():
            return cached[1]
        self._memory.pop(key, None)
        return None

    def _remember(self, key: str, value: Any, ttl_seconds: int) -> Any:
        self._memory[key] = (time.time() + ttl_seconds, value)
        return value

    async def member_index(self, *, force: bool = False) -> list[dict[str, Any]]:
        cached = None if force else self._cached("member-index")
        if cached is not None:
            return cached
        async with self._index_lock:
            cached = None if force else self._cached("member-index")
            if cached is not None:
                return cached
            ttl = int(os.getenv("CONGRESS_INDEX_CACHE_HOURS", "24")) * 3600
            if not force and MEMBER_INDEX_CACHE.exists():
                try:
                    data = json.loads(MEMBER_INDEX_CACHE.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        if time.time() - MEMBER_INDEX_CACHE.stat().st_mtime >= ttl:
                            snapshots.schedule("congress-index", lambda: self.member_index(force=True))
                        for member in data:
                            member["_search"] = normalize_name(member.get("name", ""))
                        return self._remember("member-index", data, ttl)
                except (OSError, ValueError):
                    pass
            members: list[dict[str, Any]] = []
            next_url: str | None = "/member"
            params: dict[str, Any] | None = {"limit": 250, "offset": 0}
            while next_url:
                payload = await self._get(next_url, params=params)
                params = None
                members.extend(normalize_index_member(item) for item in payload.get("members") or [])
                next_url = (payload.get("pagination") or {}).get("next")
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            temp = MEMBER_INDEX_CACHE.with_suffix(".tmp")
            temp.write_text(json.dumps(members, ensure_ascii=False), encoding="utf-8")
            temp.replace(MEMBER_INDEX_CACHE)
            return self._remember("member-index", members, ttl)

    async def search(self, query: str, limit: int = 12) -> list[dict[str, Any]]:
        return search_members(await self.member_index(), query, limit)

    async def overview(self, bioguide_id: str) -> dict[str, Any]:
        bioguide = bioguide_id.upper()
        if not BIOGUIDE_RE.fullmatch(bioguide):
            raise CongressError("Invalid Bioguide ID.", status_code=400)
        hit = await snapshots.read('congress:' + bioguide)
        if hit:
            if hit['fetchedAt'] + 14400 < time.time():
                snapshots.schedule('congress:' + bioguide, lambda: self.profile(bioguide))
            return {**hit['data'], 'status': 'ready', 'fetchedAt': hit['fetchedAt']}
        # A direct profile URL should never wait for the complete member directory.
        # Congress.gov can return one member in a single request; loading every page
        # of /member is reserved for name search and is warmed independently.
        if self.api_key:
            try:
                return {**await self.profile(bioguide), 'status': 'ready'}
            except CongressError as error:
                if error.status_code != 404:
                    raise
        member = next((m for m in await self.member_index() if m['bioguideId'] == bioguide), None)
        if not member:
            raise CongressError('No congressional member found.', status_code=404)
        return {**{k: v for k, v in member.items() if not k.startswith('_')},
                'id': bioguide, 'profileType': 'legislator', 'status': 'overview',
                'currentChamber': member.get('chamber'), 'currentParty': member.get('party'),
                'currentState': member.get('state'), 'currentDistrict': member.get('district'),
                'terms': [], 'partyHistory': [], 'yearsInCongress': None, 'totalTerms': None,
                'sponsoredLegislation': {'total': None}, 'cosponsoredLegislation': {'total': None},
                'source': {'name': 'Congress.gov', 'updatedAt': member.get('updateDate'),
                           'officialUrl': f'https://www.congress.gov/member/{bioguide}'}}

    async def profile(self, bioguide_id: str) -> dict[str, Any]:
        bioguide = bioguide_id.upper()
        if not BIOGUIDE_RE.fullmatch(bioguide):
            raise CongressError("Invalid Bioguide ID.", status_code=400)
        key = f"profile:{bioguide}"
        if (cached := self._cached(key)) is not None:
            return cached
        self._require_key()
        async def fetch():
            return normalize_member_detail(await self._get(f"/member/{bioguide}"))
        data = await fetch() if self.transport else await snapshots.get("congress:" + bioguide, fetch)
        return self._remember(key, data, 4 * 3600)

    async def legislation(self, bioguide_id: str, kind: Literal["sponsored", "cosponsored"], offset: int, limit: int) -> dict[str, Any]:
        bioguide = bioguide_id.upper()
        if not BIOGUIDE_RE.fullmatch(bioguide):
            raise CongressError("Invalid Bioguide ID.", status_code=400)
        key = f"legislation:{bioguide}:{kind}:{offset}:{limit}"
        if (cached := self._cached(key)) is not None:
            return cached
        data = await self._get(f"/member/{bioguide}/{kind}-legislation", params={"offset": offset, "limit": limit})
        return self._remember(key, normalize_legislation(data, kind), 4 * 3600)

    async def intelligence(self, bioguide_id: str) -> dict[str, Any]:
        bioguide = bioguide_id.upper()
        if not BIOGUIDE_RE.fullmatch(bioguide):
            raise CongressError("Invalid Bioguide ID.", status_code=400)
        key = f"intelligence:v2:{bioguide}"
        if (cached := self._cached(key)) is not None:
            return cached
        profile = await self.profile(bioguide)
        directory_task = legislator_directory(bioguide, profile.get("currentMember", False))
        legislation_task = self._all_sponsored_legislation(bioguide)
        directory, sponsored = await asyncio.gather(directory_task, legislation_task, return_exceptions=True)
        directory = {} if isinstance(directory, Exception) else directory
        sponsored = {"total": profile.get("sponsoredLegislation", {}).get("total"), "items": []} if isinstance(sponsored, Exception) else sponsored
        laws = [item for item in sponsored.get("items") or [] if item.get("enacted")]
        years = int(profile.get("yearsInCongress") or 0)
        sponsored_total = int(sponsored.get("total") or 0)
        committees = directory.get("committees") or []
        score_parts = {
            "service": min(20, round(years / 30 * 20)),
            "legislation": min(25, round(sponsored_total / 100 * 25)),
            "enactedLaws": min(35, len(laws) * 7),
            "committees": min(15, len([row for row in committees if not row.get("subcommittee")]) * 5),
            "recordCompleteness": 5 if directory.get("birthday") else 2,
        }
        score = min(100, sum(score_parts.values()))
        result = {
            "score": score,
            "grade": "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 55 else "D" if score >= 40 else "E",
            "scoreLabel": "Legislative footprint",
            "scoreParts": score_parts,
            "scoreNote": "Measures the size and verifiability of the public record—not ideology, ethics, or job performance.",
            "enactedLaws": laws,
            "enactedLawCountInLoadedRecord": len(laws),
            "sponsoredLoaded": len(sponsored.get("items") or []),
            "directory": directory,
        }
        return self._remember(key, result, 24 * 3600)

    async def roll_call_votes(self, bioguide_id: str, *, limit: int = 12) -> dict[str, Any]:
        """Return recent recorded House votes without putting them on the critical profile path."""
        bioguide = bioguide_id.upper()
        if not BIOGUIDE_RE.fullmatch(bioguide):
            raise CongressError("Invalid Bioguide ID.", status_code=400)
        profile = await self.profile(bioguide)
        if profile.get("currentChamber") != "House":
            return {
                "available": False,
                "chamber": profile.get("currentChamber"),
                "message": "A reliable member-level Senate roll-call feed is not available through Congress.gov yet.",
                "source": {"name": "U.S. Senate Roll Call Votes", "officialUrl": "https://www.senate.gov/legislative/votes_new.htm"},
            }
        current_term = max(profile.get("terms") or [], key=lambda row: int(row.get("startYear") or 0), default={})
        congress = int(current_term.get("congress") or 0)
        if not congress:
            return {"available": False, "message": "No current Congress number was found for this member."}
        session = 1 if time.gmtime().tm_year % 2 else 2
        cache_key = f"house-votes:v3:{congress}:{session}:{bioguide}:{limit}"

        async def fetch() -> dict[str, Any]:
            first = await self._get(f"/house-vote/{congress}/{session}", params={"offset": 0, "limit": 250})
            metadata = list(first.get("houseRollCallVotes") or [])
            total = int((first.get("pagination") or {}).get("count") or len(metadata))
            if len(metadata) < total:
                pages = await asyncio.gather(*[
                    self._get(f"/house-vote/{congress}/{session}", params={"offset": offset, "limit": 250})
                    for offset in range(250, total, 250)
                ], return_exceptions=True)
                for page in pages:
                    if not isinstance(page, Exception):
                        metadata.extend(page.get("houseRollCallVotes") or [])
            metadata.sort(key=lambda row: str(row.get("startDate") or ""), reverse=True)
            recent = metadata[:limit]
            details = await asyncio.gather(*[
                self._house_clerk_vote(str(row.get("sourceDataURL") or ""), bioguide) for row in recent
            ], return_exceptions=True)
            votes = []
            for summary, payload in zip(recent, details):
                if isinstance(payload, Exception) or not payload:
                    continue
                roll = int(summary.get("rollCallNumber") or 0)
                start = str(summary.get("startDate") or "")
                votes.append({
                    "rollCallNumber": roll,
                    "date": start,
                    "vote": payload.get("vote"),
                    "question": payload.get("question"),
                    "description": payload.get("description"),
                    "result": payload.get("result") or summary.get("result"),
                    "voteType": summary.get("voteType"),
                    "legislation": " ".join(str(value) for value in (summary.get("legislationType"), summary.get("legislationNumber")) if value),
                    "legislationUrl": summary.get("legislationUrl"),
                    "officialUrl": f"https://clerk.house.gov/Votes/{start[:4]}{roll:03d}" if roll and start else summary.get("sourceDataURL"),
                })
            return {
                "available": True,
                "chamber": "House",
                "congress": congress,
                "session": session,
                "votes": votes,
                "source": {"name": "Congress.gov / Office of the House Clerk", "officialUrl": "https://clerk.house.gov/Votes"},
            }

        return await cache.cached(cache_key, fetch, ttl=2 * 60 * 60)

    async def _all_sponsored_legislation(self, bioguide: str) -> dict[str, Any]:
        first = await self.legislation(bioguide, "sponsored", 0, 250)
        items = list(first.get("items") or [])
        total = int(first.get("total") or len(items))
        offset = len(items)
        # Congress.gov caps a page at 250. Long-serving members occasionally need a
        # few pages; the cap prevents pathological records from delaying enrichment.
        while offset < total and offset < 1250:
            page = await self.legislation(bioguide, "sponsored", offset, 250)
            page_items = page.get("items") or []
            if not page_items:
                break
            items.extend(page_items)
            offset += len(page_items)
        return {**first, "items": items, "hasMore": offset < total}


congress_client = CongressClient()
