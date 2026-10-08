from src.core import keys
from src.redis_client import ensure_group
from src.services.base import BaseService


class Worker(BaseService):
    name = "worker"
    
    async def setup(self) -> None:
        await super().setup()
        await ensure_group(self.redis, keys.JOBS_STREAM, keys.WORKERS_GROUP)
    
    async def run(self) -> None:
        self.log.info(f"Idle (concurrency={self.settings.worker_concurrency}, vllm={self.settings.vllm_url})")
        while not self.stopping:
            await self.wait_or_stop(5)


if __name__ == "__main__":
    Worker().start()
