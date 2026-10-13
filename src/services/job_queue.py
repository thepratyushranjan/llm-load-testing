import argparse
import asyncio
import json
import os
import socket
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import Dict, List, Optional, Tuple

import redis.asyncio as redis

from src.core import keys
from src.core.config import Settings, get_settings
from src.models.job import Job, Result, now_ms
from src.redis_client import connect_redis, ensure_group
from src.services import run_stats

# Token bucket shared by every worker on this machine. Returns 0 if a token was taken,
# otherwise the ms to wait before trying again. Uses Redis time so all callers agree.
TOKEN_BUCKET = """
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1]) or burst
local ts = tonumber(state[2]) or now
tokens = math.min(burst, tokens + (now - ts) * rate / 1000)
local wait = 0
if tokens >= 1 then
  tokens = tokens - 1
else
  wait = math.ceil((1 - tokens) * 1000 / rate)
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], 60000)
return wait
"""

# Publish a result once per job_id:attempt, however often the attempt is redelivered.
PUBLISH_ONCE = """
if redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[2]) then
  redis.call('XADD', KEYS[2], '*', 'result', ARGV[1])
  redis.call('RPUSH', KEYS[3], ARGV[3])
  redis.call('EXPIRE', KEYS[3], ARGV[4])
  return 1
end
return 0
"""

# Move retries whose time has come from the delayed set back onto the job stream.
PROMOTE_DUE = """
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
local moved = 0
for _, job in ipairs(due) do
  if redis.call('ZREM', KEYS[1], job) == 1 then
    redis.call('XADD', KEYS[2], '*', 'job', job, 'enqueued_ts', ARGV[1])
    moved = moved + 1
  end
end
return moved
"""


class ControlState(StrEnum):
    RUN = "run"
    PAUSE = "pause"
    STOP = "stop"


@dataclass
class Delivery:
    """One job handed to a worker"""
    msg_id: str
    job: Job
    enqueued_ts: int
    deliveries: int = 1  # >1 means it was reclaimed from a crashed worker


def consumer_name() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


