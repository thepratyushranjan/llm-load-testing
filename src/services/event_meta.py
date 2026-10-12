import argparse
import hashlib
import json
import random
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from src.core.config import get_settings
from src.models.dataset import EventMeta, ManifestRow
from src.services.manifest import load_manifest

EVENT_FUNCTIONS = ("event", "ai_info")
PAYLOAD_TYPE = {"event": "event", "ai_info": "ai-info"}

# Defaults for synthetic payloads; override any key in <data>/events/synthetic.yaml.
# Shape mirrors the production event / ai-info payload: same keys, values generated per case.
SYNTHETIC_DEFAULTS = {
    "company_id": "000000000000000000000c01",
    "bucket_id": "000000000000000000000b01",
    "site_id": "000000000000000000000501",
    "site_name": "Test Office",
    "location": {"latitude": 77.0381654, "longitude": 28.439501, "country": "india",
                 "state": "haryana", "district": "gurugram", "city": "gurugram"},
    "cams": [
        {"cam_name": "cafeteria", "zone_names": ["mob_gathering", "temporal"]},
        {"cam_name": "boss_cabin", "zone_names": ["mob_gathering", "management_only", "role_restricted", "temporal"]},
        {"cam_name": "reception", "zone_names": ["temporal", "visitor_restricted"]},
        {"cam_name": "workstation", "zone_names": ["role_restricted", "behavioral"]},
        {"cam_name": "server_room", "zone_names": ["high_security"]},
    ],
    "event_triggers": ["unauthorized_management_zone_stranger", "unauthorized_access", "tailgating", "loitering",
                       "after_hours_intrusion", "cafeteria_overcrowding", "smoking", "unescorted_visitor"],
    "event_status": ["critical", "critical", "warning"],
    "ai_info_triggers": ["mob_gathering", "extended_break", "trip_hazard_object", "visitor_without_escort"],
    "ai_info_trigger_rate": 0.2,  # most ai-info payloads carry no triggers
    "labels": {"person": [0, 0.55], "chair": [13, 0.25], "bottle": [11, 0.1], "backpack": [24, 0.05],
               "laptop": [63, 0.05]},  # label: [class_id, weight]
    "identities": ["employee_01", "employee_02", "employee_03", "employee_04", "employee_05"],
    "detections": [1, 12],
    "frame": [1280, 720],
    "start_ts": "2026-10-01T00:00:00Z",
    "days": 7,
}


def _seed(*parts: str) -> int:
    return int(hashlib.sha1(":".join(parts).encode()).hexdigest()[:16], 16)


def _hex_id(rng: random.Random, n: int = 24) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


