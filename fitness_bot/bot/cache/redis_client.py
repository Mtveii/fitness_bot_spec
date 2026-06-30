import json
import os
import time
import asyncio
from typing import Optional, Any

from bot.config import REDIS_URL

_redis = None
_last_connect_attempt = 0
_fallback_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "cache_fallback")
os.makedirs(_fallback_dir, exist_ok=True)


def _get_fallback_path(key: str) -> str:
    safe = key.replace(":", "_").replace("/", "_")
    return os.path.join(_fallback_dir, f"{safe}.json")


async def get_redis():
    global _redis, _last_connect_attempt
    now = time.time()
    if _redis is None and now - _last_connect_attempt < 60:
        return None
    if _redis is None:
        _last_connect_attempt = now
        try:
            import redis.asyncio as aioredis
            _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
            await _redis.ping()
        except Exception:
            _redis = None
    return _redis


async def cache_get(key: str) -> Optional[Any]:
    r = await get_redis()
    if r:
        try:
            val = await r.get(key)
            if val:
                return json.loads(val)
        except Exception:
            pass
    fpath = _get_fallback_path(key)
    if os.path.exists(fpath):
        try:
            loop = asyncio.get_event_loop()
            entry = await loop.run_in_executor(None, _read_fallback_sync, fpath)
            if "expires_at" in entry and entry["expires_at"] < time.time():
                await loop.run_in_executor(None, os.remove, fpath)
                return None
            return entry.get("value")
        except Exception:
            return None
    return None


def _read_fallback_sync(fpath: str) -> dict:
    with open(fpath, "r") as f:
        return json.load(f)


def _write_fallback_sync(fpath: str, entry: dict):
    with open(fpath, "w") as f:
        json.dump(entry, f, ensure_ascii=False, default=str)


async def cache_set(key: str, value: Any, ttl: int = 3600):
    r = await get_redis()
    data = json.dumps(value, ensure_ascii=False, default=str)
    if r:
        try:
            await r.setex(key, ttl, data)
            return
        except Exception:
            pass
    fpath = _get_fallback_path(key)
    entry = {"value": value, "expires_at": time.time() + ttl}
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _write_fallback_sync, fpath, entry)


async def cache_delete(key: str):
    r = await get_redis()
    if r:
        try:
            await r.delete(key)
        except Exception:
            pass
    fpath = _get_fallback_path(key)
    if os.path.exists(fpath):
        os.remove(fpath)
