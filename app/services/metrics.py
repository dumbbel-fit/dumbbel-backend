"""In-process request meter for the executive console's Capacity screen.

Keeps the last 24 hours as one bucket per minute (count, errors, total time, slow requests), the number of
requests in flight right now and the highest it has been, a rolling sample of response times, and totals per
route. It lives in memory: it starts empty on every deploy/restart and describes this instance only.
Nothing about the caller is kept: no user, phone, IP or query string."""
import threading
import time
from collections import deque

MINUTES = 24 * 60
SLOW_MS = 1000
_lock = threading.Lock()
_started = time.time()
_buckets: dict[int, list] = {}          # minute -> [count, errors, total_ms, slow]
_samples: deque = deque(maxlen=3000)    # latest response times, ms
_routes: dict[str, list] = {}           # "GET /v1/..." -> [count, total_ms, max_ms, errors]
_inflight = 0
_peak_inflight = 0
_peak_at = 0.0
_total = 0


def begin() -> None:
    global _inflight, _peak_inflight, _peak_at
    with _lock:
        _inflight += 1
        if _inflight > _peak_inflight:
            _peak_inflight, _peak_at = _inflight, time.time()


def end(route: str, status: int, ms: float) -> None:
    global _inflight, _total
    now = int(time.time() // 60)
    with _lock:
        _inflight = max(0, _inflight - 1)
        _total += 1
        b = _buckets.get(now)
        if b is None:
            b = _buckets[now] = [0, 0, 0.0, 0]
            if len(_buckets) > MINUTES + 5:
                for k in [k for k in _buckets if k < now - MINUTES]:
                    del _buckets[k]
        b[0] += 1
        b[2] += ms
        if status >= 500:
            b[1] += 1
        if ms >= SLOW_MS:
            b[3] += 1
        _samples.append(ms)
        r = _routes.get(route)
        if r is None:
            if len(_routes) >= 400:      # unknown paths (scanners) must not grow this without limit
                return
            r = _routes[route] = [0, 0.0, 0.0, 0]
        r[0] += 1
        r[1] += ms
        r[2] = max(r[2], ms)
        if status >= 500:
            r[3] += 1


def _pctl(sorted_vals: list, p: float):
    if not sorted_vals:
        return None
    return round(sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * p))], 1)


def snapshot() -> dict:
    now = int(time.time() // 60)
    with _lock:
        buckets = {k: list(v) for k, v in _buckets.items() if k >= now - MINUTES}
        samples = sorted(_samples)
        routes = {k: list(v) for k, v in _routes.items()}
        inflight, peak, peak_at, total = _inflight, _peak_inflight, _peak_at, _total
    count = sum(b[0] for b in buckets.values())
    errors = sum(b[1] for b in buckets.values())
    slow = sum(b[3] for b in buckets.values())
    total_ms = sum(b[2] for b in buckets.values())
    busiest = max(buckets.items(), key=lambda kv: kv[1][0], default=None)
    hours = []
    for i in range(23, -1, -1):          # 24 hourly points, oldest first
        lo, hi = now - (i + 1) * 60, now - i * 60
        hb = [v for k, v in buckets.items() if lo < k <= hi]
        c = sum(b[0] for b in hb)
        hours.append({"t": hi * 60, "requests": c, "peak_per_min": max((b[0] for b in hb), default=0),
                      "avg_ms": round(sum(b[2] for b in hb) / c, 1) if c else None, "errors": sum(b[1] for b in hb)})
    top = sorted(routes.items(), key=lambda kv: kv[1][1], reverse=True)[:12]
    return {
        "since": _started, "uptime_sec": int(time.time() - _started), "requests_total": total,
        "requests_24h": count, "errors_24h": errors, "slow_24h": slow,
        "avg_ms": round(total_ms / count, 1) if count else None,
        "p50_ms": _pctl(samples, 0.50), "p95_ms": _pctl(samples, 0.95), "p99_ms": _pctl(samples, 0.99),
        "sampled": len(samples),
        "last_min": buckets.get(now - 1, [0])[0],
        "peak_per_min": busiest[1][0] if busiest else 0, "peak_minute_at": busiest[0] * 60 if busiest else None,
        "inflight": inflight, "peak_inflight": peak, "peak_inflight_at": peak_at or None,
        "hours": hours,
        "routes": [{"route": k, "requests": v[0], "avg_ms": round(v[1] / v[0], 1), "max_ms": round(v[2], 1),
                    "errors": v[3], "time_share_pct": round(v[1] / total_ms * 100, 1) if total_ms else None}
                   for k, v in top if v[0]],
    }
