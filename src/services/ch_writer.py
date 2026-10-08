import asyncio

from src.core import keys
from src.redis_client import ensure_group
from src.services.base import BaseService


class ChWriter(BaseService):
    name = "ch-writer"
    uses_clickhouse = True
    
    async def setup(self) -> None:
        await super().setup()
        await ensure_group(self.redis, keys.RESULTS_STREAM, keys.WRITERS_GROUP)
        await asyncio.to_thread(self.db.init_schema)
    
    async def run(self) -> None:
        self.log.info(f"Idle (ClickHouse {self.db.get_version()})")
        while not self.stopping:
            await self.wait_or_stop(self.settings.ch_flush_interval_s)


if __name__ == "__main__":
    ChWriter().start()
