import asyncio
import signal
import socket
from typing import Optional

import redis.asyncio as redis

from src.core import keys
from src.core.config import Settings, get_settings
from src.core.logging import setup_logging
from src.database import db
from src.redis_client import connect_redis

HEARTBEAT_EVERY_S = 5
HEARTBEAT_TTL_S = 15


class BaseService:
    """Base class for scheduler, worker, ch-writer and metrics-poller"""

    name: str = "service"
    uses_clickhouse: bool = False

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.instance = socket.gethostname()
        self.log = setup_logging(self.name, self.settings.machine_id, self.settings.log_level)
        self.redis: Optional[redis.Redis] = None
        self.db = db
        self._stop = asyncio.Event()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    async def wait_or_stop(self, seconds: float) -> None:
        """Sleep, but wake immediately on shutdown"""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def setup(self) -> None:
        """Override for service-specific startup (call super first)"""
        self.redis = await connect_redis()
        if self.uses_clickhouse:
            while not await asyncio.to_thread(self.db.connect) or not self.db.ping():
                self.log.warning("ClickHouse not ready, retrying")
                await asyncio.sleep(1)

    async def run(self) -> None:
        raise NotImplementedError

    async def teardown(self) -> None:
        if self.uses_clickhouse:
            self.db.disconnect()
        if self.redis is not None:
            await self.redis.aclose()

    async def _heartbeat(self) -> None:
        key = keys.heartbeat_key(self.name, self.instance)
        while not self.stopping:
            try:
                await self.redis.set(key, self.settings.machine_id, ex=HEARTBEAT_TTL_S)
            except Exception as e:
                self.log.warning(f"Heartbeat failed: {e}")
            await self.wait_or_stop(HEARTBEAT_EVERY_S)
        await self.redis.delete(key)

    async def _main(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.request_stop)

        await self.setup()
        self.log.info(f"Started on {self.instance}")
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            await self.run()
        finally:
            self.request_stop()
            await heartbeat
            await self.teardown()
            self.log.info("Stopped")

    def start(self) -> None:
        asyncio.run(self._main())
