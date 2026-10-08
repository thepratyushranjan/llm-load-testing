from typing import Dict, List, Optional

from pydantic import BaseModel, Field

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
SPLITS = ("trained", "untrained")
ALL_FUNCTIONS = "all"  # media not sorted by function: used for every function


class ManifestRow(BaseModel):
    """One test case: an image and/or a video sharing the same name"""
    case_id: str
    split: str
    function: str  # event | ai_info | extraction | all
    machine: str

    image_path: Optional[str] = None  # relative to data_dir
    image_sha1: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    image_kb: Optional[int] = None

    video_path: Optional[str] = None
    video_sha1: Optional[str] = None
    video_width: Optional[int] = None
    video_height: Optional[int] = None
    video_kb: Optional[int] = None
    duration_s: Optional[float] = None
    fps: Optional[float] = None

    def functions(self, enabled: List[str]) -> List[str]:
        """Functions this case can be used for"""
        if self.function == ALL_FUNCTIONS:
            return list(enabled)
        return [self.function] if self.function in enabled else []


class CacheEntry(BaseModel):
    """One pre-processed media file, looked up by (sha1, variant)"""
    sha1: str
    kind: str      # image | video
    variant: str   # e.g. px1003520 | s1280
    path: str      # relative to data_dir
    bytes: int
    width: int
    height: int
    duration_s: Optional[float] = None
    fps: Optional[float] = None


class EventMeta(BaseModel):
    """Event metadata fed to the event and AI-info prompts"""
    source: str  # json | template | synthetic
    meta: Dict
    data: Dict


class GroundTruth(BaseModel):
    case_id: str
    function: str
    alert_valid: Optional[bool] = None
    plate_number: Optional[str] = None
    labels: List[str] = Field(default_factory=list)
