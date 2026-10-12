import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from src.models.job import Result, now_ms
from src.services.base import BaseService
from src.services.job_queue import JobQueue, consumer_name

REQUEST_COLUMNS = [
    "machine_id", "run_id", "step", "job_id", "attempt", "model", "function", "case_id", "split",
    "n_images", "n_videos", "media_bytes", "video_duration_s", "num_frames",
    "created_ts", "enqueued_ts", "dequeued_ts", "sent_ts", "first_token_ts", "done_ts", "inserted_ts",
    "prompt_tokens", "completion_tokens", "cached_tokens",
    "status", "http_status", "error_type", "json_parse_ok", "schema_valid",
]
RESPONSE_COLUMNS = [
    "run_id", "machine_id", "job_id", "attempt", "model", "function", "case_id", "split", "prompt_version",
    "raw_text", "parsed_json", "alert_valid", "plate_number", "labels", "severity", "inserted_ts",
]
RECLAIM_EVERY_S = 30
MAX_BACKOFF_S = 30


def ts(ms: Optional[int]) -> Optional[datetime]:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else None


def as_int(value: Optional[bool]) -> Optional[int]:
    return None if value is None else int(value)


class ChWriter(BaseService):
    """Batches lt:results into ClickHouse; acks only after the insert succeeded, so nothing is lost"""

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
                    await asyncio.to_thread(self.insert, buffer)
                except Exception as e:
                    self.log.warning(f"Insert of {len(buffer)} results failed, retrying in {backoff:.0f}s "
                                     f"(kept in Redis, not acked): {e}")
                    await self.wait_or_stop(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF_S)
                    if not self.db.ping():
                        await asyncio.to_thread(self.db.connect)
                    continue
                ids = [m for m, _ in buffer]
                await self.queue.ack_results(*ids)
                lag = now_ms() - min(r.done_ts or r.dequeued_ts or r.created_ts for _, r in buffer)
                self.log.info(f"Wrote {len(buffer)} results (oldest {lag / 1000:.1f}s old)")
                seen.difference_update(ids)
                buffer.clear()
                backoff = 1.0
                last_flush = time.monotonic()
            elif not buffer:
                last_flush = time.monotonic()

    def insert(self, buffer: List[Tuple[str, Result]]) -> None:
        # Same token for the same batch → a retried insert is dropped, in the rollup MV too
        token = hashlib.sha1(",".join(m for m, _ in buffer).encode()).hexdigest()
        settings = {"insert_deduplication_token": token, "deduplicate_blocks_in_dependent_materialized_views": 1}
        inserted = datetime.now(timezone.utc)
        requests, responses = [], []
        for _, r in buffer:
            requests.append([
                r.machine_id, r.run_id, r.step, r.job_id, r.attempt, r.model, r.function, r.case_id, r.split,
                r.n_images, r.n_videos, r.media_bytes, r.video_duration_s, r.num_frames,
                ts(r.created_ts), ts(r.enqueued_ts), ts(r.dequeued_ts), ts(r.sent_ts), ts(r.first_token_ts),
                ts(r.done_ts), inserted,
                r.prompt_tokens, r.completion_tokens, r.cached_tokens,
                r.status.value, r.http_status, r.error_type, as_int(r.json_parse_ok), as_int(r.schema_valid),
            ])
            if r.raw_text is not None:
                k = r.key_fields or {}
                responses.append([
                    r.run_id, r.machine_id, r.job_id, r.attempt, r.model, r.function, r.case_id, r.split,
                    r.prompt_version or "", r.raw_text, json.dumps(r.parsed_json) if r.parsed_json else "",
                    as_int(k.get("alert_valid")), k.get("plate_number"), k.get("labels") or [], k.get("severity"),
                    inserted,
                ])
        self.db.insert("lt_requests", requests, REQUEST_COLUMNS, settings)
        if responses:
            self.db.insert("lt_responses", responses, RESPONSE_COLUMNS,
                           {**settings, "insert_deduplication_token": token + ":responses"})


if __name__ == "__main__":
    ChWriter().start()
