import time
import uuid
from enum import StrEnum
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

Function = Literal["event", "ai_info", "extraction"]
Provider = Literal["vllm", "gemini"]
Split = Literal["trained", "untrained"]


def now_ms() -> int:
    """Current epoch time in milliseconds"""
    return time.time_ns() // 1_000_000


class Status(StrEnum):
    """Outcome of one request attempt"""
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    EXPIRED = "expired"
    DLQ = "dlq"


class MediaRef(BaseModel):
    """Reference to one image or video in the local dataset"""
    kind: Literal["image", "video"]
    sha1: str
    path: str  # relative to the data dir
    bytes: int = 0
    duration_s: Optional[float] = None  # videos only


class Job(BaseModel):
    """One unit of work pushed by the scheduler"""
    job_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str
    machine_id: str
    step: int = 0
    attempt: int = 0
    provider: Provider = "vllm"
    function: Function
    split: Optional[Split] = None
    case_id: str  # manifest id of the image/video case
    media: List[MediaRef] = Field(default_factory=list)
    event_meta: Optional[Dict] = None  # event / ai_info only
    created_ts: int = Field(default_factory=now_ms)
    deadline_ts: int

    def expired(self, at_ms: Optional[int] = None) -> bool:
        return (at_ms or now_ms()) >= self.deadline_ts

    def to_stream(self) -> Dict[str, str]:
        return {"job": self.model_dump_json()}

    @classmethod
    def from_stream(cls, fields: Dict) -> "Job":
        return cls.model_validate_json(fields["job"])


class Result(BaseModel):
    """One row of lt_requests (one per attempt) plus the optional response body"""
    machine_id: str
    run_id: str
    step: int
    job_id: str
    attempt: int
    provider: Provider
    model: str
    function: Function
    case_id: str
    split: Optional[Split] = None
    n_images: int = 0
    n_videos: int = 0
    media_bytes: int = 0

    created_ts: int
    enqueued_ts: Optional[int] = None
    dequeued_ts: Optional[int] = None
    sent_ts: Optional[int] = None
    first_token_ts: Optional[int] = None
    done_ts: Optional[int] = None

    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None

    status: Status
    http_status: Optional[int] = None
    error_type: Optional[str] = None
    json_parse_ok: Optional[bool] = None
    schema_valid: Optional[bool] = None

    raw_text: Optional[str] = None
    parsed_json: Optional[Dict] = None

    def to_stream(self) -> Dict[str, str]:
        return {"result": self.model_dump_json()}

    @classmethod
    def from_stream(cls, fields: Dict) -> "Result":
        return cls.model_validate_json(fields["result"])
