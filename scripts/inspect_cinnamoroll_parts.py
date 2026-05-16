from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Any

from common import BUNDLES_ROOT, REPORTS, load_inventory_index, load_unitypy, write_json

DEFAULT_BUNDLE_HASH = "f75e6233b13254a0a5e316a96d0d3114"
TARGET_NAMES = [
    "MiniStyle_BodyShape_Cinnamoroll",
    "MiniStyle_Head_Cinnamoroll",
    "MiniStyle_Tail_Cinnamoroll",
    "Shared_EyeMeshSet_Cinnamoroll",
    "Shared_MouthMeshSet_Cinnamoroll",
    "Shared_EyePlateTexture_Cinnamoroll",
    "Shared_MouthPlateTexture_Cinnamoroll",
]
INVENTORY_OBJECT_TYPES = {"Mesh", "Material", "Texture2D"}
COMPONENT_TYPES = {"GameObject", "Transform", "RectTransform", "MeshFilter", "MeshRenderer", "SkinnedMeshRenderer"}


def resolve_bundle_path(bundle_hash: str) -> Path:
    normalized = bundle_hash.strip()
    filename = normalized if normalized.lower().endswith(".bundle") else f"{normalized}.bundle"
    bundle_path = BUNDLES_ROOT / filename
    if not bundle_path.is_file():
        raise SystemExit(f"Bundle not found: {bundle_path}")
    return bundle_path


def pptr_file_id(pptr: Any) -> int:
    try:
        return int(getattr(pptr, "m_FileID", 0) or 0)
    except Exception:
        return 0


def pptr_path_id(pptr: Any) -> int:
    try:
        return int(getattr(pptr, "m_PathID", 0) or 0)
    except Exception:
        return 0


def read_pptr(pptr: Any) -> Any | None:
    if not pptr_path_id(pptr):
        return None
    try:
        return pptr.read()
    except Exception:
        return None


def object_name(data: Any, fallback: str = "") -> str:
    for attr in ("m_Name", "name"):
        value = getattr(data, attr, "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    game_object = read_pptr(getattr(data, "m_GameObject", None))
    if game_object is not None:
        name = getattr(game_object, "m_Name", "")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return fallback


def vec3_from_obj(value: Any, default: tuple[float, float, float]) -> list[float]:
    if value is None:
        return [float(default[0]), float(default[1]), float(default[2])]
    return [float(getattr(value, "x", default[0])), float(getattr(value, "y", default[1])), float(getattr(value, "z", default[2]))]


def quat_from_obj(value: Any) -> list[float]:
    if value is None:
        return [0.0, 0.0, 0.0, 1.0]
    return [
        float(getattr(value, "x", 0.0)),
        float(getattr(value, "y", 0.0)),
        float(getattr(value, "z", 0.0)),
        float(getattr(value, "w", 1.0)),
    ]


def safe_read_object(obj: Any) -> Any | None:
    try:
        return obj.read()
    except Exception:
        return None


def inventory_candidates(
    inventory_index: dict[int, list[dict[str, str]]],
    path_id: int,
    expected_types: set[str] | None = None,
) -> list[dict[str, str]]:
    candidates = inventory_index.get(path_id, [])
    if expected_types:
        candidates = [candidate for candidate in candidates if candidate["object_type"] in expected_types]
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate["bundle_hash"],
            candidate["object_type"],
            candidate["object_name"],
        ),
    )


def describe_pointer(
    pptr: Any,
    *,
    current_bundle_hash: str,
    inventory_index: dict[int, list[dict[str, str]]],
    expected_types: set[str] | None = None,
) -> dict[str, Any] | None:
    path_id = pptr_path_id(pptr)
    if not path_id:
        return None

    file_id = pptr_file_id(pptr)
    info: dict[str, Any] = {
        "file_id": file_id,
        "path_id": path_id,
        "resolved_bundle_hash": current_bundle_hash if file_id == 0 else None,
        "resolved_object_type": None,
        "resolved_object_name": None,
        "resolution": "internal" if file_id == 0 else "unresolved",
        "inventory_candidates": [],
    }

    resolved = read_pptr(pptr)
    if resolved is not None:
        info["resolved_object_name"] = object_name(resolved)
        info["resolved_object_type"] = type(resolved).__name__
        if file_id != 0:
            info["resolution"] = "resolved-read"
        return info

    if file_id == 0:
        info["resolution"] = "internal-read-failed"
        return info

    candidates = inventory_candidates(inventory_index, path_id, expected_types)
    info["inventory_candidates"] = candidates
    if len(candidates) == 1:
        candidate = candidates[0]
        info["resolved_bundle_hash"] = candidate["bundle_hash"]
        info["resolved_object_type"] = candidate["object_type"]
        info["resolved_object_name"] = candidate["object_name"] or None
        info["resolution"] = "inventory"
    elif candidates:
        info["resolution"] = "inventory-ambiguous"
    return info


