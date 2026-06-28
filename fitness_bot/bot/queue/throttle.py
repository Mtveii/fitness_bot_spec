"""
P1.7 — антифлуд на пользователя (rate-limit на вызовы ИИ).
"""
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
