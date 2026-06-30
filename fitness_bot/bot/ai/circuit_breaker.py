import time
import logging

logger = logging.getLogger(__name__)

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, recovery_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._failures: dict[str, int] = {}
        self._state: dict[str, str] = {}
        self._opened_at: dict[str, float] = {}

    def _get_state(self, provider: str) -> str:
        if provider not in self._state:
            return CLOSED
        state = self._state[provider]
        if state == OPEN:
            if time.time() - self._opened_at[provider] >= self.recovery_timeout:
                self._state[provider] = HALF_OPEN
                logger.info(f"Circuit breaker HALF_OPEN for {provider}")
                return HALF_OPEN
        return state

    def record_success(self, provider: str):
        self._failures[provider] = 0
        self._state[provider] = CLOSED

    def record_failure(self, provider: str):
        self._failures[provider] = self._failures.get(provider, 0) + 1
        if self._failures[provider] >= self.failure_threshold:
            self._state[provider] = OPEN
            self._opened_at[provider] = time.time()
            logger.warning(
                f"Circuit breaker OPEN for {provider} "
                f"({self._failures[provider]} failures)"
            )

    def allow_request(self, provider: str) -> bool:
        state = self._get_state(provider)
        if state == CLOSED:
            return True
        if state == HALF_OPEN:
            return True
        return False

    def get_available_providers(self, providers: list[str]) -> list[str]:
        available = [p for p in providers if self.allow_request(p)]
        return available if available else providers


circuit_breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=60)
