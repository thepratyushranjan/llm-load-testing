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


def worker_stats_key(instance: str) -> str:
    return f"lt:worker:{instance}"


def heartbeat_key(service: str, instance: str) -> str:
    """Heartbeat key for one service instance"""
    return f"lt:hb:{service}:{instance}"


def idempotency_key(job_id: str, attempt: int) -> str:
    """Idempotency key for one job attempt"""
    return f"lt:idem:{job_id}:{attempt}"
