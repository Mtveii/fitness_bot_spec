"""
P1.7 — антифлуд на пользователя (rate-limit на вызовы ИИ).
P1.8 — debounce: если юзер пишет 2+ сообщения подряд быстро,
       они склеиваются в один запрос к ИИ вместо параллельных гонок.
"""
import asyncio
import time
import logging
from collections import deque

logger = logging.getLogger(__name__)

# ─── P1.7: Rate-limit ────────────────────────────────────────
RATE_LIMIT_COUNT = 8       # макс. запросов к ИИ
RATE_LIMIT_WINDOW_SEC = 60  # за это окно времени

_request_log: dict[int, deque] = {}


def is_rate_limited(user_id: int) -> bool:
    """True если юзер превысил лимит запросов к ИИ за окно (P1.7)."""
    now = time.monotonic()
    log = _request_log.setdefault(user_id, deque())
    while log and now - log[0] > RATE_LIMIT_WINDOW_SEC:
        log.popleft()
    if len(log) >= RATE_LIMIT_COUNT:
        return True
    log.append(now)
    return False


# ─── P1.8: Debounce ──────────────────────────────────────────
# user_id -> {"buffer": [str], "task": asyncio.Task}
_pending: dict[int, dict] = {}
DEBOUNCE_SEC = 1.2  # сколько ждать новых сообщений перед отправкой в ИИ


async def debounce_message(user_id: int, text: str, handler) -> None:
    """
    Копит сообщения юзера DEBOUNCE_SEC секунд, затем склеивает их
    через '\n' и вызывает handler(user_id, combined_text) один раз.
    handler — async функция, принимающая (user_id, text) и отправляющая ответ.
    """
    entry = _pending.get(user_id)
    if entry:
        entry["buffer"].append(text)
        entry["task"].cancel()
    else:
        entry = {"buffer": [text], "task": None}
        _pending[user_id] = entry

    async def _flush():
        try:
            await asyncio.sleep(DEBOUNCE_SEC)
        except asyncio.CancelledError:
            return
        data = _pending.pop(user_id, None)
        if not data:
            return
        combined = "\n".join(data["buffer"])
        await handler(user_id, combined)

    entry["task"] = asyncio.create_task(_flush())
