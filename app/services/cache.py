"""Redis when REDIS_URL is set, otherwise a thread-safe in-process TTL store.
Used for OTP rate limits, phone locks, QR replay (jti) cache and the general API rate limit."""
import threading
import time

from app.config import settings
from app.errors import ApiError


class _MemoryCache:
    def __init__(self):
        self._d: dict[str, tuple[object, float]] = {}
        self._lock = threading.Lock()

    def _alive(self, k):
        v = self._d.get(k)
        if v and v[1] and v[1] < time.time():
            self._d.pop(k, None)
            return None
        return v

    def get(self, k):
        with self._lock:
            v = self._alive(k)
            return v[0] if v else None

    def set(self, k, val, ex: int | None = None, nx: bool = False) -> bool:
        with self._lock:
            if nx and self._alive(k):
                return False
            self._d[k] = (val, time.time() + ex if ex else 0)
            return True

    def incr(self, k, ex: int) -> int:
        with self._lock:
            v = self._alive(k)
            n = (int(v[0]) if v else 0) + 1
            self._d[k] = (n, v[1] if v else time.time() + ex)
            return n

    def ttl(self, k) -> int:
        with self._lock:
            v = self._alive(k)
            if not v:
                return -2
            return int(v[1] - time.time()) if v[1] else -1

    def delete(self, k):
        with self._lock:
            self._d.pop(k, None)


class _RedisCache:
    def __init__(self, url):
        import redis
        self.r = redis.Redis.from_url(url, decode_responses=True)

    def get(self, k):
        return self.r.get(k)

    def set(self, k, val, ex=None, nx=False) -> bool:
        return bool(self.r.set(k, val, ex=ex, nx=nx))

    def incr(self, k, ex: int) -> int:
        p = self.r.pipeline()
        p.incr(k)
        p.expire(k, ex, nx=True)
        return int(p.execute()[0])

    def ttl(self, k) -> int:
        return int(self.r.ttl(k))

    def delete(self, k):
        self.r.delete(k)


cache = _RedisCache(settings.redis_url) if settings.redis_url else _MemoryCache()


def rate_limit(key: str, limit: int, window_sec: int, code: str = "RATE_LIMITED", status: int = 429):
    """Fixed-window counter. Raises 429 with retry_after_sec when over the limit."""
    n = cache.incr(f"rl:{key}:{window_sec}", window_sec)
    if n > limit:
        ttl = cache.ttl(f"rl:{key}:{window_sec}")
        raise ApiError(status, code, None, {"retry_after_sec": max(1, ttl if ttl > 0 else window_sec)})
