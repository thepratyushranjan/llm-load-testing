from src.services.base import BaseService


class Scheduler(BaseService):
    name = "scheduler"
    
    async def run(self) -> None:
        self.log.info(f"Idle (tick={self.settings.tick_seconds}s, profile={self.settings.profile})")
        while not self.stopping:
            await self.wait_or_stop(self.settings.tick_seconds)


if __name__ == "__main__":
    Scheduler().start()
