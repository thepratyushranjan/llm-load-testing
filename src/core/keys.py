JOBS_STREAM = "lt:jobs"
RESULTS_STREAM = "lt:results"
DLQ_STREAM = "lt:dlq"

WORKERS_GROUP = "workers"
WRITERS_GROUP = "writers"

CONTROL_KEY = "lt:control"  # run | pause | stop
CURRENT_RUN_KEY = "lt:run:current"


def heartbeat_key(service: str, instance: str) -> str:
    """Heartbeat key for one service instance"""
    return f"lt:hb:{service}:{instance}"


def idempotency_key(job_id: str, attempt: int) -> str:
    """Idempotency key for one job attempt"""
    return f"lt:idem:{job_id}:{attempt}"
