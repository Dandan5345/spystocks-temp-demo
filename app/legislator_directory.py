from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import yaml

from . import cache

BASE = "https://unitedstates.github.io/congress-legislators"
DIRECTORY_TTL = 24 * 60 * 60
_client: httpx.AsyncClient | None = None


async def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(12.0),
            follow_redirects=True,
            headers={"User-Agent": "Information Check System contact@example.com"},
        )
    return _client


async def _yaml(name: str) -> Any:
    async def fetch():
        response = await (await _http()).get(f"{BASE}/{name}.yaml")
        response.raise_for_status()
        return yaml.safe_load(response.text)

    return await cache.cached(f"legislator-directory:v2:{name}", fetch, ttl=DIRECTORY_TTL)


def _committee_index(committees: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for committee in committees or []:
        code = str(committee.get("thomas_id") or "")
        if not code:
            continue
        result[code] = committee
        for subcommittee in committee.get("subcommittees") or []:
            subcode = str(subcommittee.get("thomas_id") or "")
            if subcode:
                result[code + subcode] = {**committee, "subcommittee": subcommittee.get("name")}
    return result


def _person_details(records: list[dict[str, Any]], bioguide: str) -> dict[str, Any]:
    record = next(
        (row for row in records or [] if str((row.get("id") or {}).get("bioguide") or "").upper() == bioguide),
        None,
    )
    if not record:
        return {}
    current_term = max(record.get("terms") or [], key=lambda row: row.get("start") or "", default={})
    leadership = [
        {"title": role.get("title"), "chamber": role.get("chamber"), "start": role.get("start"), "end": role.get("end")}
        for role in record.get("leadership_roles") or []
    ]
    return {
        "birthday": (record.get("bio") or {}).get("birthday"),
        "gender": (record.get("bio") or {}).get("gender"),
        "office": current_term.get("office"),
        "phone": current_term.get("phone"),
        "address": current_term.get("address"),
        "contactForm": current_term.get("contact_form"),
        "leadershipRoles": leadership,
        "identifiers": {
            key: value for key, value in (record.get("id") or {}).items()
            if key in {"bioguide", "wikidata", "wikipedia", "govtrack", "opensecrets", "fec"}
        },
    }


async def legislator_directory(bioguide_id: str, current_member: bool) -> dict[str, Any]:
    bioguide = bioguide_id.upper()
    membership, committees, current = await asyncio.gather(
        _yaml("committee-membership-current"),
        _yaml("committees-current"),
        _yaml("legislators-current"),
    )
    records = current
    if not current_member and not any(
        str((row.get("id") or {}).get("bioguide") or "").upper() == bioguide for row in current or []
    ):
        historical = await _yaml("legislators-historical")
        records = (current or []) + (historical or [])

    lookup = _committee_index(committees or [])
    assignments = []
    for code, members in (membership or {}).items():
        member = next((row for row in members or [] if str(row.get("bioguide") or "").upper() == bioguide), None)
        if not member:
            continue
        committee = lookup.get(str(code)) or lookup.get(str(code)[:4]) or {}
        assignments.append({
            "code": code,
            "name": committee.get("name") or code,
            "subcommittee": committee.get("subcommittee"),
            "chamber": committee.get("type"),
            "title": member.get("title") or "Member",
            "rank": member.get("rank"),
            "side": member.get("party"),
            "url": committee.get("url"),
        })
    assignments.sort(key=lambda row: (bool(row.get("subcommittee")), row.get("rank") or 99, row["name"]))
    return {
        **_person_details(records or [], bioguide),
        "committees": assignments,
        "committeeUpdatedAt": time.strftime("%Y-%m-%d", time.gmtime()),
        "source": {
            "name": "Congress Legislators Project",
            "url": "https://github.com/unitedstates/congress-legislators",
            "note": "Current assignments compiled from official House and Senate records.",
        },
    }
