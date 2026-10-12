import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional

from src.core import keys
from src.core.config import get_settings
from src.core.profiles import PROFILES, RunSpec, load_profile
from src.database import db
from src.redis_client import connect_redis
from src.services.job_queue import ControlState, JobQueue

EXPORT_TABLES = {
    "lt_runs": "SELECT * FROM lt_runs FINAL WHERE run_id = {run:String}",
    "lt_requests": "SELECT * EXCEPT (inserted_ts), inserted_ts, queue_wait_ms, ttft_ms, model_latency_ms, "
                   "e2e_ms, writer_lag_ms FROM lt_requests FINAL WHERE run_id = {run:String} ORDER BY created_ts",
    "lt_responses": "SELECT * FROM lt_responses FINAL WHERE run_id = {run:String}",
    "lt_server_metrics": "SELECT * FROM lt_server_metrics WHERE run_id = {run:String} ORDER BY ts",
}

LAST_MINUTE_SQL = """
SELECT count() AS rows,
       countIf(sent_ts IS NOT NULL) / 60 AS rps,
       countIf(status = 'ok') AS ok,
       countIf(status IN ('error', 'timeout', 'rate_limited', 'expired', 'dlq')) AS failed,
       round(quantileIf(0.5)(e2e_ms, status = 'ok')) AS p50_e2e_ms,
       round(quantileIf(0.95)(e2e_ms, status = 'ok')) AS p95_e2e_ms,
       round(quantileIf(0.95)(ttft_ms, status = 'ok')) AS p95_ttft_ms,
       round(avgIf(json_parse_ok, status = 'ok') * 100, 1) AS json_ok_pct,
       round(avgIf(schema_valid, status = 'ok') * 100, 1) AS schema_ok_pct
FROM lt_requests
WHERE run_id = {run:String} AND created_ts >= now64(3) - INTERVAL 60 SECOND
"""

RUN_SUMMARY_SQL = """
SELECT step, function, count() AS rows,
       countIf(status = 'ok') AS ok,
       countIf(status != 'ok') AS not_ok,
       round(quantileIf(0.5)(e2e_ms, status = 'ok')) AS p50_ms,
       round(quantileIf(0.95)(e2e_ms, status = 'ok')) AS p95_ms,
       round(quantileIf(0.95)(ttft_ms, status = 'ok')) AS p95_ttft_ms,
       round(avgIf(schema_valid, status = 'ok') * 100, 1) AS schema_ok_pct
FROM lt_requests FINAL
WHERE run_id = {run:String}
GROUP BY step, function ORDER BY step, function
"""


def table(rows: list) -> str:
    if not rows:
        return "  (no rows)"
    cols = list(rows[0])
    width = {c: max(len(str(c)), *(len(str(r[c])) for r in rows)) for c in cols}
    lines = ["  " + "  ".join(str(c).ljust(width[c]) for c in cols)]
    lines += ["  " + "  ".join(str(r[c]).ljust(width[c]) for c in cols) for r in rows]
    return "\n".join(lines)


