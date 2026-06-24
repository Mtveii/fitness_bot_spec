import os
import json
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
        retry_on_timeout=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    USE_REDIS = True
except Exception:
    logger.warning("Redis not available, using file-based state")


# ─── File-based fallback ─────────────────────────────────────

def _state_path(user_id: int) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"today_{user_id}.json"


def _load_file_state(user_id: int) -> dict:
    path = _state_path(user_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return _default_today()


def _save_file_state(user_id: int, data: dict):
    path = _state_path(user_id)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


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


def _today_key(user_id: int) -> str:
    return f"today:{user_id}:{date.today().isoformat()}"


async def get_today_state(user_id: int) -> dict:
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
                return result
        except Exception as e:
            logger.warning(f"Redis error: {e}")

    return _load_file_state(user_id)


async def update_today_state(user_id: int, **kwargs) -> dict:
    if USE_REDIS:
        try:
            key = _today_key(user_id)
            if not await redis.exists(key):
                await redis.hset(key, mapping={k: str(v) for k, v in DEFAULT_TODAY.items()})
            for k, v in kwargs.items():
                if k in DEFAULT_TODAY:
                    await redis.hset(key, k, str(v))
            data = await redis.hgetall(key)
            cal_in = float(data.get("calories_in", 0))
            cal_out = float(data.get("calories_out", 0))
            await redis.hset(key, "balance", str(cal_in - cal_out))
            await redis.expire(key, 86400 * 2)
            return await get_today_state(user_id)
        except Exception as e:
            logger.warning(f"Redis error: {e}")

    state = _load_file_state(user_id)
    for k, v in kwargs.items():
        if k in state:
            state[k] = v
    state["balance"] = state["calories_in"] - state["calories_out"]
    _save_file_state(user_id, state)
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
