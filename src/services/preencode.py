import argparse
import csv
import math
import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageOps

from src.core.config import get_settings
from src.models.dataset import CacheEntry
from src.services.manifest import load_manifest, probe_video

QWEN_FACTOR = 28
QWEN_MIN_PIXELS = 4 * 28 * 28
INDEX_FIELDS = list(CacheEntry.model_fields)


def smart_resize(height: int, width: int, max_pixels: int,
                 factor: int = QWEN_FACTOR, min_pixels: int = QWEN_MIN_PIXELS) -> Tuple[int, int]:
    """Same sizing rule as Qwen2.5-VL: multiples of 28 within [min_pixels, max_pixels]"""
    h_bar = max(factor, round(height / factor) * factor)
    w_bar = max(factor, round(width / factor) * factor)
    if h_bar * w_bar > max_pixels:
        beta = math.sqrt(height * width / max_pixels)
        h_bar = max(factor, math.floor(height / beta / factor) * factor)
        w_bar = max(factor, math.floor(width / beta / factor) * factor)
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = math.ceil(height * beta / factor) * factor
        w_bar = math.ceil(width * beta / factor) * factor
    return h_bar, w_bar


def image_variant(max_pixels: int) -> str:
    return f"px{max_pixels}"


def video_variant(max_side: int) -> str:
    return f"s{max_side}"


def cache_path(data_dir: Path, kind: str, variant: str, sha1: str) -> Path:
    ext = "jpg" if kind == "image" else "mp4"
    return data_dir / "cache" / kind / variant / f"{sha1}.{ext}"


def encode_image(src: str, dst: str, max_pixels: int, quality: int) -> Tuple[int, int]:
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        h, w = smart_resize(im.height, im.width, max_pixels)
        if (w, h) != im.size:
            im = im.resize((w, h), Image.Resampling.LANCZOS)
        tmp = f"{dst}.tmp"
        im.save(tmp, "JPEG", quality=quality, optimize=True)
    os.replace(tmp, dst)
    return w, h


