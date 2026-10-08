LT_RUNS = """
CREATE TABLE IF NOT EXISTS lt_runs
(
    run_id              String,
    machine_id          LowCardinality(String),
    profile             LowCardinality(String),
    model               LowCardinality(String),
    vllm_config         String,                  -- JSON: launch flags, version
    worker_concurrency  UInt32,
    max_pixels          UInt32,
    video_num_frames    Nullable(UInt32),
    video_fps           Nullable(Float32),
    prefix_cache        UInt8,
    guided_json         UInt8,
    started_at          DateTime64(3, 'UTC'),
    ended_at            Nullable(DateTime64(3, 'UTC')),
    notes               String,
    updated_at          DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (run_id, machine_id)
"""

LT_REQUESTS = """
CREATE TABLE IF NOT EXISTS lt_requests
(
    machine_id          LowCardinality(String),
    run_id              String,
    step                UInt16,
    job_id              String,
    attempt             UInt8,
    model               LowCardinality(String),
    function            LowCardinality(String),  -- event | ai_info | extraction
    case_id             String,
    split               LowCardinality(Nullable(String)),

    n_images            UInt8,
    n_videos            UInt8,
    media_bytes         UInt64,
    video_duration_s    Nullable(Float32),
    num_frames          Nullable(UInt32),

    created_ts          DateTime64(3, 'UTC'),
    enqueued_ts         Nullable(DateTime64(3, 'UTC')),
    dequeued_ts         Nullable(DateTime64(3, 'UTC')),
    sent_ts             Nullable(DateTime64(3, 'UTC')),
    first_token_ts      Nullable(DateTime64(3, 'UTC')),
    done_ts             Nullable(DateTime64(3, 'UTC')),
    inserted_ts         DateTime64(3, 'UTC') DEFAULT now64(3),

    queue_wait_ms       Nullable(Int64) MATERIALIZED dateDiff('millisecond', created_ts, dequeued_ts),
    ttft_ms             Nullable(Int64) MATERIALIZED dateDiff('millisecond', sent_ts, first_token_ts),
    model_latency_ms    Nullable(Int64) MATERIALIZED dateDiff('millisecond', sent_ts, done_ts),
    e2e_ms              Nullable(Int64) MATERIALIZED dateDiff('millisecond', created_ts, done_ts),
    writer_lag_ms       Nullable(Int64) MATERIALIZED dateDiff('millisecond', done_ts, inserted_ts),

    prompt_tokens       Nullable(UInt32),
    completion_tokens   Nullable(UInt32),
    cached_tokens       Nullable(UInt32),

    status              LowCardinality(String),  -- ok | error | timeout | rate_limited | expired | dlq
    http_status         Nullable(UInt16),
    error_type          LowCardinality(Nullable(String)),
    json_parse_ok       Nullable(UInt8),
    schema_valid        Nullable(UInt8)
)
ENGINE = ReplacingMergeTree(inserted_ts)
PARTITION BY toDate(created_ts)
ORDER BY (run_id, function, created_ts, job_id, attempt)
SETTINGS non_replicated_deduplication_window = 1000
"""

LT_RESPONSES = """
CREATE TABLE IF NOT EXISTS lt_responses
(
    run_id              String,
    machine_id          LowCardinality(String),
    job_id              String,
    attempt             UInt8,
    model               LowCardinality(String),
    function            LowCardinality(String),
    case_id             String,
    split               LowCardinality(Nullable(String)),
    prompt_version      LowCardinality(String),
    raw_text            String CODEC(ZSTD(3)),
    parsed_json         String CODEC(ZSTD(3)),
    alert_valid         Nullable(UInt8),
    plate_number        Nullable(String),
    labels              Array(String),
    severity            LowCardinality(Nullable(String)),
    inserted_ts         DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(inserted_ts)
ORDER BY (function, case_id, run_id, job_id, attempt)
SETTINGS non_replicated_deduplication_window = 1000
"""

