import argparse
import hashlib
import json
import random
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from src.core.config import get_settings
from src.models.dataset import EventMeta, ManifestRow
from src.services.manifest import load_manifest

EVENT_FUNCTIONS = ("event", "ai_info")

# Defaults for synthetic events; override any key in <data>/events/synthetic.yaml
SYNTHETIC_DEFAULTS = {
    "company_id": "test_company",
    "site_id": "test_site",
    "site_name": "Test Site",
    "cams": [
        {"cam_id": "cam_01", "cam_name": "Main Gate", "zone_names": ["entrance"]},
        {"cam_id": "cam_02", "cam_name": "Lobby", "zone_names": ["reception"]},
        {"cam_id": "cam_03", "cam_name": "Parking", "zone_names": ["parking"]},
        {"cam_id": "cam_04", "cam_name": "Corridor", "zone_names": ["corridor"]},
    ],
    # Trigger codes known to the pinned event prompt (office domain)
    "triggers": ["unauthorized_access", "tailgating", "loitering", "after_hours_intrusion",
                 "cafeteria_overcrowding", "smoking", "fire_smoke_detected", "unescorted_visitor"],
    "labels": {"person": 0.7, "car": 0.3, "motorcycle": 0.1, "truck": 0.05, "dog": 0.03},
    "vehicle_labels": ["car", "motorcycle", "truck", "bus"],
    "animal_labels": ["dog", "cat", "cow"],
    "max_objects": 6,
    "start_ts": "2026-10-01T00:00:00Z",
    "days": 7,
}


def _seed(case_id: str) -> int:
    return int(hashlib.sha1(case_id.encode()).hexdigest()[:16], 16)


class EventMetaLoader:
    """Event metadata per case: per-case JSON → ClickHouse templates → synthetic"""

    def __init__(self, events_dir: Optional[str] = None, zone_type: Optional[str] = None):
        settings = get_settings()
        self.dir = Path(events_dir or settings.events_dir)
        self.zone_type = zone_type or settings.zone_type
        self.templates = self._load_templates()
        self.synthetic = dict(SYNTHETIC_DEFAULTS)
        cfg = self.dir / "synthetic.yaml"
        if cfg.exists():
            self.synthetic.update(yaml.safe_load(cfg.read_text()) or {})

    def _load_templates(self) -> List[Dict]:
        path = self.dir / "templates.jsonl"
        if not path.exists():
            return []
        with path.open() as f:
            return [json.loads(line) for line in f if line.strip()]

    def get(self, row: ManifestRow) -> EventMeta:
        return self._from_json(row) or self._from_template(row) or self._synthetic(row)

    def _from_json(self, row: ManifestRow) -> Optional[EventMeta]:
        names = [row.case_id]
        for media in (row.image_path, row.video_path):
            if media:
                names.append(Path(media).stem)
        for name in names:
            path = self.dir / f"{name}.json"
            if path.exists():
                doc = json.loads(path.read_text())
                return EventMeta(source="json", meta=doc.get("meta", {}), data=doc.get("data", doc))
        return None

    def _from_template(self, row: ManifestRow) -> Optional[EventMeta]:
        if not self.templates:
            return None
        doc = self.templates[_seed(row.case_id) % len(self.templates)]
        meta = dict(doc.get("meta", {}))
        # never carry real tenant ids into the test
        meta.update(company_id=self.synthetic["company_id"], site_id=self.synthetic["site_id"])
        return EventMeta(source="template", meta=meta, data=doc.get("data", {}))

    def _synthetic(self, row: ManifestRow) -> EventMeta:
        cfg = self.synthetic
        rng = random.Random(_seed(row.case_id))
        width, height = row.width or row.video_width or 1920, row.height or row.video_height or 1080

        cam = rng.choice(cfg["cams"])
        start = datetime.fromisoformat(cfg["start_ts"].replace("Z", "+00:00"))
        ts = start + timedelta(seconds=rng.randrange(int(cfg["days"] * 86400)))

        labels, weights = zip(*cfg["labels"].items())
        detections = []
        for _ in range(rng.randint(1, cfg["max_objects"])):
            w = rng.randint(width // 20, width // 4)
            h = rng.randint(height // 10, height // 2)
            x, y = rng.randint(0, width - w), rng.randint(0, height - h)
            detections.append({
                "label": rng.choices(labels, weights)[0],
                "confidence": round(rng.uniform(0.5, 0.99), 2),
                "bbox": [x, y, x + w, y + h],
            })
        found = Counter(d["label"] for d in detections)

        return EventMeta(
            source="synthetic",
            meta={
                "company_id": cfg["company_id"], "site_id": cfg["site_id"], "site_name": cfg["site_name"],
                "cam_id": cam["cam_id"], "cam_name": cam["cam_name"],
                "zone_type": self.zone_type, "zone_names": cam["zone_names"],
                "ts": ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            data={
                "people_count": found["person"],
                "vehicle_count": sum(found[l] for l in cfg["vehicle_labels"]),
                "animal_count": sum(found[l] for l in cfg["animal_labels"]),
                "triggers": rng.sample(cfg["triggers"], k=rng.randint(1, 2)),
                "detections": detections,
                "recognitions": [],
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Check event metadata coverage for event / AI-info cases")
    parser.add_argument("--machine", help="only this machine's share")
    parser.add_argument("--show", metavar="CASE_ID", help="print the metadata for one case")
    args = parser.parse_args()

    loader = EventMetaLoader()
    rows = [r for r in load_manifest(machine=args.machine)
            if r.function in EVENT_FUNCTIONS or r.function == "all"]

    if args.show:
        row = next((r for r in rows if r.case_id == args.show), None)
        if row is None:
            print(f"❌ {args.show} is not an event / AI-info case", file=sys.stderr)
            return 1
        print(loader.get(row).model_dump_json(indent=2))
        return 0

    sources = Counter(loader.get(r).source for r in rows)
    print(f"📋 {len(rows)} event / AI-info cases, templates loaded: {len(loader.templates)}")
    print("   metadata source:", dict(sources))
    print(f"✅ {sum(sources.values())}/{len(rows)} cases have metadata")
    return 0


if __name__ == "__main__":
    sys.exit(main())