def encode_video(src: str, dst: str, max_side: int, max_seconds: float, fps: float) -> None:
    vf = (f"scale=w='min({max_side},iw)':h='min({max_side},ih)'"
          ":force_original_aspect_ratio=decrease:force_divisible_by=2")
    if fps > 0:
        vf += f",fps={fps}"
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", src]
    if max_seconds > 0:
        cmd += ["-t", str(max_seconds)]
    tmp = f"{dst}.tmp.mp4"
    cmd += ["-an", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", tmp]
    subprocess.run(cmd, check=True, capture_output=True, timeout=600)
    os.replace(tmp, dst)


def image_entry(data_dir: Path, sha1: str, variant: str, path: Path) -> CacheEntry:
    with Image.open(path) as im:
        w, h = im.size
    return CacheEntry(sha1=sha1, kind="image", variant=variant, path=str(path.relative_to(data_dir)),
                      bytes=path.stat().st_size, width=w, height=h)


def video_entry(data_dir: Path, sha1: str, variant: str, path: Path) -> CacheEntry:
    info = probe_video(path)
    return CacheEntry(sha1=sha1, kind="video", variant=variant, path=str(path.relative_to(data_dir)),
                      bytes=path.stat().st_size, width=info["width"], height=info["height"],
                      duration_s=info["duration_s"], fps=info["fps"])


def load_cache_index(path: Optional[str] = None) -> Dict[Tuple[str, str], CacheEntry]:
    """(sha1, variant) -> cache entry"""
    path = path or f"{get_settings().cache_dir}/index.csv"
    with open(path, newline="") as f:
        entries = [CacheEntry(**{k: v for k, v in r.items() if v != ""}) for r in csv.DictReader(f)]
    return {(e.sha1, e.variant): e for e in entries}


def write_cache_index(entries: Dict[Tuple[str, str], CacheEntry], path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=INDEX_FIELDS)
        writer.writeheader()
        for e in sorted(entries.values(), key=lambda e: (e.kind, e.variant, e.sha1)):
            writer.writerow({k: ("" if v is None else v) for k, v in e.model_dump().items()})
    tmp.replace(path)


def main() -> int:
    settings = get_settings()
    data_dir = Path(settings.data_dir).resolve()
    parser = argparse.ArgumentParser(description="Resize images / transcode videos into the local cache")
    parser.add_argument("--machine", default=settings.machine_id, help="only this machine's share (default: MACHINE_ID)")
    parser.add_argument("--all", action="store_true", help="process every machine's share")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--force", action="store_true", help="re-encode even if the cache file exists")
    args = parser.parse_args()

    rows = load_manifest(machine=None if args.all else args.machine)
    images = {r.image_sha1: data_dir / r.image_path for r in rows if r.image_sha1}
    videos = {r.video_sha1: data_dir / r.video_path for r in rows if r.video_sha1}
    print(f"🧮 {len(rows)} cases → {len(images)} images × {len(settings.image_variants)} variants, "
          f"{len(videos)} videos × {len(settings.video_variants)} variants")

    index_path = data_dir / "cache" / "index.csv"
    index = load_cache_index(str(index_path)) if index_path.exists() else {}
    failed: List[str] = []

    def todo(kind: str, sources: Dict[str, Path], variants: List[str]):
        for sha1, src in sources.items():
            for variant in variants:
                dst = cache_path(data_dir, kind, variant, sha1)
                if dst.exists() and not args.force:
                    if (sha1, variant) not in index:
                        entry = image_entry if kind == "image" else video_entry
                        index[(sha1, variant)] = entry(data_dir, sha1, variant, dst)
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                yield sha1, variant, src, dst

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {}
        for px in settings.image_variants:
            for sha1, variant, src, dst in todo("image", images, [image_variant(px)]):
                futures[pool.submit(encode_image, str(src), str(dst), px, settings.jpeg_quality)] = (sha1, variant, src, dst)
        for done, fut in enumerate(as_completed(futures), 1):
            sha1, variant, src, dst = futures[fut]
            try:
                fut.result()
                index[(sha1, variant)] = image_entry(data_dir, sha1, variant, dst)
            except Exception as e:
                failed.append(f"{src} [{variant}]: {e}")
            if done % 200 == 0 or done == len(futures):
                print(f"   images {done}/{len(futures)}")

    # ffmpeg is the heavy process; threads just wait on it
    with ThreadPoolExecutor(max_workers=max(1, args.workers // 2)) as pool:
        futures = {}
        for side in settings.video_variants:
            for sha1, variant, src, dst in todo("video", videos, [video_variant(side)]):
                fut = pool.submit(encode_video, str(src), str(dst), side, settings.video_max_seconds, settings.video_fps)
                futures[fut] = (sha1, variant, src, dst)
        for done, fut in enumerate(as_completed(futures), 1):
            sha1, variant, src, dst = futures[fut]
            try:
                fut.result()
                index[(sha1, variant)] = video_entry(data_dir, sha1, variant, dst)
            except subprocess.CalledProcessError as e:
                failed.append(f"{src} [{variant}]: {e.stderr.decode(errors='replace').strip()[-300:]}")
            except Exception as e:
                failed.append(f"{src} [{variant}]: {e}")
            if done % 20 == 0 or done == len(futures):
                print(f"   videos {done}/{len(futures)}")

    index_path.parent.mkdir(parents=True, exist_ok=True)
    write_cache_index(index, index_path)

    for f in failed:
        print(f"❌ {f}", file=sys.stderr)
    needed = {(s, image_variant(px)) for s in images for px in settings.image_variants}
    needed |= {(s, video_variant(side)) for s in videos for side in settings.video_variants}
    missing = len(needed - set(index))
    total = sum(e.bytes for k, e in index.items() if k in needed)
    print(f"{'✅' if not missing else '⚠️ '} Cache: {len(needed) - missing}/{len(needed)} files ready, "
          f"{total / 1e9:.2f} GB, index at {index_path}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
