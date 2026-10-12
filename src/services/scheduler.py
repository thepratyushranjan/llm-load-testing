import asyncio
import json
import random
import statistics
import time
from collections import deque
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from src.core import keys
from src.core.profiles import RunSpec, Step
from src.models.dataset import ManifestRow
from src.models.job import Job, now_ms
from src.services.base import BaseService
from src.services.event_meta import EventMetaLoader
from src.services.job_builder import JobBuilder
from src.services.job_queue import ControlState, ControlWatcher, JobQueue
from src.services.manifest import load_manifest

AUTO_STOP_CHECK_S = 10
ERROR_STATUSES = ("error", "timeout", "rate_limited", "expired", "dlq")


class CasePicker:
    """Chooses (function, case) for each job following the function / split mix, cycling through cases"""

    def __init__(self, rows: List[ManifestRow], functions: List[str], split_mix: Optional[Dict[str, float]],
                 seed: str, sample: Optional[int] = None):
        self.rng = random.Random(seed)
        self.split_mix = split_mix
        by_function: Dict[str, List[ManifestRow]] = {}
        for row in rows:
            for fn in row.functions(functions):
                by_function.setdefault(fn, []).append(row)
        self.pools: Dict[Tuple[str, str], List[ManifestRow]] = {}
        for fn, fn_rows in by_function.items():
            self.rng.shuffle(fn_rows)
            for row in fn_rows[:sample] if sample else fn_rows:
                self.pools.setdefault((fn, row.split), []).append(row)
        self.cursor: Dict[Tuple[str, str], int] = {}

    def available(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for (fn, _), pool in self.pools.items():
            out[fn] = out.get(fn, 0) + len(pool)
        return out

    def pick(self, function_mix: Dict[str, float]) -> Tuple[str, ManifestRow]:
        mix = {f: w for f, w in function_mix.items() if w > 0 and any(k[0] == f for k in self.pools)}
        if not mix:
            raise LookupError(f"no cases for any of {list(function_mix)}")
        fn = self.rng.choices(list(mix), weights=list(mix.values()))[0]
        splits = [s for (f, s) in self.pools if f == fn]
        if self.split_mix:
            weighted = [s for s in splits if self.split_mix.get(s, 0) > 0] or splits
            split = self.rng.choices(weighted, weights=[self.split_mix.get(s, 1) for s in weighted])[0]
        else:
            split = self.rng.choices(splits, weights=[len(self.pools[(fn, s)]) for s in splits])[0]
        key = (fn, split)
        i = self.cursor.get(key, 0)
        self.cursor[key] = i + 1
        pool = self.pools[key]
        return fn, pool[i % len(pool)]


class Scheduler(BaseService):
    """Executes the run the CLI submits: open-loop ticks per profile step, drain + cooldown, auto-stop"""

    name = "scheduler"
    uses_clickhouse = True

    async def setup(self) -> None:
        await super().setup()
        self.queue = JobQueue(self.redis, self.settings)
        await self.queue.setup()
        self.watcher = ControlWatcher(self.queue)
        self.watcher_task = asyncio.create_task(self.watcher.watch())

    async def run(self) -> None:
        self.log.info(f"Waiting for runs (tick={self.settings.tick_seconds}s)")
        while not self.stopping:
            raw = await self.redis.get(keys.CURRENT_RUN_KEY)
            if raw:
                spec = RunSpec.model_validate_json(raw)
                try:
                    await self.execute(spec)
                except Exception as e:
                    self.log.exception(f"Run {spec.run_id} failed: {e}")
                    await self.finish(spec, f"failed: {e}")
            await self.wait_or_stop(1)

    async def teardown(self) -> None:
        self.watcher_task.cancel()
        await super().teardown()

    # ── run lifecycle ────────────────────────────────────────────────────

    async def execute(self, spec: RunSpec) -> None:
        profile = spec.profile
        self.spec = spec
        self.abort_reason: Optional[str] = None
        self.deadline_s = profile.job_deadline_s or self.settings.job_deadline_s
        self.log.info(f"▶ Run {spec.run_id}: profile={profile.name} steps={len(profile.steps)} "
                      f"functions={spec.functions} ~{profile.duration_s / 60:.1f} min")

        for _ in range(25):  # let the control watcher catch up with the "run" the CLI just set
            if self.watcher.state == ControlState.RUN:
                break
            await asyncio.sleep(0.2)
        rows = await asyncio.to_thread(load_manifest, None, self.settings.machine_id)
        self.builder = await asyncio.to_thread(JobBuilder, self.settings, EventMetaLoader())
        self.picker = CasePicker(rows, spec.functions, profile.split_mix, seed=spec.run_id, sample=spec.sample)
        self.log.info(f"Cases available for this machine: {self.picker.available()}")

        await self.write_run_row(spec, ended=False, notes=spec.notes)
        await self.progress(status="running", started_ms=now_ms(), enqueued=0, abort_reason="")
        monitor = asyncio.create_task(self.auto_stop_monitor())
        try:
            for index, step in enumerate(profile.steps):
                await self.run_step(index, step)
                if self.halted():
                    break
                last = index == len(profile.steps) - 1
                if step.drain_after or last:
                    await self.drain()
                if step.drain_after and not last and not self.halted():
                    await self.progress(status="cooldown")
                    await self.sleep_active(profile.cooldown_s)
                if self.halted():
                    break
        finally:
            monitor.cancel()

        if self.abort_reason:
            await self.queue.set_control(ControlState.STOP)
            await self.queue.purge_jobs()
            await self.finish(spec, f"auto-stopped: {self.abort_reason}")
        elif self.watcher.stopped.is_set() or self.stopping:
            await self.finish(spec, "stopped")
        else:
            await self.finish(spec, "completed")

    def halted(self) -> bool:
        return bool(self.abort_reason) or self.watcher.stopped.is_set() or self.stopping

    async def finish(self, spec: RunSpec, outcome: str) -> None:
        notes = f"{spec.notes} | {outcome}".strip(" |")
        await self.write_run_row(spec, ended=True, notes=notes)
        await self.progress(status=outcome, finished_ms=now_ms())
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.set(keys.LAST_RUN_KEY, spec.run_id)
            pipe.delete(keys.CURRENT_RUN_KEY)
            await pipe.execute()
        self.log.info(f"■ Run {spec.run_id} {outcome}")

    async def progress(self, **fields) -> None:
        fields.setdefault("run_id", self.spec.run_id)
        await self.redis.hset(keys.RUN_PROGRESS_KEY, mapping={k: str(v) for k, v in fields.items()})

    async def write_run_row(self, spec: RunSpec, ended: bool, notes: str) -> None:
        """lt_runs is a ReplacingMergeTree: the end-of-run row replaces the start row"""
        s = self.settings
        if not ended:
            self._started_dt = datetime.now(timezone.utc)
        started = getattr(self, "_started_dt", None) or datetime.now(timezone.utc)
        vllm_config = {"vllm_url": s.vllm_url, "extra_body": s.extra_body, "video_input": s.video_input,
                       "video_side": s.video_side, "tick_seconds": s.tick_seconds,
                       "functions": spec.functions, "profile": spec.profile.model_dump()}
        row = [spec.run_id, s.machine_id, spec.profile.name, s.model, json.dumps(vllm_config),
               s.worker_concurrency, s.max_pixels,
               s.video_frames if s.video_input == "frames" else None, s.video_fps or None,
               0, int(s.guided_json), started, datetime.now(timezone.utc) if ended else None, notes]
        cols = ["run_id", "machine_id", "profile", "model", "vllm_config", "worker_concurrency", "max_pixels",
                "video_num_frames", "video_fps", "prefix_cache", "guided_json", "started_at", "ended_at", "notes"]
        for attempt in range(12):  # ClickHouse may be restarting; never fail the run over its bookkeeping row
            try:
                await asyncio.to_thread(self.db.insert, "lt_runs", [row], cols)
                break
            except Exception as e:
                self.log.warning(f"lt_runs write failed (attempt {attempt + 1}): {e}")
                await asyncio.sleep(5)
                if not self.db.ping():
                    await asyncio.to_thread(self.db.connect)
        else:
            self.log.error(f"Giving up on the lt_runs row for {spec.run_id}")
        if ended:
            self._started_dt = None

    # ── steps ────────────────────────────────────────────────────────────

    async def sleep_active(self, seconds: float) -> None:
        """Sleep, waking on stop; paused time doesn't count"""
        end = time.monotonic() + seconds
        while not self.halted():
            if not self.watcher.running.is_set():
                paused = time.monotonic()
                while not self.watcher.running.is_set() and not self.halted():
                    await asyncio.sleep(0.2)
                end += time.monotonic() - paused
                continue
            left = end - time.monotonic()
            if left <= 0:
                return
            await asyncio.sleep(min(left, 0.2))

    async def run_step(self, index: int, step: Step) -> None:
        tick_s = self.settings.tick_seconds
        per_tick = step.per_tick(tick_s)
        n_ticks = max(1, round(step.duration_s / tick_s))
        mix = step.function_mix or self.spec.profile.function_mix
        await self.progress(status="running", step=index, step_name=step.name or f"step {index}",
                            step_started_ms=now_ms(), step_ticks=n_ticks, tick=0)
        self.log.info(f"Step {index} '{step.name}': {per_tick:g} jobs/tick × {n_ticks} ticks "
                      f"(~{per_tick / tick_s:.2f} RPS)")

        start = time.monotonic()
        paused_total = 0.0
        carry = 0.0
        lateness: List[float] = []
        enqueued = 0
        for k in range(n_ticks):
            target = start + paused_total + k * tick_s
            while True:  # open loop: fire on the clock, never wait for earlier jobs
                if self.halted():
                    return
                if not self.watcher.running.is_set():
                    paused = time.monotonic()
                    while not self.watcher.running.is_set() and not self.halted():
                        await asyncio.sleep(0.2)
                    paused_total += time.monotonic() - paused
                    target = start + paused_total + k * tick_s
                    continue
                left = target - time.monotonic()
                if left <= 0:
                    break
                await asyncio.sleep(min(left, 0.2))
            lateness.append((time.monotonic() - target) * 1000)

            carry += per_tick
            n = int(carry)
            carry -= n
            jobs = []
            for _ in range(n):
                fn, row = self.picker.pick(mix)
                try:
                    jobs.append(self.builder.build(row, fn, run_id=self.spec.run_id, step=index,
                                                   deadline_s=self.deadline_s))
                except (KeyError, FileNotFoundError) as e:
                    self.log.warning(f"Skipping {row.case_id}: {e}")
            if jobs:
                await self.queue.enqueue(jobs)
                enqueued += len(jobs)
            await self.redis.hincrby(keys.RUN_PROGRESS_KEY, "enqueued", len(jobs))
            await self.redis.hset(keys.RUN_PROGRESS_KEY, "tick", k + 1)

        p50 = statistics.median(lateness)
        self.log.info(f"Step {index} done: {enqueued} jobs; tick lateness p50={p50:.1f}ms max={max(lateness):.1f}ms")
        await self.progress(**{f"step{index}_tick_late_max_ms": round(max(lateness), 1)})

    async def drain(self) -> None:
        """Wait until queued, in-flight and delayed jobs are gone (they expire at the deadline anyway)"""
        await self.progress(status="draining")
        limit = time.monotonic() + self.deadline_s + self.settings.request_timeout_s + 15
        while time.monotonic() < limit and not self.halted():
            s = await self.queue.stats()
            if s["jobs_waiting"] + s["jobs_in_flight"] + s["jobs_delayed"] == 0:
                return
            await asyncio.sleep(1)

    # ── auto-stop (8.3) ──────────────────────────────────────────────────

    async def auto_stop_monitor(self) -> None:
        p, s = self.spec.profile, self.settings
        enabled = s.auto_stop if p.auto_stop is None else p.auto_stop
        if not enabled:
            return
        sla = s.sla_p95_ms if p.sla_p95_ms is None else p.sla_p95_ms
        max_err = s.max_error_rate if p.max_error_rate is None else p.max_error_rate
        growth_s = s.queue_growth_window_s if p.queue_growth_window_s is None else p.queue_growth_window_s
        window = deque()
        while True:
            await asyncio.sleep(AUTO_STOP_CHECK_S)
            try:
                if await self.auto_stop_check(window, growth_s, max_err, sla):
                    return
            except Exception as e:  # e.g. ClickHouse restarting: skip this check, keep monitoring
                self.log.warning(f"Auto-stop check skipped: {e}")

    async def auto_stop_check(self, window: deque, growth_s: float, max_err: float, sla: float) -> bool:
        # Backlog = jobs waiting in Redis + requests waiting inside vLLM. Workers pull jobs at once,
        # so when the GPU saturates the queue builds up at the server first.
        stats = await self.queue.stats()
        [server] = await asyncio.to_thread(self.db.query_rows, """
            SELECT argMax(requests_waiting, ts) AS waiting, count() AS n
            FROM lt_server_metrics WHERE run_id = {run:String} AND ts >= now64(3) - INTERVAL 15 SECOND""",
            {"run": self.spec.run_id})
        backlog = stats["jobs_waiting"] + (int(server["waiting"]) if server["n"] else 0)
        now = time.monotonic()
        window.append((now, backlog))
        while window and now - window[0][0] > growth_s:
            window.popleft()
        depths = [d for _, d in window]
        steps = list(zip(depths, depths[1:]))
        spanned = now - window[0][0] >= growth_s - AUTO_STOP_CHECK_S
        growing = len(steps) >= 3 and all(b > a for a, b in steps) and depths[-1] - depths[0] >= 5
        if spanned and growing:
            self.abort_reason = (f"backlog (queue + vLLM waiting) grew {depths[0]} → {depths[-1]} over "
                                 f"{growth_s:.0f}s (beyond capacity)")
        else:
            [m] = await asyncio.to_thread(self.db.query_rows, f"""
                SELECT count() AS n,
                       countIf(status IN {ERROR_STATUSES}) AS failed,
                       quantileIf(0.95)(e2e_ms, status = 'ok') AS p95
                FROM lt_requests
                WHERE run_id = {{run:String}} AND created_ts >= now64(3) - INTERVAL 60 SECOND""",
                {"run": self.spec.run_id})
            if m["n"] >= 20 and m["failed"] / m["n"] > max_err:
                self.abort_reason = f"error rate {m['failed'] / m['n']:.1%} > {max_err:.1%} in the last minute"
            elif sla and m["n"] >= 20 and m["p95"] and m["p95"] > sla:
                self.abort_reason = f"p95 latency {m['p95']:.0f}ms > SLA {sla:.0f}ms in the last minute"
        if self.abort_reason:
            self.log.warning(f"Auto-stop: {self.abort_reason}")
            await self.progress(abort_reason=self.abort_reason)
            return True
        return False


if __name__ == "__main__":
    Scheduler().start()
