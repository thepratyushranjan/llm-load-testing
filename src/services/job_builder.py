from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.core.config import Settings, get_settings
from src.models.dataset import CacheEntry, ManifestRow
from src.models.job import Job, MediaRef, now_ms
from src.services.event_meta import EVENT_FUNCTIONS, EventMetaLoader
from src.services.preencode import frame_paths, image_variant, load_cache_index, video_variant


class JobBuilder:
    """Turn a manifest case into a Job: pick the cache variants and attach event metadata"""

    def __init__(self, settings: Optional[Settings] = None, meta_loader: Optional[EventMetaLoader] = None,
                 index: Optional[Dict[Tuple[str, str], CacheEntry]] = None):
        self.settings = settings or get_settings()
        self.meta_loader = meta_loader or EventMetaLoader()
        self.index = index if index is not None else load_cache_index()
        self.image_variant = image_variant(self.settings.max_pixels)
        self.video_variant = video_variant(self.settings.video_side)

    def media(self, row: ManifestRow, function: str) -> List[MediaRef]:
        refs: List[MediaRef] = []
        if row.video_sha1 and function in EVENT_FUNCTIONS:
            entry = self._entry(row.video_sha1, self.video_variant)
            if self.settings.video_input == "frames":
                data_dir = Path(self.settings.data_dir)
                for path in frame_paths(data_dir, self.video_variant, entry.sha1, self.settings.video_frames):
                    if not path.exists():
                        raise FileNotFoundError(f"{path} missing: run preencode with VIDEO_INPUT=frames")
                    refs.append(MediaRef(kind="image", sha1=entry.sha1, path=str(path.relative_to(data_dir)),
                                         bytes=path.stat().st_size))
            else:
                refs.append(MediaRef(kind="video", sha1=entry.sha1, path=entry.path, bytes=entry.bytes,
                                     duration_s=entry.duration_s))
        if row.image_sha1:
            entry = self._entry(row.image_sha1, self.image_variant)
            refs.append(MediaRef(kind="image", sha1=entry.sha1, path=entry.path, bytes=entry.bytes))
        return refs

    def _entry(self, sha1: str, variant: str) -> CacheEntry:
        entry = self.index.get((sha1, variant))
        if entry is None:
            raise KeyError(f"{sha1} [{variant}] not in cache: run preencode")
        return entry

    def build(self, row: ManifestRow, function: str, run_id: str, step: int = 0,
              deadline_s: Optional[float] = None) -> Job:
        created = now_ms()
        deadline_s = self.settings.job_deadline_s if deadline_s is None else deadline_s
        return Job(
            run_id=run_id,
            machine_id=self.settings.machine_id,
            step=step,
            function=function,
            split=row.split if row.split in ("trained", "untrained") else None,
            case_id=row.case_id,
            media=self.media(row, function),
            event_meta=self.meta_loader.get(row).model_dump(include={"meta", "data"}) if function in EVENT_FUNCTIONS else None,
            created_ts=created,
            deadline_ts=created + int(deadline_s * 1000),
        )