class Cli:
    def __init__(self):
        self.settings = get_settings()

    async def __aenter__(self):
        self.redis = await connect_redis()
        self.queue = JobQueue(self.redis, self.settings)
        await self.queue.setup()
        return self

    async def __aexit__(self, *exc):
        await self.redis.aclose()
        if db.client:
            db.disconnect()

    def ch(self):
        if not db.client:
            db.connect()
        return db

    # ── commands ─────────────────────────────────────────────────────────

    async def profiles(self, args) -> int:
        if args.file:
            p = load_profile(path=args.file)
            print(f"✅ {args.file} is valid: {p.name}, {len(p.steps)} steps, ~{p.duration_s / 60:.0f} min")
            return 0
        tick = self.settings.tick_seconds
        for p in PROFILES.values():
            steps = ", ".join(f"{s.per_tick(tick):g}/tick×{s.duration_s / 60:g}m" for s in p.steps)
            print(f"✅ {p.name:9} ~{p.duration_s / 60:5.0f} min  [{steps}]\n             {p.description}")
        return 0

    async def run(self, args) -> int:
        current = await self.redis.get(keys.CURRENT_RUN_KEY)
        if current and not args.force:
            print(f"❌ run {RunSpec.model_validate_json(current).run_id} is still active "
                  f"(./lt stop, or --force to replace it)", file=sys.stderr)
            return 1
        profile = load_profile(name=args.profile, path=args.file).scaled(args.scale, args.rate_scale)
        if args.rate is not None:
            profile = profile.model_copy(update={"steps": [
                s.model_copy(update={"batch_per_tick": args.rate, "rps": None}) for s in profile.steps]})
        overrides = {k: v for k, v in {"sla_p95_ms": args.sla_p95_ms, "max_error_rate": args.max_error_rate,
                                       "queue_growth_window_s": args.queue_growth_s,
                                       "auto_stop": False if args.no_auto_stop else None}.items() if v is not None}
        profile = profile.model_copy(update=overrides)
        functions = args.functions.split(",") if args.functions else self.settings.function_list
        if args.functions:
            profile = profile.model_copy(update={"function_mix": {f: profile.function_mix.get(f, 1.0) or 1.0
                                                                  for f in functions}})
        if not Path(self.settings.manifest_path).exists():
            print(f"❌ no manifest at {self.settings.manifest_path}: run the dataset pipeline first", file=sys.stderr)
            return 1

        spec = RunSpec(run_id=RunSpec.new_id(profile.name), machine_id=self.settings.machine_id,
                       profile=profile, functions=functions, sample=args.sample, notes=args.notes or "")
        await self.queue.purge_jobs()
        await self.redis.delete(keys.RUN_PROGRESS_KEY)
        await self.queue.set_control(ControlState.RUN)  # before submitting: a leftover "stop" must not end it
        await self.redis.set(keys.CURRENT_RUN_KEY, spec.model_dump_json())
        print(f"▶ submitted {spec.run_id}: {profile.name}, {len(profile.steps)} steps, "
              f"~{profile.duration_s / 60:.1f} min, functions={functions}")
        if args.follow:
            return await self.follow(spec.run_id)
        return 0

    async def follow(self, run_id: str) -> int:
        while True:
            await asyncio.sleep(5)
            await self.status(argparse.Namespace(run_id=run_id, brief=True))
            if (await self.redis.get(keys.LAST_RUN_KEY)) == run_id and not await self.redis.exists(keys.CURRENT_RUN_KEY):
                for _ in range(15):  # let the writer flush the last results before the final summary
                    if (await self.queue.stats())["results_unwritten"] == 0:
                        break
                    await asyncio.sleep(1)
                await asyncio.sleep(self.settings.ch_flush_interval_s)
                print()
                await self.status(argparse.Namespace(run_id=run_id, brief=False))
                return 0

    async def control(self, state: ControlState, purge: bool = False) -> int:
        await self.queue.set_control(state)
        print(f"✅ control = {state.value}")
        if purge:
            print(f"🧹 dropped {await self.queue.purge_jobs()} queued / delayed jobs")
        return 0

    async def status(self, args) -> int:
        progress = await self.redis.hgetall(keys.RUN_PROGRESS_KEY)
        current = await self.redis.get(keys.CURRENT_RUN_KEY)
        run_id = getattr(args, "run_id", None) or (RunSpec.model_validate_json(current).run_id if current else None) \
            or progress.get("run_id") or await self.redis.get(keys.LAST_RUN_KEY)
        stats = await self.queue.stats()
        workers = []
        async for key in self.redis.scan_iter(match="lt:worker:*"):
            workers.append(await self.redis.hgetall(key))

        if progress and progress.get("run_id") == run_id:
            started = int(progress.get("started_ms", 0) or 0)
            elapsed = (int(progress.get("finished_ms") or time.time() * 1000) - started) / 1000 if started else 0
            print(f"run {run_id}: {progress.get('status')}  step {progress.get('step', '-')} "
                  f"'{progress.get('step_name', '')}' tick {progress.get('tick', 0)}/{progress.get('step_ticks', '?')}  "
                  f"enqueued {progress.get('enqueued', 0)}  elapsed {elapsed / 60:.1f} min"
                  + (f"\n  ⚠️  {progress['abort_reason']}" if progress.get("abort_reason") else ""))
        else:
            print(f"run {run_id or '-'}: no progress recorded")
        print(f"queue: {stats}")
        for w in workers:
            print(f"worker: cpu {w.get('cpu_pct')}%  inflight {w.get('inflight')}  done {w.get('done')}  "
                  f"loop lag {w.get('loop_lag_ms')}ms")
        if run_id:
            ch = self.ch()
            [last] = await asyncio.to_thread(ch.query_rows, LAST_MINUTE_SQL, {"run": run_id})
            print(f"last 60s: {last}")
            if not getattr(args, "brief", False):
                print("by step / function:")
                print(table(await asyncio.to_thread(ch.query_rows, RUN_SUMMARY_SQL, {"run": run_id})))
        return 0

    async def export(self, args) -> int:
        run_id = args.run_id or await self.redis.get(keys.LAST_RUN_KEY)
        if not run_id:
            print("❌ no run to export (give --run-id)", file=sys.stderr)
            return 1
        out = Path(self.settings.data_dir) / "exports" / run_id
        out.mkdir(parents=True, exist_ok=True)
        ch = self.ch()
        for name, sql in EXPORT_TABLES.items():
            data = await asyncio.to_thread(ch.export_parquet, sql, {"run": run_id})
            (out / f"{name}.parquet").write_bytes(data)
            [n] = await asyncio.to_thread(ch.query_rows, f"SELECT count() AS n FROM ({sql})", {"run": run_id})
            print(f"  {name:18} {n['n']:>7} rows → {out / (name + '.parquet')}")
        print(f"✅ exported {run_id}")
        return 0


