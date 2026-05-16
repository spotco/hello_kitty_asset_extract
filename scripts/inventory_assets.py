from __future__ import annotations

import argparse
import time
from collections import Counter

from pathlib import Path

from common import BUNDLES_ROOT, REPORTS, iter_bundles, load_unitypy, write_csv


FIELDS = ["bundle_hash", "object_type", "object_name", "path_id"]

# Only read() these types — they have useful m_Name and are worth the cost.
# Everything else is recorded by type only, name left blank.
NAMED_TYPES = {
    "Mesh",
    "AnimationClip",
    "AnimatorController",
    "Material",
    "Texture2D",
    "Sprite",
    "GameObject",
    "SkinnedMeshRenderer",
    "Animator",
    "AssetBundle",
}


def object_name_from_data(data) -> str:
    for attr in ("m_Name", "name"):
        value = getattr(data, attr, "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def summarize(counter: Counter[str], total_bundles: int, total_objects: int) -> str:
    lines = [
        f"Bundles scanned: {total_bundles}",
        f"Objects recorded: {total_objects}",
        "",
        "Counts by object type:",
    ]
    for object_type, count in counter.most_common():
        lines.append(f"  {object_type}: {count}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan HKIA Unity bundles and build an object inventory CSV.")
    parser.add_argument("--limit", type=int, default=0, help="Process at most this many bundles. 0 means all bundles.")
    parser.add_argument("--reports", type=Path, default=REPORTS, help="Output reports directory.")
    parser.add_argument("--progress", type=int, default=50, help="Print progress every N bundles.")
    parser.add_argument("--verbose", action="store_true", help="Print every bundle as it loads.")
    args = parser.parse_args()

    if not BUNDLES_ROOT.is_dir():
        raise SystemExit(f"Bundles directory not found: {BUNDLES_ROOT}")

    UnityPy = load_unitypy()
    bundles = list(iter_bundles())
    if args.limit > 0:
        bundles = bundles[: args.limit]

    total = len(bundles)
    rows: list[dict[str, str | int]] = []
    type_counts: Counter[str] = Counter()

    print(f"Scanning {total} bundle(s) from {BUNDLES_ROOT}")
    t0 = time.monotonic()

    for index, bundle_path in enumerate(bundles, start=1):
        if args.verbose:
            print(f"  loading [{index}/{total}] {bundle_path.name} ({bundle_path.stat().st_size//1024}KB) ...", flush=True)
        bt = time.monotonic()
        try:
            env = UnityPy.load(str(bundle_path))
        except Exception as exc:
            print(f"[{index}/{total}] SKIP {bundle_path.name}: {exc}")
            continue
        if args.verbose:
            print(f"    loaded in {time.monotonic()-bt:.2f}s, iterating {len(list(env.objects))} objects", flush=True)

        for obj in env.objects:
            object_type = obj.type.name
            name = ""
            if object_type in NAMED_TYPES:
                if args.verbose:
                    print(f"      reading {object_type} path_id={obj.path_id} ...", flush=True)
                try:
                    name = object_name_from_data(obj.read())
                except Exception:
                    pass
            rows.append(
                {
                    "bundle_hash": bundle_path.stem,
                    "object_type": object_type,
                    "object_name": name,
                    "path_id": obj.path_id,
                }
            )
            type_counts[object_type] += 1

        if index % args.progress == 0 or index == total:
            elapsed = time.monotonic() - t0
            rate = index / elapsed if elapsed > 0 else 0
            eta = (total - index) / rate if rate > 0 else 0
            print(f"  [{index}/{total}] {index/total*100:.1f}%  {rate:.1f} bundles/s  ETA {eta:.0f}s  objects so far: {len(rows)}")

    reports_dir = args.reports
    reports_dir.mkdir(parents=True, exist_ok=True)
    csv_path = reports_dir / "bundle_inventory.csv"
    summary_path = reports_dir / "bundle_summary.txt"
    write_csv(csv_path, rows, FIELDS)
    summary_path.write_text(summarize(type_counts, total, len(rows)), encoding="utf-8")

    print(f"Wrote {csv_path}")
    print(f"Wrote {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
