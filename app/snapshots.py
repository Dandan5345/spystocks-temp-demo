"""Persistent snapshots and bounded, deduplicated background refreshes."""
import asyncio
import logging
import time
from . import cache

jobs = {}
retry_after = {}
logger = logging.getLogger(__name__)

def schedule(key, producer):
    if key in jobs or retry_after.get(key, 0) > time.time():
        return
    async def run():
        try:
            await producer()
        except Exception:
            retry_after[key] = time.time() + 60
            logger.warning("Background refresh failed for %s", key)
        finally:
            jobs.pop(key, None)
    jobs[key] = asyncio.create_task(run())

async def read(key):
    return await cache.get("snapshot:v1:" + key)

async def save(key, data):
    await cache.set("snapshot:v1:" + key, {"data": data, "fetchedAt": time.time()})
    return data

async def get(key, producer, ttl=14400):
    hit = await read(key)
    if hit:
        if hit["fetchedAt"] + ttl < time.time():
            schedule(key, lambda: refresh(key, producer))
        return hit["data"]
    return await refresh(key, producer)

async def refresh(key, producer):
    async with await cache.key_lock("snapshot-job:" + key):
        hit = await read(key)
        if hit and hit["fetchedAt"] + 5 > time.time():
            return hit["data"]
        return await save(key, await producer())

async def close():
    tasks = list(jobs.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