class EventMetaLoader:
    """Event / ai-info payload per case: per-case JSON → templates → synthetic (production shape)"""

    def __init__(self, events_dir: Optional[str] = None):
        settings = get_settings()
        self.dir = Path(events_dir or settings.events_dir)
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

    def get(self, row: ManifestRow, function: str = "event") -> EventMeta:
        return self._from_json(row) or self._from_template(row, function) or self._synthetic(row, function)

    def _from_json(self, row: ManifestRow) -> Optional[EventMeta]:
        """A real payload (event folder payload.json, or <case_id | image name | video name>.json), used exactly as given"""
        if row.payload_path:
            doc = json.loads((Path(get_settings().data_dir) / row.payload_path).read_text())
            return EventMeta(source="json", meta=doc.get("meta", {}), data=doc.get("data", doc))
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

    def _from_template(self, row: ManifestRow, function: str) -> Optional[EventMeta]:
        """Real payloads (one JSON per line); picks one of the matching type per case, as-is"""
        wanted = [t for t in self.templates if t.get("type") in (PAYLOAD_TYPE[function], None)] or self.templates
        if not wanted:
            return None
        doc = wanted[_seed(row.case_id, function) % len(wanted)]
        return EventMeta(source="template", meta=doc.get("meta", {}), data=doc.get("data", {}))

    def _synthetic(self, row: ManifestRow, function: str) -> EventMeta:
        cfg = self.synthetic
        rng = random.Random(_seed(row.case_id, function))
        width, height = cfg["frame"]

        cam = rng.choice(cfg["cams"])
        cam_id = _hex_id(random.Random(_seed("cam", cam["cam_name"])))
        start = int(datetime.fromisoformat(cfg["start_ts"].replace("Z", "+00:00")).timestamp())
        ts = start + rng.randrange(int(cfg["days"] * 86400))
        loc = cfg["location"]

        labels = list(cfg["labels"])
        weights = [cfg["labels"][label][1] for label in labels]
        boxes = []
        for _ in range(rng.randint(*cfg["detections"])):
            label = rng.choices(labels, weights)[0]
            w, h = rng.randint(width // 30, width // 4), rng.randint(height // 12, height // 2)
            boxes.append((cfg["labels"][label][0], label, {"top": rng.randint(0, height - h),
                                                           "left": rng.randint(0, width - w),
                                                           "width": w, "height": h}))
        detections = []
        for class_id, label, bbox in boxes:
            detection = {
                "label": label,
                "class_id": class_id,
                "confidence": 1.0,
                "model_id": 1,
                "track_id": rng.randint(1, 400),
                "bbox": bbox,
                "display_label": f"{label} (Tracked)",
            }
            if label == "person":
                kind = rng.random()
                if kind < 0.3:
                    identity, conf, display = "improper_angle", 0.0, "Improper angle"
                elif kind < 0.6:
                    identity, conf = "stranger", rng.uniform(0.1, 0.3)
                    display = f"stranger ({conf:.2f})"
                else:
                    identity, conf = rng.choice(cfg["identities"]), rng.uniform(0.35, 0.9)
                    display = f"{identity} ({conf:.2f})"
                detection["display_label"] = display
                detection["recognition"] = {"identity": identity, "confidence": conf,
                                            "identity_id": 0 if identity in ("stranger", "improper_angle")
                                            else cfg["identities"].index(identity) + 1}
            detections.append(detection)

        if function == "event":
            triggers = rng.sample(cfg["event_triggers"], k=1 if rng.random() < 0.8 else 2)
            status = rng.choice(cfg["event_status"])
            evidence = f"{loc['country']}_{loc['state']}_{loc['district']}_{cfg['site_id']}_{cam_id}_{ts}"
        else:
            triggers = rng.sample(cfg["ai_info_triggers"], k=1) if rng.random() < cfg["ai_info_trigger_rate"] else []
            status = "safe" if not triggers else "warning"
            evidence = f"insight_{loc['country']}_{loc['state']}_{loc['district']}_{cfg['site_id']}_0_{ts}"

        return EventMeta(
            source="synthetic",
            meta={
                "ts": ts, "cam_id": cam_id, "cam_name": cam["cam_name"], "zone_names": cam["zone_names"],
                "site_name": cfg["site_name"], "site_id": cfg["site_id"], **loc,
                "company_id": cfg["company_id"],
                "device_id": _hex_id(random.Random(_seed("device", cfg["site_id"])), 64),
            },
            data={
                "status": status,
                "triggers": triggers,
                "video_count": 1 if row.video_path else 0,
                "image_count": len(row.images),
                "detections": detections,
                "triaged_by": "", "triage_notes": "", "triage_timestamp": 0, "ai_insights": "",
                "capture_triggered": True,
                "evidence_path": f"{cfg['bucket_id']}/events/{evidence}",
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Check event metadata coverage for event / AI-info cases")
    parser.add_argument("--machine", help="only this machine's share")
    parser.add_argument("--show", metavar="CASE_ID", help="print the payload for one case")
    parser.add_argument("--function", default="event", choices=EVENT_FUNCTIONS)
    args = parser.parse_args()

    loader = EventMetaLoader()
    rows = [r for r in load_manifest(machine=args.machine) if r.functions(list(EVENT_FUNCTIONS))]

    if args.show:
        row = next((r for r in rows if r.case_id == args.show), None)
        if row is None:
            print(f"❌ {args.show} is not an event / AI-info case", file=sys.stderr)
            return 1
        print(json.dumps(loader.get(row, args.function).model_dump(), indent=2))
        return 0

    sources = Counter(loader.get(r, fn).source for r in rows for fn in r.functions(list(EVENT_FUNCTIONS)))
    print(f"📋 {len(rows)} event / AI-info cases, templates loaded: {len(loader.templates)}")
    print("   metadata source:", dict(sources))
    print(f"✅ {sum(sources.values())}/{sum(len(r.functions(list(EVENT_FUNCTIONS))) for r in rows)} "
          f"case × function payloads available")
    return 0


if __name__ == "__main__":
    sys.exit(main())
