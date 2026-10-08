from src.services.base import BaseService


class MetricsPoller(BaseService):
    name = "metrics-poller"
    uses_clickhouse = True
    
    async def run(self) -> None:
        self.log.info(f"Idle (metrics_url={self.settings.metrics_url})")
        while not self.stopping:
            await self.wait_or_stop(self.settings.metrics_poll_interval_s)


if __name__ == "__main__":
    MetricsPoller().start()
