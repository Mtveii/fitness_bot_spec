import os
import json
import asyncio
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

USE_REDIS = False
redis = None

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DATA_DIR = Path(__file__).parent.parent.parent / "data"
STATE_DIR = DATA_DIR / "state"

try:
    import redis.asyncio as aioredis
    redis = aioredis.from_url(
        REDIS_URL,
        decode_responses=True,
        retry_on_timeout=False,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
    )
    _loop = asyncio.new_event_loop()
    _loop.run_until_complete(redis.ping())
    _loop.close()
    USE_REDIS = True
    logger.info("Redis connected")
except Exception:
    logger.warning("Redis not available, using file-based state")
    USE_REDIS = False
    redis = None


# ─── File-based fallback ─────────────────────────────────────

def _state_path(user_id: int) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"today_{user_id}.json"


def _load_file_state_sync(user_id: int) -> dict:
    path = _state_path(user_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return _default_today()


async def _load_file_state(user_id: int) -> dict:
    return await asyncio.to_thread(_load_file_state_sync, user_id)


def _save_file_state_sync(user_id: int, data: dict):
    path = _state_path(user_id)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


async def _save_file_state(user_id: int, data: dict):
    await asyncio.to_thread(_save_file_state_sync, user_id, data)


def _default_today() -> dict:
    return {
        "calories_in": 0,
        "calories_out": 0,
        "protein": 0,
        "fat": 0,
        "carbs": 0,
        "steps": 0,
        "workout_kcal": 0,
        "balance": 0,
    }


# ─── Today State ─────────────────────────────────────────────

DEFAULT_TODAY = _default_today()

_CONSUMED_FIELDS = {"calories_in", "calories_out", "protein", "fat", "carbs", "steps", "workout_kcal"}


def _today_key(user_id: int) -> str:
    return f"today:{user_id}:{date.today().isoformat()}"


async def _sanitize_and_persist(user_id: int, key: str | None, state: dict) -> dict:
    sanitized = False
    for k in _CONSUMED_FIELDS:
        if k in state and state[k] < 0:
            state[k] = 0
            sanitized = True
    if sanitized and key is not None:
        for k in _CONSUMED_FIELDS:
            await redis.hset(key, k, str(state[k]))
        balance = state.get("calories_in", 0) - state.get("calories_out", 0)
        await redis.hset(key, "balance", str(balance))
    return state


async def get_today_state(user_id: int, day: date | None = None) -> dict:
    if day is not None and USE_REDIS:
        try:
            key = f"today:{user_id}:{day.isoformat()}"
            data = await redis.hgetall(key)
            if data:
                result = {}
                for k, v in data.items():
                    try:
                        result[k] = int(v) if k == "steps" else float(v)
                    except (ValueError, TypeError):
                        result[k] = DEFAULT_TODAY.get(k, 0)
                return await _sanitize_and_persist(user_id, key, result)
        except Exception as e:
            logger.warning(f"Redis error: {e}")
        return dict(DEFAULT_TODAY)
    if USE_REDIS:
        try:
            key = _today_key(user_id)
            data = await redis.hgetall(key)
            if data:
                result = {}
                for k, v in data.items():
                    try:
                        result[k] = int(v) if k == "steps" else float(v)
                    except (ValueError, TypeError):
                        result[k] = DEFAULT_TODAY.get(k, 0)
                return await _sanitize_and_persist(user_id, key, result)
        except Exception as e:
            logger.warning(f"Redis error: {e}")

    state = await _load_file_state(user_id)
    sanitized = False
    for k in _CONSUMED_FIELDS:
        if k in state and state[k] < 0:
            state[k] = 0
            sanitized = True
    if sanitized:
        await _save_file_state(user_id, state)
    return state


async def update_today_state(user_id: int, **kwargs) -> dict:
    if USE_REDIS:
        try:
            key = _today_key(user_id)
            if not await redis.exists(key):
                await redis.hset(key, mapping={k: str(v) for k, v in DEFAULT_TODAY.items()})
            for k, v in kwargs.items():
                if k in DEFAULT_TODAY and k != "balance":
                    current = await redis.hget(key, k)
                    if k == "steps":
                        current_val = int(current) if current else 0
                        new_val = max(current_val + int(v), 0)
                        await redis.hset(key, k, str(new_val))
                    else:
                        current_val = float(current) if current else 0.0
                        new_val = max(current_val + float(v), 0.0)
                        await redis.hset(key, k, str(new_val))
            data = await redis.hgetall(key)
            cal_in = float(data.get("calories_in", 0))
            cal_out = float(data.get("calories_out", 0))
            await redis.hset(key, "balance", str(cal_in - cal_out))
            await redis.expire(key, 86400 * 2)
            return await get_today_state(user_id)
        except Exception as e:
            logger.warning(f"Redis error: {e}")

    state = await _load_file_state(user_id)
    for k, v in kwargs.items():
        if k in state and k != "balance":
            state[k] = max(state.get(k, 0) + v, 0)
    state["balance"] = state["calories_in"] - state["calories_out"]
    await _save_file_state(user_id, state)
    return state


async def reset_today_state(user_id: int) -> None:
    if USE_REDIS:
        try:
            await redis.delete(_today_key(user_id))
            return
        except Exception as e:
            logger.warning(f"Redis error: {e}")

    path = _state_path(user_id)
    if path.exists():
        path.unlink()


# ─── Chat History ────────────────────────────────────────────

def _chat_path(user_id: int) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"chat_{user_id}.json"


async def get_chat_history(user_id: int, limit: int = 10) -> list[dict]:
    if USE_REDIS:
        try:
            key = f"chat:{user_id}"
            messages = await redis.lrange(key, 0, limit - 1)
            return [json.loads(m) for m in messages]
        except Exception:
            pass

    path = _chat_path(user_id)
    if path.exists():
        try:
            msgs = json.loads(path.read_text(encoding="utf-8"))
            return msgs[:limit]
        except Exception:
            pass
    return []


async def add_chat_message(user_id: int, role: str, content: str) -> None:
    if USE_REDIS:
        try:
            key = f"chat:{user_id}"
            msg = json.dumps({"role": role, "content": content})
            await redis.lpush(key, msg)
            await redis.ltrim(key, 0, 9)
            await redis.expire(key, 86400)
            return
        except Exception:
            pass

    path = _chat_path(user_id)
    try:
        msgs = []
        if path.exists():
            msgs = json.loads(path.read_text(encoding="utf-8"))
        msgs.insert(0, {"role": role, "content": content})
        msgs = msgs[:10]
        path.write_text(json.dumps(msgs, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ─── Context Cache (P2.11) ──────────────────────────────────


async def get_cached_context(user_id: int) -> dict | None:
    if USE_REDIS:
        try:
            data = await redis.get(f"ctx:{user_id}")
            if data:
                return json.loads(data)
        except Exception as e:
            logger.warning(f"Redis error: {e}")
    return None


async def set_cached_context(user_id: int, ctx: dict, ttl: int = 45) -> None:
    if USE_REDIS:
        try:
            await redis.set(f"ctx:{user_id}", json.dumps(ctx, default=str), ex=ttl)
        except Exception as e:
            logger.warning(f"Redis error: {e}")


async def invalidate_context(user_id: int) -> None:
    if USE_REDIS:
        try:
            await redis.delete(f"ctx:{user_id}")
        except Exception as e:
            logger.warning(f"Redis error: {e}")


# ─── Decrement Today State (P4.17) ──────────────────────────


async def decrement_today_state(user_id: int, **kwargs) -> dict:
    deltas = {k: -v for k, v in kwargs.items() if k in DEFAULT_TODAY}
    return await update_today_state(user_id, **deltas)


# ─── Photo Cache ─────────────────────────────────────────────

PHOTO_CACHE_TTL = 60 * 60 * 24 * 30  # 30 дней


async def get_photo_cache(photo_hash: str) -> dict | None:
    if USE_REDIS:
        try:
            data = await redis.get(f"photo:{photo_hash}")
            if data:
                return json.loads(data)
        except Exception as e:
            logger.warning(f"Redis error: {e}")
        return None
    path = DATA_DIR / "photo_cache" / f"{photo_hash}.json"
    if path.exists():
        try:
            return json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
        except Exception:
            return None
    return None


async def set_photo_cache(photo_hash: str, result: dict) -> None:
    if USE_REDIS:
        try:
            await redis.set(f"photo:{photo_hash}", json.dumps(result, ensure_ascii=False), ex=PHOTO_CACHE_TTL)
            return
        except Exception as e:
            logger.warning(f"Redis error: {e}")
    dir_path = DATA_DIR / "photo_cache"

    def _write():
        dir_path.mkdir(parents=True, exist_ok=True)
        (dir_path / f"{photo_hash}.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )

    await asyncio.to_thread(_write)