class JobQueue:
    """Redis Streams job / result queues with crash recovery, retries, DLQ, idempotency and rate limiting"""

    def __init__(self, client: redis.Redis, settings: Optional[Settings] = None):
        self.redis = client
        self.settings = settings or get_settings()
        self._token_bucket = client.register_script(TOKEN_BUCKET)
        self._publish_once = client.register_script(PUBLISH_ONCE)
        self._promote_due = client.register_script(PROMOTE_DUE)

    @property
    def claim_idle_ms(self) -> int:
        idle_s = self.settings.claim_idle_s or 2 * self.settings.request_timeout_s
        return int(idle_s * 1000)

    async def setup(self) -> None:
        await ensure_group(self.redis, keys.JOBS_STREAM, keys.WORKERS_GROUP)
        await ensure_group(self.redis, keys.RESULTS_STREAM, keys.WRITERS_GROUP)

    # ── jobs: scheduler side ─────────────────────────────────────────────

    async def enqueue(self, jobs: List[Job]) -> None:
        now = str(now_ms())
        async with self.redis.pipeline(transaction=False) as pipe:
            for job in jobs:
                pipe.xadd(keys.JOBS_STREAM, {**job.to_stream(), "enqueued_ts": now})
            await pipe.execute()

    # ── jobs: worker side ────────────────────────────────────────────────

    @staticmethod
    def _delivery(msg_id: str, fields: Dict, deliveries: int = 1) -> Delivery:
        return Delivery(msg_id=msg_id, job=Job.from_stream(fields),
                        enqueued_ts=int(fields.get("enqueued_ts", 0)), deliveries=deliveries)

    async def consume(self, consumer: str, count: int, block_ms: int = 1000) -> List[Delivery]:
        """New jobs for this consumer (blocks up to block_ms when the stream is empty)"""
        reply = await self.redis.xreadgroup(keys.WORKERS_GROUP, consumer, {keys.JOBS_STREAM: ">"},
                                            count=count, block=block_ms)
        return [self._delivery(msg_id, fields) for _, messages in reply or [] for msg_id, fields in messages]

    async def reclaim(self, consumer: str, count: int = 100) -> Tuple[List[Delivery], List[Delivery]]:
        """Take over jobs a crashed worker left unacked. Returns (to_process, sent_to_dlq)"""
        _, claimed, deleted = await self.redis.xautoclaim(
            keys.JOBS_STREAM, keys.WORKERS_GROUP, consumer, min_idle_time=self.claim_idle_ms,
            start_id="0-0", count=count)
        if deleted:  # entries trimmed while pending: nothing left to process
            await self.redis.xack(keys.JOBS_STREAM, keys.WORKERS_GROUP, *deleted)
        if not claimed:
            return [], []

        ids = [msg_id for msg_id, _ in claimed]
        pending = await self.redis.xpending_range(keys.JOBS_STREAM, keys.WORKERS_GROUP,
                                                  min=min(ids), max=max(ids), count=len(ids) + 1000,
                                                  consumername=consumer)
        times = {p["message_id"]: p["times_delivered"] for p in pending}

        retry, dead = [], []
        for msg_id, fields in claimed:
            if not fields:
                await self.ack(msg_id)
                continue
            delivery = self._delivery(msg_id, fields, deliveries=times.get(msg_id, 2))
            if delivery.deliveries > self.settings.max_deliveries:
                await self.to_dlq(delivery, reason="max_deliveries",
                                  error=f"handed out {delivery.deliveries} times without being acked")
                dead.append(delivery)
            else:
                retry.append(delivery)
        return retry, dead

    async def ack(self, *msg_ids: str) -> None:
        """Done with these jobs: ack and delete so the stream doesn't grow"""
        if msg_ids:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.xack(keys.JOBS_STREAM, keys.WORKERS_GROUP, *msg_ids)
                pipe.xdel(keys.JOBS_STREAM, *msg_ids)
                await pipe.execute()

    def retry_delay_s(self, job: Job, retry_after_s: Optional[float] = None) -> float:
        if retry_after_s is not None:
            return retry_after_s
        return self.settings.retry_backoff_s * (2 ** job.attempt)

    async def retry_or_dlq(self, delivery: Delivery, error: str, retry_after_s: Optional[float] = None) -> bool:
        """Schedule the next attempt, or DLQ when out of retries / past the deadline. True if retried"""
        job = delivery.job
        due = now_ms() + int(self.retry_delay_s(job, retry_after_s) * 1000)
        if job.attempt >= self.settings.max_retries:
            await self.to_dlq(delivery, reason="max_retries", error=error)
            return False
        if due >= job.deadline_ts:
            await self.to_dlq(delivery, reason="deadline", error=error)
            return False
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zadd(keys.DELAYED_KEY, {job.next_attempt().model_dump_json(): due})
            pipe.xack(keys.JOBS_STREAM, keys.WORKERS_GROUP, delivery.msg_id)
            pipe.xdel(keys.JOBS_STREAM, delivery.msg_id)
            await pipe.execute()
        return True

    async def promote_due(self, limit: int = 500) -> int:
        """Put retries that are due back on the job stream (safe to call from every worker)"""
        return await self._promote_due(keys=[keys.DELAYED_KEY, keys.JOBS_STREAM], args=[now_ms(), limit])

    async def to_dlq(self, delivery: Delivery, reason: str, error: str = "") -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xadd(keys.DLQ_STREAM, {"job": delivery.job.model_dump_json(), "reason": reason,
                                        "error": error[:1000], "deliveries": delivery.deliveries,
                                        "ts": now_ms()})
            pipe.xack(keys.JOBS_STREAM, keys.WORKERS_GROUP, delivery.msg_id)
            pipe.xdel(keys.JOBS_STREAM, delivery.msg_id)
            await pipe.execute()

    # ── idempotency + results ────────────────────────────────────────────

    async def already_done(self, job: Job) -> bool:
        """A result was already published for this job attempt (e.g. redelivered after a crash)"""
        return bool(await self.redis.exists(keys.idempotency_key(job.job_id, job.attempt)))

    async def publish_result(self, result: Result) -> bool:
        """Publish once per job_id:attempt (+ its live-stats entry). False if this attempt was already recorded"""
        added = await self._publish_once(
            keys=[keys.idempotency_key(result.job_id, result.attempt), keys.RESULTS_STREAM,
                  keys.run_stats_key(result.run_id)],
            args=[result.model_dump_json(), self.settings.idempotency_ttl_s, run_stats.entry(result),
                  run_stats.STATS_TTL_S])
        return bool(added)

    async def read_results(self, consumer: str, count: int, block_ms: int = 1000,
                           pending: bool = False) -> List[Tuple[str, Result]]:
        """Results for the ClickHouse writer. pending=True re-reads its own unacked ones (after a restart)"""
        reply = await self.redis.xreadgroup(keys.WRITERS_GROUP, consumer,
                                            {keys.RESULTS_STREAM: "0" if pending else ">"},
                                            count=count, block=None if pending else block_ms)
        return [(msg_id, Result.from_stream(fields))
                for _, messages in reply or [] for msg_id, fields in messages if fields]

    async def reclaim_results(self, consumer: str, min_idle_ms: int = 60000, count: int = 1000) -> List[Tuple[str, Result]]:
        """Results a crashed writer left unacked"""
        _, claimed, _ = await self.redis.xautoclaim(keys.RESULTS_STREAM, keys.WRITERS_GROUP, consumer,
                                                    min_idle_time=min_idle_ms, start_id="0-0", count=count)
        return [(msg_id, Result.from_stream(fields)) for msg_id, fields in claimed if fields]

    async def ack_results(self, *msg_ids: str) -> None:
        if msg_ids:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.xack(keys.RESULTS_STREAM, keys.WRITERS_GROUP, *msg_ids)
                pipe.xdel(keys.RESULTS_STREAM, *msg_ids)
                await pipe.execute()

    # ── rate limit ───────────────────────────────────────────────────────

    async def acquire_rate(self) -> int:
        """Wait for a send token; returns ms waited (0 when no cap)"""
        rate = self.settings.max_rps
        if rate <= 0:
            return 0
        burst = self.settings.rate_burst or max(1, int(rate))
        started = now_ms()
        while True:
            wait_ms = await self._token_bucket(keys=[keys.RATE_LIMIT_KEY], args=[rate, burst])
            if not wait_ms:
                return now_ms() - started
            await asyncio.sleep(wait_ms / 1000)

    # ── control / status ─────────────────────────────────────────────────

    async def set_control(self, state: ControlState) -> None:
        await self.redis.set(keys.CONTROL_KEY, state.value)

    async def get_control(self) -> ControlState:
        value = await self.redis.get(keys.CONTROL_KEY)
        return ControlState(value) if value else ControlState.RUN

    async def purge_jobs(self) -> int:
        """Drop every queued, pending and delayed job (results are kept)"""
        dropped = await self.redis.xlen(keys.JOBS_STREAM) + await self.redis.zcard(keys.DELAYED_KEY)
        # one transaction, so a worker reading meanwhile never sees the group missing
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(keys.JOBS_STREAM, keys.DELAYED_KEY)
            pipe.xgroup_create(keys.JOBS_STREAM, keys.WORKERS_GROUP, id="0", mkstream=True)
            await pipe.execute()
        return dropped

    async def stats(self) -> Dict:
        """Queue depth etc. — depth growing during a load step means that step is beyond capacity"""
        groups = {}
        for stream, group in ((keys.JOBS_STREAM, keys.WORKERS_GROUP), (keys.RESULTS_STREAM, keys.WRITERS_GROUP)):
            info = next((g for g in await self.redis.xinfo_groups(stream) if g["name"] == group), {}) \
                if await self.redis.exists(stream) else {}
            groups[stream] = info
        jobs, results = groups[keys.JOBS_STREAM], groups[keys.RESULTS_STREAM]
        return {
            "control": (await self.get_control()).value,
            "jobs_waiting": jobs.get("lag") or 0,          # enqueued, not yet handed to a worker
            "jobs_in_flight": jobs.get("pending", 0),      # handed out, not yet acked
            "jobs_delayed": await self.redis.zcard(keys.DELAYED_KEY),
            "results_unwritten": (results.get("lag") or 0) + results.get("pending", 0),
            "dlq": await self.redis.xlen(keys.DLQ_STREAM),
            "workers": len([c for c in await self.redis.xinfo_consumers(keys.JOBS_STREAM, keys.WORKERS_GROUP)])
                       if jobs else 0,
        }


