JOBS_STREAM = "lt:jobs"
RESULTS_STREAM = "lt:results"
DLQ_STREAM = "lt:dlq"

WORKERS_GROUP = "workers"
WRITERS_GROUP = "writers"

DELAYED_KEY = "lt:delayed"  # sorted set: jobs waiting for a retry, scored by due time (ms)
RATE_LIMIT_KEY = "lt:ratelimit"

CONTROL_KEY = "lt:control"  # run | pause | stop
CURRENT_RUN_KEY = "lt:run:current"    # RunSpec JSON submitted by the CLI, picked up by the scheduler
RUN_PROGRESS_KEY = "lt:run:progress"  # hash the scheduler updates while a run is active
LAST_RUN_KEY = "lt:run:last"          # run_id of the last finished run
SERVER_METRICS_KEY = "lt:server"      # hash: latest vLLM /metrics sample


def worker_stats_key(instance: str) -> str:
    return f"lt:worker:{instance}"


def heartbeat_key(service: str, instance: str) -> str:
    """Heartbeat key for one service instance"""
    return f"lt:hb:{service}:{instance}"


def run_stats_key(run_id: str) -> str:
    """List: one compact entry per request attempt (status, timings) for live numbers and auto-stop"""
    return f"lt:run:stats:{run_id}"


def run_info_key(run_id: str) -> str:
    """Hash: run settings, start / end time and outcome"""
    return f"lt:run:info:{run_id}"


def idempotency_key(job_id: str, attempt: int) -> str:
    """Idempotency key for one job attempt"""
    return f"lt:idem:{job_id}:{attempt}"