def build_component_index(env: Any) -> tuple[dict[int, list[dict[str, Any]]], dict[int, dict[str, Any]]]:
    components_by_game_object: dict[int, list[dict[str, Any]]] = defaultdict(list)
    game_objects_by_path_id: dict[int, dict[str, Any]] = {}

    for obj in env.objects:
        object_type = obj.type.name
        if object_type not in COMPONENT_TYPES:
            continue
        data = safe_read_object(obj)
        if data is None:
            continue

        entry = {
            "type": object_type,
            "path_id": obj.path_id,
            "name": object_name(data),
            "data": data,
        }
        if object_type == "GameObject":
            game_objects_by_path_id[obj.path_id] = entry
            continue

        game_object_path_id = pptr_path_id(getattr(data, "m_GameObject", None))
        if game_object_path_id:
            components_by_game_object[game_object_path_id].append(entry)

    return components_by_game_object, game_objects_by_path_id


def pick_component(entries: list[dict[str, Any]], component_type: str) -> dict[str, Any] | None:
    return next((entry for entry in entries if entry["type"] == component_type), None)


def describe_transform(transform_entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if transform_entry is None:
        return None
    data = transform_entry["data"]
    father = getattr(data, "m_Father", None)
    parent_transform = read_pptr(father)
    parent_game_object = None
    if parent_transform is not None:
        parent_game_object = read_pptr(getattr(parent_transform, "m_GameObject", None))
    return {
        "path_id": transform_entry["path_id"],
        "type": transform_entry["type"],
        "parent_path_id": pptr_path_id(father),
        "parent_name": object_name(parent_game_object) if parent_game_object is not None else "",
        "local_position": vec3_from_obj(getattr(data, "m_LocalPosition", None), (0.0, 0.0, 0.0)),
        "local_rotation": quat_from_obj(getattr(data, "m_LocalRotation", None)),
        "local_scale": vec3_from_obj(getattr(data, "m_LocalScale", None), (1.0, 1.0, 1.0)),
    }


def describe_mesh_filter(
    mesh_filter_entry: dict[str, Any] | None,
    *,
    current_bundle_hash: str,
    inventory_index: dict[int, list[dict[str, str]]],
) -> dict[str, Any] | None:
    if mesh_filter_entry is None:
        return None
    data = mesh_filter_entry["data"]
    return {
        "path_id": mesh_filter_entry["path_id"],
        "mesh_pointer": describe_pointer(
            getattr(data, "m_Mesh", None),
            current_bundle_hash=current_bundle_hash,
            inventory_index=inventory_index,
            expected_types={"Mesh"},
        ),
    }


def describe_renderer(
    renderer_entry: dict[str, Any],
    *,
    current_bundle_hash: str,
    inventory_index: dict[int, list[dict[str, str]]],
) -> dict[str, Any]:
    data = renderer_entry["data"]
    materials: list[dict[str, Any]] = []
    for material in list(getattr(data, "m_Materials", []) or []):
        material_info = describe_pointer(
            material,
            current_bundle_hash=current_bundle_hash,
            inventory_index=inventory_index,
            expected_types={"Material"},
        )
        if material_info is not None:
            materials.append(material_info)

    renderer_info = {
        "path_id": renderer_entry["path_id"],
        "type": renderer_entry["type"],
        "materials": materials,
    }
    if renderer_entry["type"] == "SkinnedMeshRenderer":
        renderer_info["mesh_pointer"] = describe_pointer(
            getattr(data, "m_Mesh", None),
            current_bundle_hash=current_bundle_hash,
            inventory_index=inventory_index,
            expected_types={"Mesh"},
        )
        renderer_info["bone_count"] = len(list(getattr(data, "m_Bones", []) or []))
    return renderer_info


def inspect_game_object(
    game_object_entry: dict[str, Any],
    *,
    current_bundle_hash: str,
    components_by_game_object: dict[int, list[dict[str, Any]]],
    game_objects_by_path_id: dict[int, dict[str, Any]],
    inventory_index: dict[int, list[dict[str, str]]],
    visited_game_object_ids: set[int],
    keep_empty: bool,
) -> dict[str, Any] | None:
    game_object_path_id = game_object_entry["path_id"]
    visited_game_object_ids.add(game_object_path_id)
    components = sorted(
        components_by_game_object.get(game_object_path_id, []),
        key=lambda entry: (entry["type"], entry["path_id"]),
    )
    transform_entry = pick_component(components, "Transform") or pick_component(components, "RectTransform")
    mesh_filter_entry = pick_component(components, "MeshFilter")
    renderer_entries = [entry for entry in components if entry["type"] in {"MeshRenderer", "SkinnedMeshRenderer"}]
    has_geometry = bool(mesh_filter_entry is not None or renderer_entries)
    child_entries: list[dict[str, Any]] = []
    if transform_entry is not None and not has_geometry:
        for child_pptr in list(getattr(transform_entry["data"], "m_Children", []) or []):
            child_transform = read_pptr(child_pptr)
            if child_transform is None:
                continue
            child_game_object_path_id = pptr_path_id(getattr(child_transform, "m_GameObject", None))
            if not child_game_object_path_id or child_game_object_path_id in visited_game_object_ids:
                continue
            child_game_object_entry = game_objects_by_path_id.get(child_game_object_path_id)
            if child_game_object_entry is None:
                continue
            child_report = inspect_game_object(
                child_game_object_entry,
                current_bundle_hash=current_bundle_hash,
                components_by_game_object=components_by_game_object,
                game_objects_by_path_id=game_objects_by_path_id,
                inventory_index=inventory_index,
                visited_game_object_ids=visited_game_object_ids,
                keep_empty=False,
            )
            if child_report is not None:
                child_entries.append(child_report)

    if not keep_empty and not has_geometry and not child_entries:
        return None

    return {
        "target_name": game_object_entry["name"],
        "game_object_path_id": game_object_path_id,
        "transform": describe_transform(transform_entry),
        "mesh_filter": describe_mesh_filter(
            mesh_filter_entry,
            current_bundle_hash=current_bundle_hash,
            inventory_index=inventory_index,
        ),
        "renderers": [
            describe_renderer(
                renderer_entry,
                current_bundle_hash=current_bundle_hash,
                inventory_index=inventory_index,
            )
            for renderer_entry in renderer_entries
        ],
        "children": child_entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect key Cinnamoroll source-bundle parts and resolve mesh/material pointers.")
    parser.add_argument(
        "--bundle-hash",
        default=DEFAULT_BUNDLE_HASH,
        help="Source bundle hash containing the base Cinnamoroll assets.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPORTS / "cinnamoroll_parts_inspection.json",
        help="Where to write the JSON inspection report.",
    )
    parser.add_argument(
        "--inventory",
        type=Path,
        default=REPORTS / "bundle_inventory.csv",
        help="Bundle inventory CSV used to resolve external mesh/material pointers.",
    )
    args = parser.parse_args()

    bundle_path = resolve_bundle_path(args.bundle_hash)
    UnityPy = load_unitypy()
    inventory_index = load_inventory_index(args.inventory, object_types=INVENTORY_OBJECT_TYPES)

    print(f"Loading {bundle_path.name}")
    env = UnityPy.load(str(bundle_path))
    components_by_game_object, game_objects_by_path_id = build_component_index(env)

    target_entries = [
        game_object
        for game_object in game_objects_by_path_id.values()
        if game_object["name"] in TARGET_NAMES
    ]
    target_entries.sort(key=lambda entry: (TARGET_NAMES.index(entry["name"]), entry["path_id"]))

    if not target_entries:
        raise SystemExit(f"No target Cinnamoroll game objects found in {bundle_path.name}")

    report = {
        "bundle_hash": bundle_path.stem,
        "bundle_path": str(bundle_path),
        "inventory_csv": str(args.inventory),
        "target_names": TARGET_NAMES,
        "matches": [
            inspect_game_object(
                entry,
                current_bundle_hash=bundle_path.stem,
                components_by_game_object=components_by_game_object,
                game_objects_by_path_id=game_objects_by_path_id,
                inventory_index=inventory_index,
                visited_game_object_ids=set(),
                keep_empty=True,
            )
            for entry in target_entries
        ],
    }
    write_json(args.output, report)

    print(f"Wrote {args.output}")
    for match in report["matches"]:
        transform = match.get("transform") or {}
        print(
            f"- {match['target_name']} game_object={match['game_object_path_id']} "
            f"transform={transform.get('path_id', 0)} parent={transform.get('parent_path_id', 0)} "
            f"renderers={len(match['renderers'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
