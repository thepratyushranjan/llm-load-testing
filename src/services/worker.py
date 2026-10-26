import asyncio
import time
from typing import Dict, Optional, Set

from src.core import keys, prompts
from src.models.job import Job, Result, Status, now_ms
from src.services.base import BaseService
from src.services.job_queue import ControlWatcher, Delivery, JobQueue, consumer_name
from src.services.tasks import TASKS, get_task
from src.services.vllm_client import CallResult, VllmClient

RETRYABLE = (Status.RATE_LIMITED, Status.ERROR, Status.TIMEOUT)
MAINTENANCE_EVERY_S = 1.0
RECLAIM_EVERY_S = 5.0
STATS_EVERY_S = 10.0


class Worker(BaseService):
    """expiry → idempotency → rate limit → build request → vLLM → parse → result → ack"""

    name = "worker"

    async def setup(self) -> None:
        await super().setup()
        self.queue = JobQueue(self.redis, self.settings)
        await self.queue.setup()
        self.client = VllmClient(self.settings)
        self.tasks = {fn: get_task(fn, self.settings) for fn in TASKS}
        self.watcher = ControlWatcher(self.queue)
        self.consumer = consumer_name()
        self.inflight: Set[asyncio.Task] = set()
        self.done = 0
        self.loop_lag_ms = 0.0

    async def teardown(self) -> None:
        await self.cancel_inflight()
        await self.client.close()
        await super().teardown()

    async def run(self) -> None:
        self.log.info(f"Ready: consumer={self.consumer} concurrency={self.settings.worker_concurrency} "
                      f"vllm={self.settings.vllm_url} model={self.settings.model}")
        background = [asyncio.create_task(t) for t in (self.watcher.watch(), self.maintenance(), self.stats_loop())]
        try:
            await self.consume_loop()
        finally:
            for t in background:
                t.cancel()

    async def consume_loop(self) -> None:
        limit = self.settings.worker_concurrency
        while not self.stopping:
            if self.watcher.stopped.is_set():
                if self.inflight:
                    self.log.warning(f"Kill switch: cancelling {len(self.inflight)} in-flight requests")
                    await self.cancel_inflight()
                await asyncio.sleep(0.2)
                continue
            if not self.watcher.running.is_set():  # paused: let in-flight finish, take nothing new
                await asyncio.sleep(0.2)
                continue
            free = limit - len(self.inflight)
            if free <= 0:
                await asyncio.wait(self.inflight, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
                continue
            try:
                deliveries = await self.queue.consume(self.consumer, count=min(free, 32), block_ms=500)
            except Exception as e:  # e.g. Redis restarting or the group recreated: retry, don't crash
                self.log.warning(f"Reading jobs failed: {e}")
                await asyncio.sleep(1)
                await self.queue.setup()
                continue
            for delivery in deliveries:
                self.spawn(delivery)

    def spawn(self, delivery: Delivery) -> None:
        task = asyncio.create_task(self.process(delivery))
        self.inflight.add(task)
        task.add_done_callback(self.inflight.discard)

    async def cancel_inflight(self) -> None:
        for t in list(self.inflight):
            t.cancel()
        if self.inflight:
            await asyncio.gather(*self.inflight, return_exceptions=True)

    async def maintenance(self) -> None:
        """Promote due retries; take over jobs from crashed workers"""
        last_reclaim = 0.0
        while True:
            await asyncio.sleep(MAINTENANCE_EVERY_S)
            if not self.watcher.running.is_set():
                continue
            try:
                await self.queue.promote_due()
                if time.monotonic() - last_reclaim >= RECLAIM_EVERY_S:
                    last_reclaim = time.monotonic()
                    retry, dead = await self.queue.reclaim(self.consumer)
                    for d in retry:
                        self.log.warning(f"Reclaimed job {d.job.job_id} (delivery {d.deliveries})")
                        self.spawn(d)
                    for d in dead:
                        await self.publish(self.base_result(d, now_ms()), status=Status.DLQ,
                                           error_type="max_deliveries", attempt=d.job.attempt + 1)
            except Exception as e:
                self.log.warning(f"Maintenance failed: {e}")

    async def stats_loop(self) -> None:
        """Log the worker's own CPU and event-loop lag so it's visible if the worker becomes the bottleneck"""
        cpu, wall = time.process_time(), time.monotonic()
        while True:
            t = time.monotonic()
            await asyncio.sleep(STATS_EVERY_S)
            self.loop_lag_ms = max(0.0, (time.monotonic() - t - STATS_EVERY_S) * 1000)
            cpu_pct = (time.process_time() - cpu) / (time.monotonic() - wall) * 100
            cpu, wall = time.process_time(), time.monotonic()
            stats = {"cpu_pct": round(cpu_pct, 1), "inflight": len(self.inflight), "done": self.done,
                     "loop_lag_ms": round(self.loop_lag_ms, 1), "ts": now_ms()}
            await self.redis.hset(keys.worker_stats_key(self.instance), mapping=stats)
            await self.redis.expire(keys.worker_stats_key(self.instance), 30)
            if self.inflight or cpu_pct > 1:
                self.log.info(f"cpu={cpu_pct:.0f}% inflight={len(self.inflight)} done={self.done} "
                              f"loop_lag={self.loop_lag_ms:.0f}ms")
            if cpu_pct > 80:
                self.log.warning("Worker CPU > 80%: add worker replicas or the worker becomes the bottleneck")

    # ── one job ──────────────────────────────────────────────────────────

    def base_result(self, d: Delivery, dequeued_ts: int) -> Result:
        job = d.job
        videos = [m for m in job.media if m.kind == "video"]
        return Result(
            machine_id=job.machine_id, run_id=job.run_id, step=job.step, job_id=job.job_id, attempt=job.attempt,
            model=self.settings.model, function=job.function, case_id=job.case_id, split=job.split,
            n_images=sum(m.kind == "image" for m in job.media), n_videos=len(videos),
            media_bytes=sum(m.bytes for m in job.media),
            video_duration_s=sum(m.duration_s or 0 for m in videos) or None,
            created_ts=job.created_ts, enqueued_ts=d.enqueued_ts or None, dequeued_ts=dequeued_ts,
            status=Status.ERROR,
        )

    async def publish(self, result: Result, status: Status, error_type: Optional[str] = None,
                      attempt: Optional[int] = None) -> None:
        update = {"status": status, "error_type": error_type}
        if attempt is not None:
            update["attempt"] = attempt
        await self.queue.publish_result(result.model_copy(update=update))

    async def process(self, d: Delivery) -> None:
        dequeued = now_ms()
        job = d.job
        result = self.base_result(d, dequeued)
        try:
            if job.expired(dequeued):
                await self.publish(result, Status.EXPIRED, "expired_in_queue")
                await self.queue.ack(d.msg_id)
                return
            if await self.queue.already_done(job):  # redelivered after the result was already recorded
                await self.queue.ack(d.msg_id)
                return

            await self.queue.acquire_rate()
            task = self.tasks[job.function]
            body = await asyncio.to_thread(task.build_request, job, self.settings.model)
            call = await self.client.chat(body, deadline_ts=job.deadline_ts)
            result = self.fill(result, call)
            if call.status == Status.OK:
                parsed = await asyncio.to_thread(task.parse, call.text, job)
                parse_error = None
                if not parsed.json_parse_ok:
                    parse_error = "json_invalid"
                elif not parsed.schema_valid:
                    parse_error = "format_error" if (parsed.error or "").startswith("format") else "schema_invalid"
                result = result.model_copy(update={
                    "json_parse_ok": parsed.json_parse_ok, "schema_valid": parsed.schema_valid,
                    "record": parsed.record or None,
                    "prompt_version": prompts.PROMPT_VERSION,
                    "error_type": call.error_type or parse_error,
                })
            await self.queue.publish_result(result)
            self.done += 1

            if call.status in RETRYABLE:
                retried = await self.queue.retry_or_dlq(d, error=f"{call.error_type}: {call.error_message or ''}",
                                                        retry_after_s=call.retry_after_s)
                if not retried:
                    await self.publish(self.base_result(d, now_ms()), Status.DLQ, call.error_type,
                                       attempt=job.attempt + 1)
            else:
                await self.queue.ack(d.msg_id)
        except asyncio.CancelledError:
            raise  # kill switch / shutdown: left unacked (purged or reclaimed later)
        except Exception as e:
            self.log.exception(f"Job {job.job_id} failed in the worker: {e}")
            await self.publish(result, Status.ERROR, f"worker_{type(e).__name__}")
            if not await self.queue.retry_or_dlq(d, error=str(e)):
                await self.publish(self.base_result(d, now_ms()), Status.DLQ, f"worker_{type(e).__name__}",
                                   attempt=job.attempt + 1)

    @staticmethod
    def fill(result: Result, call: CallResult) -> Result:
        return result.model_copy(update={
            "sent_ts": call.sent_ts, "first_token_ts": call.first_token_ts, "done_ts": call.done_ts,
            "prompt_tokens": call.prompt_tokens, "completion_tokens": call.completion_tokens,
            "cached_tokens": call.cached_tokens, "status": call.status, "http_status": call.http_status,
            "error_type": call.error_type,
        })


if __name__ == "__main__":
    Worker().start()
