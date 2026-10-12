from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
SPLITS = ("trained", "untrained")
SPLIT_DIRS = {"trained": "trained", "train": "trained", "untrained": "untrained", "val": "untrained", "test": "untrained"}
ALL_FUNCTIONS = "all"  # media not sorted by function: used for every function


class ManifestRow(BaseModel):
    """One test case: an image and/or a video sharing the same name, or a real event folder"""
    case_id: str
    split: str
    function: str  # event | ai_info | extraction | all | comma list (e.g. "event,ai_info")
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

    # Real event folders (payload.json + its images [+ video]): every image, "|"-joined, and the payload file
    image_paths: Optional[str] = None
    image_sha1s: Optional[str] = None
    payload_path: Optional[str] = None

    def functions(self, enabled: List[str]) -> List[str]:
        """Functions this case can be used for"""
        if self.function == ALL_FUNCTIONS:
            return list(enabled)
        return [f for f in self.function.split(",") if f in enabled]

    @property
    def images(self) -> List[Tuple[str, str]]:
        """[(path, sha1)] of every image sent with this case"""
        if self.image_paths:
            return list(zip(self.image_paths.split("|"), self.image_sha1s.split("|")))
        return [(self.image_path, self.image_sha1)] if self.image_sha1 else []


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
