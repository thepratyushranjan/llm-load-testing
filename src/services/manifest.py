import argparse
import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image

from src.core.config import get_settings
from src.models.dataset import ALL_FUNCTIONS, IMAGE_EXTS, SPLITS, VIDEO_EXTS, ManifestRow
from src.models.job import Function

FUNCTIONS = Function.__args__
FIELDS = list(ManifestRow.model_fields)


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe_image(path: Path) -> Tuple[int, int]:
    with Image.open(path) as im:
        im.verify()
    with Image.open(path) as im:
        return im.size


def probe_video(path: Path) -> Dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,avg_frame_rate:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=60,
    )
    info = json.loads(out.stdout)
    stream = info["streams"][0]
    num, _, den = stream.get("avg_frame_rate", "0/1").partition("/")
    fps = float(num) / float(den) if den and float(den) else None
    return {
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "duration_s": round(float(info["format"]["duration"]), 3),
        "fps": round(fps, 3) if fps else None,
    }


def classify(rel: Path) -> Tuple[str, str, str]:
    """(split, function, stem) from <split>/<function>/<sub/dirs/name>.<ext>"""
    parts = list(rel.parts)
    split = parts.pop(0) if len(parts) > 1 and parts[0] in SPLITS else "unknown"
    function = parts.pop(0) if len(parts) > 1 and parts[0] in FUNCTIONS else ALL_FUNCTIONS
    stem = str(Path(*parts).with_suffix(""))
    return split, function, stem


def scan(roots: List[Path], data_dir: Path, pair: bool) -> Tuple[Dict, Counter]:
    """Group media files into cases keyed by (split, function, stem)"""
    cases: Dict[Tuple, Dict[str, Path]] = {}
    stats = Counter()
    for root in roots:
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            ext = path.suffix.lower()
            kind = "image" if ext in IMAGE_EXTS else "video" if ext in VIDEO_EXTS else None
            if kind is None:
                stats["skipped_other_ext"] += 1
                continue
            stats[f"scanned_{kind}s"] += 1
            split, function, stem = classify(path.relative_to(root))
            key = (split, function, stem) if pair else (split, function, f"{kind}:{stem}")
            case = cases.setdefault(key, {})
            if kind in case:
                print(f"⚠️  {path} has the same name as {case[kind]}; keeping the first", file=sys.stderr)
                stats[f"name_clash_{kind}s"] += 1
                continue
            case[kind] = path
    return cases, stats


def describe(key: Tuple, media: Dict[str, Path], data_dir: Path) -> Optional[Dict]:
    """Hash and probe one case; None if any file is unreadable"""
    split, function, _ = key
    row = {"split": split, "function": function}
    try:
        if "image" in media:
            p = media["image"]
            row["image_path"] = str(p.relative_to(data_dir))
            row["image_sha1"] = sha1_file(p)
            row["width"], row["height"] = probe_image(p)
            row["image_kb"] = p.stat().st_size // 1024
        if "video" in media:
            p = media["video"]
            info = probe_video(p)
            row["video_path"] = str(p.relative_to(data_dir))
            row["video_sha1"] = sha1_file(p)
            row["video_width"], row["video_height"] = info["width"], info["height"]
            row["video_kb"] = p.stat().st_size // 1024
            row["duration_s"], row["fps"] = info["duration_s"], info["fps"]
    except Exception as e:
        print(f"❌ unreadable {key}: {e}", file=sys.stderr)
        return None
    return row


def build(roots: List[Path], data_dir: Path, machines: List[str], pair: bool, workers: int) -> Tuple[List[ManifestRow], Counter]:
    cases, stats = scan(roots, data_dir, pair)
    keys = sorted(cases)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        described = list(pool.map(lambda k: describe(k, cases[k], data_dir), keys))

    rows: List[ManifestRow] = []
    seen = set()
    for key, row in zip(keys, described):
        if row is None:
            stats["unreadable_cases"] += 1
            stats["unreadable_files"] += len(cases[key])
            continue
        content = hashlib.sha1(f"{row.get('image_sha1', '')}:{row.get('video_sha1', '')}".encode()).hexdigest()
        if (row["function"], content) in seen:
            stats["duplicate_cases"] += 1
            stats["duplicate_files"] += len(cases[key])
            continue
        seen.add((row["function"], content))
        rows.append(ManifestRow(
            case_id=f"c_{content[:12]}",
            machine=machines[int(content, 16) % len(machines)],
            **row,
        ))
    return rows, stats


def write_manifest(rows: List[ManifestRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if v is None else v) for k, v in row.model_dump().items()})
    tmp.replace(path)


def load_manifest(path: Optional[str] = None, machine: Optional[str] = None) -> List[ManifestRow]:
    """Read the manifest; optionally only one machine's share"""
    path = path or get_settings().manifest_path
    with open(path, newline="") as f:
        rows = [ManifestRow(**{k: v for k, v in r.items() if v != ""}) for r in csv.DictReader(f)]
    return [r for r in rows if machine is None or r.machine == machine]


def print_summary(rows: List[ManifestRow], stats: Counter) -> None:
    pairs = Counter(
        "image+video" if r.image_path and r.video_path else "image only" if r.image_path else "video only"
        for r in rows
    )
    print(f"\n📋 {len(rows)} cases")
    print("   by split/function:", dict(sorted(Counter(f"{r.split}/{r.function}" for r in rows).items())))
    print("   by machine:       ", dict(sorted(Counter(r.machine for r in rows).items())))
    print("   by media:         ", dict(pairs))
    print("   scan:             ", dict(sorted(stats.items())))

    files = stats["scanned_images"] + stats["scanned_videos"]
    in_manifest = sum(bool(r.image_path) + bool(r.video_path) for r in rows)
    excluded = stats["duplicate_files"] + stats["unreadable_files"] + stats["name_clash_images"] + stats["name_clash_videos"]
    mark = "✅" if in_manifest + excluded == files else "❌"
    print(f"   {mark} coverage: {in_manifest + excluded}/{files} media files accounted for"
          f" ({in_manifest} in manifest, {excluded} excluded as duplicate/unreadable/name clash)")


def main() -> int:
    settings = get_settings()
    data_dir = Path(settings.data_dir)
    parser = argparse.ArgumentParser(description="Scan the dataset and build the manifest")
    parser.add_argument("--root", action="append", type=Path,
                        help="media root(s); default: <data>/images and <data>/videos")
    parser.add_argument("--out", type=Path, default=Path(settings.manifest_path))
    parser.add_argument("--no-pair", action="store_true", help="don't pair image+video by name")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    roots = args.root or [p for p in (data_dir / "images", data_dir / "videos") if p.is_dir()]
    if not roots:
        print(f"❌ no media found: put files in {data_dir}/images and/or {data_dir}/videos", file=sys.stderr)
        return 1
    for root in roots:
        if not root.resolve().is_relative_to(data_dir.resolve()):
            print(f"❌ {root} must be inside {data_dir} (paths are stored relative to it)", file=sys.stderr)
            return 1

    rows, stats = build([r.resolve() for r in roots], data_dir.resolve(), settings.machine_list,
                        pair=not args.no_pair, workers=args.workers)
    write_manifest(rows, args.out)
    print_summary(rows, stats)
    print(f"✅ Manifest written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
