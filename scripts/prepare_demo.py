"""Prepare persistent profiles for a local review, without displaying credentials."""
import asyncio
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / '.env')
from app.congress_client import congress_client
from app.whitehouse_client import whitehouse_client
from app.institution_client import institution_client
from app.organization_knowledge import knowledge
from app.wikipedia_client import wikipedia_knowledge
from app.family import CHILDREN
from app import snapshots

async def main():
    async def prepare(label, work):
        start = time.perf_counter()
        try:
            await asyncio.wait_for(work(), 90)
            print(f'{label}: ready ({time.perf_counter()-start:.1f}s)', flush=True)
        except Exception as error:
            print(f'{label}: unavailable ({type(error).__name__})', flush=True)
    for cik in ['0001067983', '0002012383', '0001423053', '0000102909', '0000093751']:
        await prepare('Institution ' + cik, lambda: institution_client.profile(cik))
        await prepare('Organization biography ' + cik, lambda: knowledge(cik))
    for bioguide in ['P000197', 'T000278', 'S000033', 'O000172']:
        await prepare('Congress ' + bioguide, lambda: congress_client.profile(bioguide))
    for item in await whitehouse_client.index():
        await prepare('White House ' + item['slug'], lambda: whitehouse_client.profile(item['slug']))
    for slug, name in CHILDREN:
        await prepare('Family ' + slug, lambda: wikipedia_knowledge('whitehouse:' + slug, name, []))
    await snapshots.close()

if __name__ == '__main__':
    asyncio.run(main())
