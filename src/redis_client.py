import asyncio
import logging

import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

from src.core.config import get_settings

log = logging.getLogger(__name__)


async def connect_redis(attempts: int = 30, delay_s: float = 1.0) -> redis.Redis:
    """Connect to Redis, retrying while the container starts"""
    client = redis.from_url(get_settings().redis_url, decode_responses=True)
    for i in range(1, attempts + 1):
        try:
            await client.ping()
            return client
        except (RedisConnectionError, OSError) as e:
            if i == attempts:
                raise
            log.warning(f"Redis not ready (attempt {i}): {e}")
            await asyncio.sleep(delay_s)
    raise AssertionError("unreachable")


async def ensure_group(client: redis.Redis, stream: str, group: str) -> None:
    """Create the consumer group (and stream) if missing; safe from every replica"""
    try:
        await client.xgroup_create(stream, group, id="0", mkstream=True)
    except ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise
