import argparse
import csv
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.core.config import get_settings
from src.models.dataset import GroundTruth, ManifestRow
from src.services.manifest import load_manifest

TRUE = {"true", "1", "yes", "y"}
FALSE = {"false", "0", "no", "n"}


def _bool(value: str) -> Optional[bool]:
    v = (value or "").strip().lower()
    return True if v in TRUE else False if v in FALSE else None


def _case_lookup(rows: List[ManifestRow]) -> Dict[str, List[ManifestRow]]:
    """Ground-truth ids may be a case_id or an image/video file name (with or without extension)"""
    lookup: Dict[str, List[ManifestRow]] = {}
    for r in rows:
        keys = {r.case_id}
        for media in (r.image_path, r.video_path):
            if media:
                keys |= {Path(media).name, Path(media).stem}
        for k in keys:
            lookup.setdefault(k, []).append(r)
    return lookup


def load_ground_truth(rows: List[ManifestRow], path: Optional[str] = None) -> Tuple[Dict[Tuple[str, str], GroundTruth], Counter]:
    """(case_id, function) -> labels, joined to the manifest by image_id"""
    path = Path(path or get_settings().ground_truth_path)
    stats = Counter()
    if not path.exists():
        return {}, stats

    lookup = _case_lookup(rows)
    truth: Dict[Tuple[str, str], GroundTruth] = {}
    with path.open(newline="") as f:
        for rec in csv.DictReader(f):
            stats["label_rows"] += 1
            image_id = (rec.get("image_id") or rec.get("case_id") or "").strip()
            function = (rec.get("function") or "").strip()
            matches = [r for r in lookup.get(image_id, []) if function in (r.function, "") or r.function == "all"]
            if not matches:
                stats["unmatched"] += 1
                continue
            plate = (rec.get("plate_number") or "").strip() or None
            labels = [l.strip() for l in (rec.get("labels") or "").split("|") if l.strip()]
            for r in matches:
                fn = function or r.function
                truth[(r.case_id, fn)] = GroundTruth(
                    case_id=r.case_id, function=fn,
                    alert_valid=_bool(rec.get("alert_valid", "")), plate_number=plate, labels=labels,
                )
                stats["joined"] += 1
    return truth, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Join ground-truth labels to the manifest and report coverage")
    parser.add_argument("--path", help="labels CSV (default: <data>/ground_truth.csv)")
    args = parser.parse_args()

    rows = load_manifest()
    truth, stats = load_ground_truth(rows, args.path)
    if not stats["label_rows"]:
        print("ℹ️  No ground-truth file; the Gemini baseline will provide reference answers")
        return 0

    trained = [r for r in rows if r.split == "trained"]
    labelled = {case_id for case_id, _ in truth}
    print(f"📋 {stats['label_rows']} label rows: {stats['joined']} joined, {stats['unmatched']} unmatched")
    print("   by function:", dict(Counter(fn for _, fn in truth)))
    print(f"   trained cases labelled: {len(labelled & {r.case_id for r in trained})}/{len(trained)}")
    return 1 if stats["unmatched"] else 0


if __name__ == "__main__":
    sys.exit(main())