async def main_async(args) -> int:
    async with Cli() as cli:
        if args.command == "profiles":
            return await cli.profiles(args)
        if args.command == "run":
            return await cli.run(args)
        if args.command == "pause":
            return await cli.control(ControlState.PAUSE)
        if args.command == "resume":
            return await cli.control(ControlState.RUN)
        if args.command == "stop":
            return await cli.control(ControlState.STOP, purge=not args.keep_queue)
        if args.command == "status":
            while True:
                await cli.status(args)
                if not args.watch:
                    return 0
                await asyncio.sleep(5)
                print()
        if args.command == "export":
            return await cli.export(args)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(prog="lt", description="Load-test control for this machine")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="start a run with a profile")
    p.add_argument("--profile", default=get_settings().profile, help=f"one of: {', '.join(PROFILES)}")
    p.add_argument("--file", help="custom profile YAML instead of a built-in one")
    p.add_argument("--scale", type=float, default=1.0, help="multiply step durations (0.1 = 10x shorter rehearsal)")
    p.add_argument("--rate-scale", type=float, default=1.0, help="multiply every step's rate")
    p.add_argument("--rate", type=float, help="jobs per tick for every step (e.g. soak at ~70%% of the ramp max)")
    p.add_argument("--functions", help="comma list; default FUNCTIONS")
    p.add_argument("--sample", type=int, help="use only N cases per function (e.g. 200 instead of the whole dataset)")
    p.add_argument("--sla-p95-ms", type=float, help="auto-stop when p95 end-to-end latency exceeds this")
    p.add_argument("--max-error-rate", type=float, help="auto-stop above this failure share (default 0.02)")
    p.add_argument("--queue-growth-s", type=float, help="auto-stop when the queue grows for this long (default 120)")
    p.add_argument("--no-auto-stop", action="store_true")
    p.add_argument("--notes", default="")
    p.add_argument("--force", action="store_true", help="replace an active run")
    p.add_argument("--follow", action="store_true", help="print status until the run ends")

    sub.add_parser("pause", help="stop sending new requests; in-flight ones finish")
    sub.add_parser("resume", help="continue after pause")
    p = sub.add_parser("stop", help="kill switch: end the run, cancel in-flight requests, drop the queue")
    p.add_argument("--keep-queue", action="store_true")
    p = sub.add_parser("status", help="run progress, queue, workers, last minute of results")
    p.add_argument("--run-id")
    p.add_argument("--watch", action="store_true")
    p = sub.add_parser("export", help="ClickHouse → Parquet for one run (data/exports/<run_id>/)")
    p.add_argument("--run-id", help="default: the last finished run")
    p = sub.add_parser("profiles", help="list and validate the built-in profiles (or --file)")
    p.add_argument("--file")

    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