class ControlWatcher:
    """Polls lt:control so long-running loops see pause / stop within ~poll_s"""

    def __init__(self, queue: JobQueue, poll_s: float = 0.2):
        self.queue = queue
        self.poll_s = poll_s
        self.state = ControlState.RUN
        self.stopped = asyncio.Event()
        self.running = asyncio.Event()
        self.running.set()
        self.changed_at = now_ms()

    async def watch(self) -> None:
        while True:
            state = await self.queue.get_control()
            if state != self.state:
                self.state, self.changed_at = state, now_ms()
                if state == ControlState.STOP:
                    self.stopped.set()
                    self.running.clear()
                elif state == ControlState.PAUSE:
                    self.stopped.clear()
                    self.running.clear()
                else:
                    self.stopped.clear()
                    self.running.set()
            await asyncio.sleep(self.poll_s)


async def _cli(args) -> int:
    client = await connect_redis()
    queue = JobQueue(client)
    await queue.setup()
    try:
        if args.command in ("run", "pause", "stop"):
            await queue.set_control(ControlState(args.command))
            print(f"✅ control = {args.command}")
            if args.command == "stop" and args.purge:
                print(f"🧹 purged {await queue.purge_jobs()} queued / delayed jobs")
        elif args.command == "purge":
            print(f"🧹 purged {await queue.purge_jobs()} queued / delayed jobs")
        print(json.dumps(await queue.stats(), indent=2))
        return 0
    finally:
        await client.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Queue status and kill switch for this machine")
    parser.add_argument("command", choices=["status", "run", "pause", "stop", "purge"])
    parser.add_argument("--purge", action="store_true", help="with stop: also drop queued and delayed jobs")
    return asyncio.run(_cli(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
