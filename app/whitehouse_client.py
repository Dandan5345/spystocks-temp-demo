from __future__ import annotations

from . import snapshots
from .family import family_index, family_profile, relatives

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from .congress_client import normalize_name

WHITEHOUSE_ROOT = "https://www.whitehouse.gov"
ADMINISTRATION_URL = f"{WHITEHOUSE_ROOT}/administration/"
CACHE_DIR = Path(__file__).resolve().parents[1] / "data"
INDEX_CACHE = CACHE_DIR / "whitehouse-official-index.json"
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ROLE_PREFIXES = ("Vice President", "President", "First Lady", "Second Lady")


class WhiteHouseError(Exception):
    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def split_role_name(label: str) -> tuple[str | None, str]:
    clean = " ".join((label or "").split())
    for prefix in ROLE_PREFIXES:
        if clean.lower().startswith(prefix.lower() + " "):
            return prefix + (" of the United States" if prefix != "President" else " of the United States"), clean[len(prefix):].strip()
    return None, clean


def profile_slug(url: str) -> str | None:
    parsed = urlparse(urljoin(WHITEHOUSE_ROOT, url))
    if parsed.netloc != "www.whitehouse.gov":
        return None
    match = re.fullmatch(r"/(?:the-)?administration/([a-z0-9-]+)/?", parsed.path)
    if not match or match.group(1) in {"cabinet", "the-cabinet"}:
        return None
    return match.group(1)


