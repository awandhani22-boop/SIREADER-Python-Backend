"""Server-side bearer authentication and bounded command rate limiting."""
from collections import defaultdict, deque
from hmac import compare_digest
from threading import Lock
from time import monotonic
import json
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



class RequestBodyLimitMiddleware:
    """Buffer only bounded command bodies; reject oversized/chunked bodies before JSON parsing."""

    def __init__(self, app, *, max_bytes: int = 20 * 1024) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        declared_length = headers.get(b"content-length")
        if declared_length is not None:
            try:
                if int(declared_length) > self.max_bytes:
                    await self._reject(send)
                    return
            except ValueError:
                await self._reject(send)
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await self._reject(send)
                return
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)

    async def _reject(self, send) -> None:
        payload = json.dumps({"detail": "COMMAND_BODY_TOO_LARGE"}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": payload})
