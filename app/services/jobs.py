"""Background job queue. Runs in-process on a thread pool with retries.
(Spec uses a Redis queue; this keeps the same job names so it can be swapped for RQ/Celery later.)"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("dumbbel.jobs")
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="job")


def enqueue(name: str, fn, *args, retries: int = 0, backoff: float = 2.0, **kwargs):
    def run():
        for attempt in range(retries + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                log.warning("job %s failed (attempt %s/%s): %s", name, attempt + 1, retries + 1, e)
                if attempt < retries:
                    time.sleep(backoff * (attempt + 1))
        log.error("job %s gave up", name)
    return _pool.submit(run)
