import asyncio
import hashlib
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from src.models.job import Result, now_ms
from src.models.tables import RESPONSE_TABLES, SPEED_COLUMNS
from src.services.base import BaseService
from src.services.job_queue import JobQueue, consumer_name

RESPONSE_COLUMNS = ["run_id", "machine_id", "job_id", "attempt", "model", "case_id", "split", "prompt_version",
                    "inserted_ts"] + [c for c, _ in SPEED_COLUMNS]
RECLAIM_EVERY_S = 30
MAX_BACKOFF_S = 30


def ts(ms: Optional[int]) -> Optional[datetime]:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else None


def span(start: Optional[int], end: Optional[int]) -> Optional[int]:
    return end - start if start and end else None


class ChWriter(BaseService):
    """Writes valid responses from lt:results into the three response tables; acks only after the insert succeeded"""

    name = "ch-writer"
    uses_clickhouse = True

    async def setup(self) -> None:
        await super().setup()
        await asyncio.to_thread(self.db.init_schema)
        self.queue = JobQueue(self.redis, self.settings)
        await self.queue.setup()
        self.consumer = consumer_name()

    async def run(self) -> None:
        self.log.info(f"Writing results to ClickHouse every {self.settings.ch_flush_interval_s}s "
                      f"(batch ≤ {self.settings.ch_batch_max})")
        # results this writer read but never acked before a restart come first
        buffer: List[Tuple[str, Result]] = await self.queue.read_results(
            self.consumer, count=self.settings.ch_batch_max, pending=True)
        if buffer:
            self.log.info(f"Recovered {len(buffer)} unwritten results from before the restart")
        seen = {m for m, _ in buffer}
        last_flush = time.monotonic()
        last_reclaim = 0.0
        backoff = 1.0
        while not self.stopping:
            room = self.settings.ch_batch_max - len(buffer)
            if room > 0:
                batch = await self.queue.read_results(self.consumer, count=room, block_ms=500)
                if time.monotonic() - last_reclaim >= RECLAIM_EVERY_S:
                    last_reclaim = time.monotonic()
                    batch += await self.queue.reclaim_results(self.consumer)
                for msg_id, result in batch:
                    if msg_id not in seen:  # pending entries are re-read until acked
                        seen.add(msg_id)
                        buffer.append((msg_id, result))
            due = time.monotonic() - last_flush >= self.settings.ch_flush_interval_s
            if buffer and (due or len(buffer) >= self.settings.ch_batch_max):
                try:
                    stored = await asyncio.to_thread(self.insert, buffer)
                except Exception as e:
                    self.log.warning(f"Insert of {len(buffer)} results failed, retrying in {backoff:.0f}s "
                                     f"(kept in Redis, not acked): {e}")
                    await self.wait_or_stop(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF_S)
                    if not self.db.ping():
                        await asyncio.to_thread(self.db.connect)
                    elif "UNKNOWN_TABLE" in str(e):  # tables dropped while running: create them again
                        await asyncio.to_thread(self.db.init_schema)
                    continue
                ids = [m for m, _ in buffer]
                await self.queue.ack_results(*ids)
                lag = now_ms() - min(r.done_ts or r.dequeued_ts or r.created_ts for _, r in buffer)
                self.log.info(f"Stored {stored} valid responses of {len(buffer)} results (oldest {lag / 1000:.1f}s old)")
                seen.difference_update(ids)
                buffer.clear()
                backoff = 1.0
                last_flush = time.monotonic()
            elif not buffer:
                last_flush = time.monotonic()

    def insert(self, buffer: List[Tuple[str, Result]]) -> int:
        """Valid responses → their function's table; other results are only counted (Redis live stats)"""
        token = hashlib.sha1(",".join(m for m, _ in buffer).encode()).hexdigest()  # a retried batch is dropped
        inserted = datetime.now(timezone.utc)
        responses = {}
        for _, r in buffer:
            if not r.record:
                continue
            row = [r.run_id, r.machine_id, r.job_id, r.attempt, r.model, r.case_id, r.split, r.prompt_version or "",
                   inserted, ts(r.created_ts), r.step, span(r.sent_ts, r.first_token_ts), span(r.sent_ts, r.done_ts),
                   span(r.created_ts, r.done_ts), r.prompt_tokens, r.completion_tokens, r.n_images]
            responses.setdefault(RESPONSE_TABLES[r.function], []).append((row, r.record))
        for table, rows in responses.items():
            columns = list(rows[0][1])
            self.db.insert(table, [row + [record[c] for c in columns] for row, record in rows],
                           RESPONSE_COLUMNS + columns, {"insert_deduplication_token": f"{token}:{table}"})
        return sum(len(rows) for rows in responses.values())


if __name__ == "__main__":
    ChWriter().start()
