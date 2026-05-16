from __future__ import annotations

import csv
import json
import re
import warnings
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GAME_ROOT = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Hello Kitty Island Adventure")
BUNDLES_ROOT = GAME_ROOT / "Hello Kitty_Data" / "StreamingAssets" / "aa" / "StandaloneWindows64"
UNITY_VERSION = "2021.3.56f2"
REPORTS = ROOT / "reports"
EXTRACTED = ROOT / "extracted"


def load_unitypy():
    try:
        import UnityPy  # type: ignore

        warnings.filterwarnings("ignore")
        UnityPy.config.FALLBACK_UNITY_VERSION = UNITY_VERSION
        return UnityPy
    except ImportError as exc:
        raise SystemExit("UnityPy not installed. Run: pip install UnityPy") from exc


def safe_name(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    return text.strip("._") or "unnamed"


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def iter_bundles():
    """Yield all .bundle file paths in BUNDLES_ROOT."""
    yield from sorted(BUNDLES_ROOT.glob("*.bundle"))
