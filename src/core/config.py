from pydantic import field_validator
from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import List


class Settings(BaseSettings):
    """Application settings"""

    # API Settings
    app_name: str = "LLM Load Testing API"
    app_version: str = "1.0.0"
    debug: bool = False

    # ClickHouse Settings
    clickhouse_host: str = "clickhouse"
    clickhouse_port: int = 8123
    clickhouse_user: str = "default"
    clickhouse_password: str = ""
    clickhouse_database: str = "default"

    # Server Settings
    host: str = "0.0.0.0"
    port: int = 8000

    # Redis Settings
    redis_url: str = "redis://redis:6379/0"

    # Machine identity (A | B | C)
    machine_id: str = "A"
    log_level: str = "INFO"

    # vLLM Settings
    vllm_url: str = "http://mock-vllm:8000/v1"
    vllm_api_key: str = ""
    vllm_metrics_url: str = ""  # defaults to <vllm_url without /v1>/metrics
    model: str = "Qwen2.5-VL-32B-Instruct"

    # Load Shape Settings
    profile: str = "smoke"
    tick_seconds: float = 5.0
    functions: str = "event,ai_info,extraction"
    job_deadline_s: float = 30.0
    max_pixels: int = 1003520

    # Request Settings
    guided_json: bool = False       # constrain output to the response schema
    extra_body: str = ""            # JSON merged into every request, e.g. {"reasoning_effort": "none"}
    video_input: str = "video"      # video: video_url part (vLLM) | frames: N image parts (models without video, e.g. Ollama)
    video_frames: int = 4           # frames per video when video_input=frames
    video_side: int = 1280          # which video variant (VIDEO_VARIANTS_SIDE) requests use; images use MAX_PIXELS

    # Mock vLLM Settings (src/services/mock_vllm.py)
    mock_max_num_seqs: int = 16             # concurrent sequences; the rest wait, like a real GPU
    mock_prefill_tokens_per_s: float = 8000
    mock_decode_tokens_per_s: float = 40    # per sequence
    mock_image_tokens: int = 1000
    mock_video_tokens: int = 4000
    mock_error_rate: float = 0.0            # HTTP 500
    mock_rate_limit_rate: float = 0.0       # HTTP 429 with Retry-After
    mock_hang_rate: float = 0.0             # never answers (client timeout)
    mock_invalid_json_rate: float = 0.0     # truncated JSON

    # Worker Settings
    worker_concurrency: int = 64
    request_timeout_s: float = 30.0
    max_retries: int = 2               # model-call retries (429 / 5xx / timeout) before the DLQ
    max_deliveries: int = 3            # times a job may be handed out after worker crashes before the DLQ
    claim_idle_s: float = 0            # reclaim jobs unacked this long (0 = 2 x request_timeout_s)
    retry_backoff_s: float = 1.0       # first retry delay when no Retry-After; doubles per attempt
    max_rps: float = 0                 # token-bucket cap on requests sent from this machine (0 = no cap)
    rate_burst: int = 0                # bucket size (0 = max(1, max_rps))
    idempotency_ttl_s: int = 86400

    # Auto-stop (a run can override these)
    auto_stop: bool = True
    sla_p95_ms: float = 0              # stop when p95 end-to-end latency exceeds this (0 = off until the SLA is agreed)
    max_error_rate: float = 0.02       # stop when more than this share of the last minute's requests fail
    queue_growth_window_s: float = 120 # stop when the queue keeps growing for this long

    # Writer / Poller Settings
    ch_flush_interval_s: float = 2.0
    ch_batch_max: int = 5000
    metrics_poll_interval_s: float = 5.0

    # Data directory inside the container
    data_dir: str = "/app/data"

    # Dataset Settings
    machines: str = "A,B,C"
    image_variants_px: str = "1003520"  # max_pixels per image variant
    video_variants_side: str = "1280"      # max long side per video variant
    video_max_seconds: float = 10.0            # trim clips to this length (0 = keep)
    video_fps: float = 0.0                     # re-time clips to this fps (0 = keep)
    jpeg_quality: int = 90
    dataset_mount: str = "/app/dataset"        # where DATASET_DIR (your image folder) is mounted in the containers

    @property
    def machine_list(self) -> List[str]:
        return [m.strip() for m in self.machines.split(",") if m.strip()]

    @property
    def image_variants(self) -> List[int]:
        return [int(v) for v in self.image_variants_px.split(",") if v.strip()]

    @property
    def video_variants(self) -> List[int]:
        return [int(v) for v in self.video_variants_side.split(",") if v.strip()]

    @property
    def manifest_path(self) -> str:
        return f"{self.data_dir}/manifest.csv"

    @property
    def cache_dir(self) -> str:
        return f"{self.data_dir}/cache"

    @property
    def events_dir(self) -> str:
        return f"{self.data_dir}/events"

    @property
    def ground_truth_path(self) -> str:
        return f"{self.data_dir}/ground_truth.csv"

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"

    @field_validator("*", mode="before")
    @classmethod
    def _drop_inline_comment(cls, value):
        """docker compose passes `KEY=   # comment` through as the value "# comment" """
        if isinstance(value, str) and value.strip().startswith("#"):
            return ""
        return value

    @property
    def function_list(self) -> List[str]:
        """Enabled functions parsed from the comma-separated FUNCTIONS setting"""
        return [f.strip() for f in self.functions.split(",") if f.strip()]

    @property
    def metrics_url(self) -> str:
        """vLLM /metrics endpoint"""
        if self.vllm_metrics_url:
            return self.vllm_metrics_url
        base = self.vllm_url.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        return f"{base}/metrics"


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance"""
    return Settings()
