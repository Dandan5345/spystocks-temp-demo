from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from . import cache
from .sec_client import name_variants, normalize_name

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
KNOWLEDGE_TTL = 7 * 24 * 60 * 60


def _candidate_names(name: str, aliases: list[dict[str, Any]] | list[str]) -> set[str]:
    names = set(name_variants(name))
    for alias in aliases:
        value = alias.get("name") if isinstance(alias, dict) else alias
        if value:
            names.update(name_variants(str(value)))
    return {normalize_name(value) for value in names if value}


async def _fetch_knowledge(name: str, aliases: list[dict[str, Any]] | list[str]) -> dict[str, Any] | None:
    accepted_names = _candidate_names(name, aliases)
    # Search several legal-name orderings in one request. We deliberately do not use a
    # fuzzy winner: showing no biography is safer than attaching a namesake's portrait.
    terms = sorted(accepted_names, key=len, reverse=True)[:6]
    search = " OR ".join(f'\"{term.title()}\"' for term in terms)
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": search,
        "gsrnamespace": 0,
        "gsrlimit": 8,
        "prop": "extracts|pageimages|pageprops",
        "exintro": 1,
        "explaintext": 1,
        "piprop": "thumbnail|original",
        "pithumbsize": 640,
        "format": "json",
        "formatversion": 2,
    }
    headers = {"User-Agent": "Information Check System contact@example.com"}
    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
        response = await client.get(WIKIPEDIA_API, params=params, headers=headers)
        response.raise_for_status()
        pages = response.json().get("query", {}).get("pages", [])

    candidate = select_candidate(name, aliases, pages)
    if candidate and candidate.get("wikidata_id"):
        try:
            candidate["facts"] = await _wikidata_facts(candidate["wikidata_id"])
        except Exception:
            candidate["facts"] = {}
    return candidate


def select_candidate(name: str, aliases: list[dict[str, Any]] | list[str], pages: list[dict[str, Any]]) -> dict[str, Any] | None:
    accepted_names = _candidate_names(name, aliases)
    for page in pages:
        title = page.get("title") or ""
        extract = re.sub(r"\s+", " ", page.get("extract") or "").strip()
        if "disambiguation" in (page.get("pageprops") or {}):
            continue
        # An exact title plus the legal name appearing in the lead are independent,
        # visible evidence signals. Require both before returning any person content.
        normalized_extract = normalize_name(extract[:600])
        normalized_title = normalize_name(title)
        matched_name = next((variant for variant in accepted_names if variant in normalized_extract), None)
        exact_title = normalized_title in accepted_names
        natural_title = _natural_title_match(normalized_title, normalized_extract, accepted_names)
        if not ((exact_title and matched_name) or natural_title):
            continue
        image = (page.get("original") or page.get("thumbnail") or {}).get("source")
        return {
            "title": title,
            "summary": extract[:1800],
            "url": f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
            "image_url": image,
            "source": "Wikipedia",
            "image_source": "Wikimedia Commons / Wikipedia" if image else None,
            "confidence": "high",
            "wikidata_id": (page.get("pageprops") or {}).get("wikibase_item"),
            "evidence_signals": [
                "Article title matches an SEC-reported legal-name form",
                "The article lead repeats that SEC-reported name",
            ],
        }
    return None


WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_PROPERTIES = {
    "P569": "Born",
    "P19": "Place of birth",
    "P27": "Citizenship",
    "P69": "Education",
    "P106": "Occupation",
    "P108": "Employer",
    "P39": "Positions held",
    "P26": "Spouse",
    "P40": "Children",
    "P166": "Awards",
}


def _claim_value(claim: dict[str, Any]) -> tuple[str, str] | None:
    value = (((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value"))
    if isinstance(value, dict) and value.get("id"):
        return "entity", str(value["id"])
    if isinstance(value, dict) and value.get("time"):
        raw = str(value["time"]).lstrip("+")
        precision = int(value.get("precision") or 9)
        return "literal", raw[:10] if precision >= 11 else raw[:7] if precision == 10 else raw[:4]
    if isinstance(value, str):
        return "literal", value
    return None


async def _wikidata_facts(entity_id: str) -> dict[str, list[str]]:
    params = {
        "action": "wbgetentities", "ids": entity_id, "props": "claims", "format": "json",
        "languages": "en", "languagefallback": 1,
    }
    headers = {"User-Agent": "Information Check System contact@example.com"}
    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
        response = await client.get(WIKIDATA_API, params=params, headers=headers)
        response.raise_for_status()
        entity = (response.json().get("entities") or {}).get(entity_id) or {}
        claims = entity.get("claims") or {}
        parsed: dict[str, list[tuple[str, str]]] = {}
        entity_ids: set[str] = set()
        for prop, label in WIKIDATA_PROPERTIES.items():
            values = []
            for claim in (claims.get(prop) or [])[:10]:
                parsed_value = _claim_value(claim)
                if not parsed_value:
                    continue
                values.append(parsed_value)
                if parsed_value[0] == "entity":
                    entity_ids.add(parsed_value[1])
            if values:
                parsed[label] = values
        labels: dict[str, str] = {}
        ids = sorted(entity_ids)
        for start in range(0, len(ids), 50):
            label_response = await client.get(WIKIDATA_API, params={
                "action": "wbgetentities", "ids": "|".join(ids[start:start + 50]),
                "props": "labels", "languages": "en", "languagefallback": 1, "format": "json",
            }, headers=headers)
            label_response.raise_for_status()
            for key, value in (label_response.json().get("entities") or {}).items():
                labels[key] = ((value.get("labels") or {}).get("en") or {}).get("value") or key
    return {
        label: list(dict.fromkeys(labels.get(value, value) if kind == "entity" else value for kind, value in values))[:8]
        for label, values in parsed.items()
    }


def _natural_title_match(title: str, extract: str, accepted_names: set[str]) -> bool:
    """Accept common public-name titles without opening the door to middle-name matches.

    SEC usually stores ``LAST FIRST MIDDLE`` while biographies may use a shortened
    first name (``Tim Cook``) or a fused nickname (``Jensen Huang``). We only allow
    this when the article title has exactly two tokens, the surname is exact, the first
    token is a prefix in either direction, and every non-initial legal-name token is
    present in the lead. A title such as ``Jane A Doe`` therefore remains rejected.
    """
    title_tokens = title.split()
    extract_tokens = set(extract.split())
    if len(title_tokens) != 2:
        return False
    for variant in accepted_names:
        tokens = [token for token in variant.split() if len(token) > 1]
        if len(tokens) < 2 or tokens[-1] != title_tokens[-1]:
            continue
        if not (tokens[0].startswith(title_tokens[0]) or title_tokens[0].startswith(tokens[0])):
            continue
        if all(token in extract_tokens for token in tokens):
            return True
    return False


async def wikipedia_knowledge(cik: str, name: str, aliases: list[dict[str, Any]] | list[str]) -> dict[str, Any] | None:
    wrapped = await cache.cached(
        f"wikipedia:v1:{str(cik).zfill(10)}:{normalize_name(name)}",
        lambda: _fetch_wrapped(name, aliases),
        ttl=KNOWLEDGE_TTL,
    )
    return wrapped.get("knowledge")


async def _fetch_wrapped(name: str, aliases: list[dict[str, Any]] | list[str]) -> dict[str, Any]:
    # Wrap misses so the generic cache can remember a conservative "no match" result.
    return {"knowledge": await _fetch_knowledge(name, aliases)}
