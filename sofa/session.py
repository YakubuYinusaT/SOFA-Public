"""Per-call working memory. Redis in production, in-process dict for local runs.
Postgres is the record; this is scratch state keyed by provider session id (30 min expiry)."""

import json
import time

TTL_SECONDS = 30 * 60


class MemoryStore:
    def __init__(self):
        self._d: dict[str, tuple[float, str]] = {}

    def get(self, key: str) -> dict | None:
        item = self._d.get(key)
        if not item or item[0] < time.time():
            self._d.pop(key, None)
            return None
        return json.loads(item[1])

    def set(self, key: str, value: dict) -> None:
        self._d[key] = (time.time() + TTL_SECONDS, json.dumps(value))

    def delete(self, key: str) -> None:
        self._d.pop(key, None)


class RedisStore:
    def __init__(self, url: str):
        import redis  # pip install sofa[redis]

        self.r = redis.Redis.from_url(url, decode_responses=True)

    def get(self, key: str) -> dict | None:
        raw = self.r.get(f"sofa:session:{key}")
        return json.loads(raw) if raw else None

    def set(self, key: str, value: dict) -> None:
        self.r.setex(f"sofa:session:{key}", TTL_SECONDS, json.dumps(value))

    def delete(self, key: str) -> None:
        self.r.delete(f"sofa:session:{key}")


def make_store(redis_url: str):
    return RedisStore(redis_url) if redis_url else MemoryStore()
