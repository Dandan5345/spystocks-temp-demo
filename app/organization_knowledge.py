"""Explicit organization-to-article mappings, avoiding fuzzy corporate namesakes."""
import httpx
from urllib.parse import quote
from . import snapshots

ARTICLES = {
    '0002012383': ('BlackRock', 'https://www.blackrock.com/corporate'),
    '0001364742': ('BlackRock', 'https://www.blackrock.com/corporate'),
    '0001067983': ('Berkshire Hathaway', 'https://www.berkshirehathaway.com/'),
    '0001423053': ('Citadel LLC', 'https://www.citadel.com/'),
    '0000102909': ('The Vanguard Group', 'https://corporate.vanguard.com/'),
    '0000093751': ('State Street Corporation', 'https://www.statestreet.com/'),
}

async def knowledge(cik):
    match = ARTICLES.get(cik.zfill(10))
    if not match:
        return None
    title, website = match
    async def fetch():
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            response = await client.get('https://en.wikipedia.org/api/rest_v1/page/summary/' + quote(title),
                                        headers={'User-Agent': 'Information Check System/0.2'})
            response.raise_for_status()
            data = response.json()
        return {'title': data.get('title', title), 'summary': data.get('extract'),
                'url': 'https://en.wikipedia.org/wiki/' + quote(title.replace(' ', '_')),
                'officialWebsite': website, 'attribution': 'Wikipedia contributors · CC BY-SA 4.0'}
    return await snapshots.get('organization-knowledge:' + cik.zfill(10), fetch, 604800)
