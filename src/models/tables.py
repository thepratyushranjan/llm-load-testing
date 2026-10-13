# Only these three tables live in ClickHouse: one row per valid model response (production JSON shape),
# with the request's speed. Live load-test numbers (errors, p95, auto-stop) are kept in Redis.
SPEED_COLUMNS = [
    ("created_ts", "DateTime64(3, 'UTC')"),     # when the request was scheduled
    ("step", "UInt16"),                         # profile step (load level)
    ("ttft_ms", "Nullable(UInt32)"),            # sent → first token
    ("model_latency_ms", "Nullable(UInt32)"),   # sent → last token
    ("e2e_ms", "Nullable(UInt32)"),             # scheduled → done
    ("prompt_tokens", "Nullable(UInt32)"),
    ("completion_tokens", "Nullable(UInt32)"),
    ("n_images", "UInt8"),
]

RESPONSE_COMMON = """
    run_id              String,
    machine_id          LowCardinality(String),
    job_id              String,
    attempt             UInt8,
    model               LowCardinality(String),
    case_id             String,
    split               LowCardinality(Nullable(String)),
    prompt_version      LowCardinality(String),""" + "".join(f"\n    {c} {t}," for c, t in SPEED_COLUMNS)

RESPONSE_ENGINE = """
    response_json       String CODEC(ZSTD(3)),  -- the full production-format response
    inserted_ts         DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(inserted_ts)
ORDER BY (case_id, run_id, job_id, attempt)
SETTINGS non_replicated_deduplication_window = 1000
"""

# Only valid responses (production JSON shape) are stored; failures are counted in the Redis live stats
EVENT_RESPONSES = """
CREATE TABLE IF NOT EXISTS event_responses
(""" + RESPONSE_COMMON + """
    alert_valid          UInt8,                   -- 1 = valid alert, 0 = false positive
    verdict              String,
    validation_summary   String,
    cam_name             Nullable(String),
    claimed_triggers     Array(String),
    claimed_people_count Nullable(Int32),
    ai_people_count      Nullable(Int32),
    what_happened        Nullable(String),
    why_it_happened      Nullable(String),
    recommendation       Nullable(String),
    roi_labels           Array(String),
    media_analyzed       UInt16,""" + RESPONSE_ENGINE

AI_INFO_RESPONSES = """
CREATE TABLE IF NOT EXISTS ai_info_responses
(""" + RESPONSE_COMMON + """
    cam_name                 Nullable(String),
    site_name                Nullable(String),
    status                   LowCardinality(String),  -- classification.status
    utilization              LowCardinality(String),
    critical_alerts          Array(String),           -- KPIs flagged = 1
    high_alerts              Array(String),
    medium_alerts            Array(String),
    low_alerts               Array(String),
    total_issues             UInt16,
    people_count             Int32,
    employee_count           Int32,
    visitor_count            Int32,
    contractor_count         Int32,
    unidentified_count       Int32,
    safety_score             Float32,
    cleanliness_score        Float32,
    uniform_compliance_score Float32,
    access_compliance_score  Float32,
    overall_compliance_score Float32,
    overall_compliance_pct   Float32,
    zone_type                LowCardinality(String),
    occupancy_level          LowCardinality(String),
    triggers                 Array(String),
    ai_summary               String,
    media_analyzed           UInt16,""" + RESPONSE_ENGINE

IMAGE_ANALYSIS_RESPONSES = """
CREATE TABLE IF NOT EXISTS image_analysis_responses
(""" + RESPONSE_COMMON + """
    person_count         Int32,
    vehicle_count        Int32,
    dog_count            Int32,
    labels               Array(String),           -- distinct object labels found
    plate_numbers        Array(String),
    detections           String CODEC(ZSTD(3)),   -- JSON list: label + attributes per object""" + RESPONSE_ENGINE

RESPONSE_TABLES = {"event": "event_responses", "ai_info": "ai_info_responses",
                   "extraction": "image_analysis_responses"}

SCHEMA_STATEMENTS = [EVENT_RESPONSES, AI_INFO_RESPONSES, IMAGE_ANALYSIS_RESPONSES] + [
    # tables created before the speed columns existed
    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {type_}"
    for table in RESPONSE_TABLES.values() for column, type_ in SPEED_COLUMNS
]
