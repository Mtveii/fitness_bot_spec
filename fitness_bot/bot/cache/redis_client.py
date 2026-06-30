import json
import os
import pickle
from typing import Optional, Any

from bot.config import REDIS_URL

_redis = None
_fallback_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "cache_fallback")
os.makedirs(_fallback_dir, exist_ok=True)


def _get_fallback_path(key: str) -> str:
    safe = key.replace(":", "_").replace("/", "_")
    return os.path.join(_fallback_dir, f"{safe}.json")


async def get_redis():
    global _redis
    if _redis is None:
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
        with open(fpath, "r") as f:
            return json.load(f)
    return None


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
    with open(fpath, "w") as f:
        f.write(data)


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