def parse_administration_index(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    by_slug: dict[str, dict[str, Any]] = {}
    for anchor in soup.select('a[href*="administration/"]'):
        slug = profile_slug(anchor.get("href") or "")
        label = " ".join(anchor.stripped_strings)
        if not slug or not label:
            continue
        role, name = split_role_name(label)
        if not role or len(name.split()) < 2:
            continue
        current = by_slug.get(slug)
        if current and len(current["name"]) >= len(name):
            continue
        by_slug[slug] = {
            "id": f"whitehouse:{slug}",
            "slug": slug,
            "name": name,
            "role": role,
            "sourceType": "whitehouse",
            "profileType": "executive",
            "currentOfficial": True,
            "currentMember": False,
            "party": None,
            "state": "United States",
            "district": None,
            "chamber": "Executive Branch",
            "imageUrl": None,
            "officialUrl": urljoin(WHITEHOUSE_ROOT, anchor.get("href")),
            "_search": normalize_name(name),
        }
    return sorted(by_slug.values(), key=lambda item: item["name"])


def parse_whitehouse_profile(html: str, slug: str, url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    name = " ".join(heading.stripped_strings) if heading else slug.replace("-", " ").title()
    main = soup.find("main") or soup
    paragraphs = [" ".join(node.stripped_strings) for node in main.find_all("p")]
    paragraphs = [text for text in paragraphs if len(text) >= 35]
    role = paragraphs[0] if paragraphs and len(paragraphs[0]) <= 120 else None
    biography = paragraphs[1:] if role else paragraphs
    if not role:
        title_meta = soup.find("meta", attrs={"property": "og:title"})
        role, _ = split_role_name(title_meta.get("content", "") if title_meta else "")
    image_url = None
    image_attribution = None
    name_tokens = set(normalize_name(name).split())
    for image in soup.find_all("img"):
        alt = image.get("alt") or ""
        alt_tokens = set(normalize_name(alt).split())
        if name_tokens and len(name_tokens & alt_tokens) >= max(2, len(name_tokens) - 1):
            candidate = image.get("src") or image.get("data-src") or image.get("data-lazy-src")
            if candidate:
                image_url = urljoin(url, candidate)
                image_attribution = alt or "Official White House image"
                break
    modified = soup.find("meta", attrs={"property": "article:modified_time"})
    return {
        "id": f"whitehouse:{slug}",
        "slug": slug,
        "name": name,
        "profileType": "executive",
        "sourceType": "whitehouse",
        "role": role or "White House Administration",
        "currentOfficial": True,
        "imageUrl": image_url,
        "imageAttribution": image_attribution,
        "biography": biography[:12],
        "officialWebsiteUrl": url,
        "source": {
            "name": "WhiteHouse.gov",
            "description": "Official Website of the White House",
            "updatedAt": modified.get("content") if modified else None,
            "officialUrl": url,
        },
    }


def public_index_item(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if not key.startswith("_")}


def search_whitehouse(index: list[dict[str, Any]], query: str, limit: int = 8) -> list[dict[str, Any]]:
    normalized = normalize_name(query)
    if len(normalized) < 2:
        return []
    tokens = normalized.split()
    reversed_query = " ".join(reversed(tokens)) if len(tokens) > 1 else normalized
    ranked = []
    for person in index:
        target = person.get("_search") or normalize_name(person.get("name", ""))
        target_tokens = target.split()
        exact = normalized == target or reversed_query == target
        prefix = all(any(target_token.startswith(query_token) for target_token in target_tokens) for query_token in tokens)
        score = max(fuzz.ratio(normalized, target), fuzz.token_sort_ratio(normalized, target))
        if exact or prefix or score >= 78:
            ranked.append((300 if exact else 200 + score if prefix else score, person))
    ranked.sort(key=lambda row: (-row[0], row[1]["name"]))
    return [public_index_item(item) for _, item in ranked[:limit]]


class WhiteHouseClient:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport
        self._memory: dict[str, tuple[float, Any]] = {}
        self._index_lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20),
                follow_redirects=True,
                headers={"User-Agent": "Information Check System/1.0", "Accept": "text/html"},
                transport=self.transport,
                http2=False,
            )
        return self._client

    async def _get(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "www.whitehouse.gov":
            raise WhiteHouseError("WhiteHouse.gov returned an invalid URL.")
        try:
            client = self._get_client()
            response = await client.get(url)
            if response.status_code == 404:
                raise WhiteHouseError("WhiteHouse.gov could not find that official.", status_code=404)
            response.raise_for_status()
            return response.text
        except WhiteHouseError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError):
            raise WhiteHouseError("We couldn't reach WhiteHouse.gov right now. Please try again.")

    def _cached(self, key: str) -> Any | None:
        cached = self._memory.get(key)
        if cached and cached[0] > time.time():
            return cached[1]
        self._memory.pop(key, None)
        return None

    def _remember(self, key: str, value: Any, ttl: int = 6 * 3600) -> Any:
        self._memory[key] = (time.time() + ttl, value)
        return value

    async def index(self, *, force: bool = False) -> list[dict[str, Any]]:
        if not force and (cached := self._cached("index")) is not None:
            return cached
        async with self._index_lock:
            if not force and (cached := self._cached("index")) is not None:
                return cached
            ttl = 6 * 3600
            if not force and INDEX_CACHE.exists():
                try:
                    data = json.loads(INDEX_CACHE.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        if time.time() - INDEX_CACHE.stat().st_mtime >= ttl:
                            snapshots.schedule("whitehouse-index", lambda: self.index(force=True))
                        for item in data:
                            item["_search"] = normalize_name(item.get("name", ""))
                        return self._remember("index", data, ttl)
                except (OSError, ValueError):
                    pass
            data = parse_administration_index(await self._get(ADMINISTRATION_URL))
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            temp = INDEX_CACHE.with_suffix(".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            temp.replace(INDEX_CACHE)
            return self._remember("index", data, ttl)

    async def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        return search_whitehouse([*await self.index(), *family_index()], query, limit)

    async def profile(self, slug: str) -> dict[str, Any]:
        slug = slug.lower()
        if not SLUG_RE.fullmatch(slug):
            raise WhiteHouseError("Invalid White House profile ID.", status_code=400)
        family = family_profile(slug)
        if family:
            return family
        key = f"profile:{slug}"
        if (cached := self._cached(key)) is not None:
            return cached
        index = await self.index()
        match = next((item for item in index if item["slug"] == slug), None)
        if not match:
            raise WhiteHouseError("WhiteHouse.gov could not find that official.", status_code=404)
        async def fetch():
            return parse_whitehouse_profile(await self._get(match["officialUrl"]), slug, match["officialUrl"])
        data = await fetch() if self.transport else await snapshots.get("whitehouse:" + slug, fetch, 21600)
        return self._remember(key, {**data, 'family': relatives(slug)})


whitehouse_client = WhiteHouseClient()