LT_SERVER_METRICS = """
CREATE TABLE IF NOT EXISTS lt_server_metrics
(
    ts                      DateTime64(3, 'UTC'),
    run_id                  String,
    machine_id              LowCardinality(String),
    requests_running        Float64,
    requests_waiting        Float64,
    kv_cache_usage          Float64,
    prefix_cache_hit_rate   Nullable(Float64),
    prompt_tokens_per_s     Nullable(Float64),
    generation_tokens_per_s Nullable(Float64),
    gpu_util                Nullable(Float64),
    gpu_mem_used_bytes      Nullable(Float64),
    cpu_util                Nullable(Float64)    -- vLLM node CPU (video decode)
)
ENGINE = MergeTree
PARTITION BY toDate(ts)
ORDER BY (run_id, ts)
"""

# Per-minute rollup for live dashboards. Reports read raw lt_requests FINAL instead.
LT_REQUESTS_1M = """
CREATE TABLE IF NOT EXISTS lt_requests_1m
(
    minute          DateTime('UTC'),
    run_id          String,
    machine_id      LowCardinality(String),
    step            UInt16,
    function        LowCardinality(String),
    requests        SimpleAggregateFunction(sum, UInt64),
    ok              SimpleAggregateFunction(sum, UInt64),
    errors          SimpleAggregateFunction(sum, UInt64),
    timeouts        SimpleAggregateFunction(sum, UInt64),
    rate_limited    SimpleAggregateFunction(sum, UInt64),
    expired         SimpleAggregateFunction(sum, UInt64),
    json_ok         SimpleAggregateFunction(sum, UInt64),
    prompt_tokens   SimpleAggregateFunction(sum, UInt64),
    completion_tokens SimpleAggregateFunction(sum, UInt64),
    e2e_q           AggregateFunction(quantiles(0.5, 0.95, 0.99), Float64),
    model_q         AggregateFunction(quantiles(0.5, 0.95, 0.99), Float64),
    ttft_q          AggregateFunction(quantiles(0.5, 0.95, 0.99), Float64)
)
ENGINE = AggregatingMergeTree
PARTITION BY toDate(minute)
ORDER BY (run_id, function, machine_id, step, minute)
-- Lets a retried writer batch (same insert_deduplication_token) be dropped here too;
-- the writer must insert with deduplicate_blocks_in_dependent_materialized_views=1.
SETTINGS non_replicated_deduplication_window = 1000
"""

LT_REQUESTS_1M_MV = """
CREATE MATERIALIZED VIEW IF NOT EXISTS lt_requests_1m_mv TO lt_requests_1m AS
SELECT
    toStartOfMinute(created_ts) AS minute,
    run_id, machine_id, step, function,
    count() AS requests,
    countIf(status = 'ok') AS ok,
    countIf(status = 'error') AS errors,
    countIf(status = 'timeout') AS timeouts,
    countIf(status = 'rate_limited') AS rate_limited,
    countIf(status = 'expired') AS expired,
    countIf(json_parse_ok = 1) AS json_ok,
    sum(ifNull(prompt_tokens, 0)) AS prompt_tokens,
    sum(ifNull(completion_tokens, 0)) AS completion_tokens,
    quantilesStateIf(0.5, 0.95, 0.99)(toFloat64(assumeNotNull(e2e_ms)), status = 'ok' AND e2e_ms IS NOT NULL) AS e2e_q,
    quantilesStateIf(0.5, 0.95, 0.99)(toFloat64(assumeNotNull(model_latency_ms)), status = 'ok' AND model_latency_ms IS NOT NULL) AS model_q,
    quantilesStateIf(0.5, 0.95, 0.99)(toFloat64(assumeNotNull(ttft_ms)), status = 'ok' AND ttft_ms IS NOT NULL) AS ttft_q
FROM lt_requests
GROUP BY minute, run_id, machine_id, step, function
"""

SCHEMA_STATEMENTS = [
    LT_RUNS,
    LT_REQUESTS,
    LT_RESPONSES,
    LT_SERVER_METRICS,
    LT_REQUESTS_1M,
    LT_REQUESTS_1M_MV,
]
