from __future__ import annotations

from pathlib import Path
import asyncio
from contextlib import asynccontextmanager
from . import snapshots

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .congress_client import CongressError, congress_client
from .disclosure_client import DisclosureError, disclosure_client
from .institution_client import InstitutionError, institution_client
from .sec_client import build_profile, build_profile_overview, search_cik
from .whitehouse_client import WhiteHouseError, whitehouse_client
from .wikipedia_client import wikipedia_knowledge
from .organization_knowledge import knowledge as organization_knowledge

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"

@asynccontextmanager
async def lifespan(app):
    yield
    await snapshots.close()

app = FastAPI(title="מערכת בדיקת מידע", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")


@app.get("/insider/{cik}")
def insider_page(cik: str):
    return FileResponse(STATIC / "index.html")


@app.get("/politician/{bioguide_id}")
def politician_page(bioguide_id: str):
    return FileResponse(STATIC / "index.html")


@app.get("/politician/whitehouse/{slug}")
def whitehouse_politician_page(slug: str):
    return FileResponse(STATIC / "index.html")


@app.get("/institution/{cik}")
def institution_page(cik: str):
    return FileResponse(STATIC / "index.html")


@app.get("/api/search")
async def search(q: str = Query(..., min_length=2), limit: int = Query(12, ge=1, le=25)):
    try:
        return {"query": q, "results": await search_cik(q, limit=limit)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"SEC lookup failed: {e}")


@app.get("/api/profile/{cik}")
async def profile(
    cik: str,
    max_filings: int = Query(120, ge=1, le=500),
    include_photo: bool = Query(True),
):
    if not cik.isdigit():
        raise HTTPException(status_code=400, detail="CIK must be numeric")
    try:
        data = await build_profile(cik, max_filings=max_filings, include_photo=include_photo)
        if not data.get("person", {}).get("name"):
            raise HTTPException(status_code=404, detail="No SEC filer found for that CIK")
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not build SEC profile: {e}")


@app.get("/api/profile/{cik}/overview")
async def profile_overview(cik: str):
    if not cik.isdigit():
        raise HTTPException(status_code=400, detail="CIK must be numeric")
    try:
        data = await build_profile_overview(cik)
        if not data.get("person", {}).get("name"):
            raise HTTPException(status_code=404, detail="No SEC filer found for that CIK")
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not load SEC identity: {e}")


@app.get("/api/profile/{cik}/knowledge")
async def profile_knowledge(cik: str):
    if not cik.isdigit():
        raise HTTPException(status_code=400, detail="CIK must be numeric")
    try:
        overview = await build_profile_overview(cik)
        person = overview.get("person") or {}
        if not person.get("name"):
            raise HTTPException(status_code=404, detail="No SEC filer found for that CIK")
        return {"knowledge": await wikipedia_knowledge(cik, person["name"], person.get("aliases") or [])}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not load public biography: {e}")


def congress_http_error(error: CongressError) -> HTTPException:
    headers = {"Retry-After": error.retry_after} if error.retry_after else None
    return HTTPException(status_code=error.status_code, detail=str(error), headers=headers)


@app.get("/api/politicians/search")
async def politician_search(q: str = Query(..., min_length=2), limit: int = Query(12, ge=1, le=25)):
    results = await asyncio.gather(congress_client.search(q, limit=limit),
                                   whitehouse_client.search(q, limit=limit), return_exceptions=True)
    congress_results = [] if isinstance(results[0], Exception) else results[0]
    whitehouse_results = [] if isinstance(results[1], Exception) else results[1]
    if all(isinstance(result, Exception) for result in results):
        raise HTTPException(status_code=502, detail="People search is temporarily unavailable.")
    query = " ".join(q.casefold().split())
    combined = whitehouse_results + congress_results
    combined.sort(key=lambda item: (
        0 if " ".join(str(item.get("name", "")).casefold().split()) == query else 1,
        0 if item.get("sourceType") == "whitehouse" else 1,
        str(item.get("name", "")),
    ))
    return combined[:limit]


@app.get("/api/politicians/whitehouse/{slug}")
async def whitehouse_politician_profile(slug: str):
    try:
        return await whitehouse_client.profile(slug)
    except WhiteHouseError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error))


@app.get("/api/politicians/{bioguide_id}")
async def politician_profile(bioguide_id: str):
    try:
        return await congress_client.profile(bioguide_id)
    except CongressError as error:
        raise congress_http_error(error)


@app.get("/api/politicians/{bioguide_id}/overview")
async def politician_overview(bioguide_id: str):
    try:
        return await congress_client.overview(bioguide_id)
    except CongressError as error:
        raise congress_http_error(error)


@app.get("/api/politicians/{bioguide_id}/legislation")
async def politician_legislation(
    bioguide_id: str,
    kind: str = Query(..., pattern="^(sponsored|cosponsored)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(12, ge=1, le=50),
):
    try:
        return await congress_client.legislation(bioguide_id, kind, offset, limit)  # type: ignore[arg-type]
    except CongressError as error:
        raise congress_http_error(error)


@app.get("/api/politicians/{bioguide_id}/disclosures")
async def politician_disclosures(bioguide_id: str):
    try:
        member = await congress_client.profile(bioguide_id)
        return await disclosure_client.disclosures(member)
    except CongressError as error:
        raise congress_http_error(error)
    except DisclosureError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error))
    except Exception:
        raise HTTPException(status_code=502, detail="Could not read the official financial disclosure source right now.")


@app.get("/api/institutions/search")
async def institution_search(q: str = Query(..., min_length=2), limit: int = Query(12, ge=1, le=25)):
    try:
        return await institution_client.search(q, limit=limit)
    except InstitutionError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error))


@app.get("/api/institutions/{cik}")
async def institution_profile(cik: str):
    try:
        data = await institution_client.profile(cik)
        return {key: value for key, value in data.items() if not key.startswith("_")}
    except InstitutionError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error))


@app.get("/api/institutions/{cik}/overview")
async def institution_overview(cik: str):
    try:
        return await institution_client.overview(cik)
    except InstitutionError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error))


@app.get("/api/institutions/{cik}/knowledge")
async def institution_knowledge(cik: str):
    if not cik.isdigit() or len(cik) > 10:
        raise HTTPException(status_code=400, detail="CIK must be numeric.")
    try:
        return {"knowledge": await organization_knowledge(cik)}
    except Exception:
        raise HTTPException(status_code=502, detail="Organization biography is temporarily unavailable.")


@app.get("/api/politicians/whitehouse/{slug}/knowledge")
async def whitehouse_knowledge(slug: str):
    profile = await whitehouse_politician_profile(slug)
    try:
        return {"knowledge": await wikipedia_knowledge('whitehouse:' + slug, profile['name'], [])}
    except Exception:
        raise HTTPException(status_code=502, detail="Public biography is temporarily unavailable.")


@app.get("/api/institutions/{cik}/holdings")
async def institution_holdings(cik: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    try:
        return await institution_client.holdings(cik, offset, limit)
    except InstitutionError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error))
