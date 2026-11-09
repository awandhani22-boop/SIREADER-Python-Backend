"""Server-side bearer authentication and bounded command rate limiting."""
from collections import defaultdict, deque
from hmac import compare_digest
from threading import Lock
from time import monotonic
from fastapi import Header, HTTPException, Request

class SlidingWindowRateLimiter:
    def __init__(self, *, limit: int = 30, window_seconds: float = 60.0) -> None:
        if limit < 1 or window_seconds <= 0:
            raise ValueError("rate limiter bounds must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def consume(self, actor_id: str, *, now: float | None = None) -> bool:
        stamp = monotonic() if now is None else now
        cutoff = stamp - self.window_seconds
        with self._lock:
            bucket = self._events[actor_id]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(stamp)
            return True

def verify_command_bearer(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    expected = getattr(request.app.state, "command_token", None)
    if not isinstance(expected, str) or not expected:
        raise HTTPException(status_code=503, detail="Command authentication is not configured.")
    prefix = "Bearer "
    if not isinstance(authorization, str) or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="A command bearer token is required.")
    supplied = authorization[len(prefix):]
    if not supplied or not compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Command authentication failed.")

def enforce_command_rate_limit(request: Request) -> None:
    limiter: SlidingWindowRateLimiter = request.app.state.rate_limiter
    if not limiter.consume("local_operator"):
        raise HTTPException(status_code=429, detail="Workforce command rate limit exceeded.")
