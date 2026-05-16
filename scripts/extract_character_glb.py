from __future__ import annotations

import argparse
import copy
import json as json_mod
import math
import re
import shutil
import struct
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import BUNDLES_ROOT, EXTRACTED, REPORTS, iter_bundles, load_inventory_index, load_unitypy, safe_name, write_json

TEXTURE_PROPERTY_PRIORITY = [
    "_MainTex",
    "_BaseMap",
    "_BaseColorMap",
    "_BaseColorTex",
    "_Diffuse",
]
DEPENDENCY_OBJECT_TYPES = {"Mesh", "Material", "Texture2D"}
GENERIC_MATERIAL_NAMES = {"Lit", "EmoteMesh_FacePlate"}
MULTIPART_CHARACTER_PARTS = {
    "cinnamoroll": {
        "assembly_bundle_hash": "222c0db735425c4fa287cca209345a62",
        "assembly_root_name": "Cinnamoroll",
        "parts": [
            {"part_name": "body", "target_name": "MiniStyle_BodyShape_Cinnamoroll", "assembly_target_name": "MiniStyle_BodyShape_Default(Clone)"},
            {"part_name": "head", "target_name": "MiniStyle_Head_Cinnamoroll", "assembly_target_name": "MiniStyle_Head_Cinnamoroll(Clone)"},
            {"part_name": "tail", "target_name": "MiniStyle_Tail_Cinnamoroll", "assembly_target_name": "MiniStyle_Tail_Cinnamoroll(Clone)"},
            {"part_name": "eyes", "target_name": "Shared_EyeMeshSet_Cinnamoroll_EmoteA", "assembly_target_name": "Shared_EyeMeshSet_Cinnamoroll(Clone)"},
            {"part_name": "mouth", "target_name": "Shared_MouthMeshSet_Cinnamoroll_EmoteA", "assembly_target_name": "Shared_MouthMeshSet_Cinnamoroll(Clone)"},
        ],
    }
}


def pack_glb(gltf_json: dict, bin_data: bytes) -> bytes:
    json_bytes = json_mod.dumps(gltf_json, separators=(",", ":")).encode("utf-8")
    json_padded = json_bytes + b" " * (-len(json_bytes) % 4)
    bin_padded = bin_data + b"\x00" * (-len(bin_data) % 4)
    total = 12 + 8 + len(json_padded) + (8 + len(bin_padded) if bin_data else 0)
    out = struct.pack("<III", 0x46546C67, 2, total)
    out += struct.pack("<II", len(json_padded), 0x4E4F534A) + json_padded
    if bin_data:
        out += struct.pack("<II", len(bin_padded), 0x004E4942) + bin_padded
    return out


def pptr_path_id(pptr: Any) -> int:
    try:
        return int(getattr(pptr, "m_PathID", 0) or 0)
    except Exception:
        return 0


def pptr_file_id(pptr: Any) -> int:
    try:
        return int(getattr(pptr, "m_FileID", 0) or 0)
    except Exception:
        return 0


class DependencyResolver:
    def __init__(self, UnityPy, inventory_path: Path | None = None):
        self.UnityPy = UnityPy
        self.inventory_path = inventory_path or (REPORTS / "bundle_inventory.csv")
        self.inventory_index = load_inventory_index(self.inventory_path, object_types=DEPENDENCY_OBJECT_TYPES)
        self.env_cache: dict[str, Any] = {}
        self.object_index_cache: dict[str, dict[int, Any]] = {}
        self.object_data_cache: dict[tuple[str, int], Any | None] = {}

    def inventory_candidates(self, path_id: int, expected_types: set[str] | None = None) -> list[dict[str, str]]:
        candidates = self.inventory_index.get(path_id, [])
        if expected_types:
            candidates = [candidate for candidate in candidates if candidate["object_type"] in expected_types]
        return sorted(candidates, key=lambda candidate: (candidate["bundle_hash"], candidate["object_type"], candidate["object_name"]))

    def bundle_path(self, bundle_hash: str) -> Path:
        return BUNDLES_ROOT / f"{bundle_hash}.bundle"

    def load_bundle_env(self, bundle_hash: str):
        env = self.env_cache.get(bundle_hash)
        if env is not None:
            return env
        bundle_path = self.bundle_path(bundle_hash)
        if not bundle_path.is_file():
            raise FileNotFoundError(f"Bundle not found for dependency resolution: {bundle_path}")
        env = self.UnityPy.load(str(bundle_path))
        self.env_cache[bundle_hash] = env
        return env

    def bundle_objects(self, bundle_hash: str) -> dict[int, Any]:
        objects = self.object_index_cache.get(bundle_hash)
        if objects is not None:
            return objects
        env = self.load_bundle_env(bundle_hash)
        objects = {obj.path_id: obj for obj in env.objects}
        self.object_index_cache[bundle_hash] = objects
        return objects

    def read_object_from_bundle(self, bundle_hash: str, path_id: int) -> Any | None:
        cache_key = (bundle_hash, path_id)
        if cache_key in self.object_data_cache:
            return self.object_data_cache[cache_key]

        obj = self.bundle_objects(bundle_hash).get(path_id)
        if obj is None:
            self.object_data_cache[cache_key] = None
            return None

        try:
            data = obj.read()
        except Exception:
            data = None
        self.object_data_cache[cache_key] = data
        return data

    def read_pptr(self, pptr: Any, expected_types: set[str] | None = None) -> Any | None:
        path_id = pptr_path_id(pptr)
        if not path_id:
            return None

        file_id = pptr_file_id(pptr)
        if file_id == 0:
            try:
                return pptr.read()
            except Exception:
                return None

        for candidate in self.inventory_candidates(path_id, expected_types):
            data = self.read_object_from_bundle(candidate["bundle_hash"], path_id)
            if data is not None:
                return data

        try:
            return pptr.read()
        except Exception:
            pass
        return None


class BundleAssetLookup:
    def __init__(self, env: Any, resolver: DependencyResolver):
        self.materials_by_name: dict[str, list[dict[str, Any]]] = {}
        self.textures_by_name: dict[str, list[dict[str, Any]]] = {}

        for obj in env.objects:
            if obj.type.name not in {"Material", "Texture2D"}:
                continue
            try:
                data = obj.read()
            except Exception:
                continue
            name = object_name(data, resolver=resolver)
            if not name:
                continue
            entry = {"path_id": obj.path_id, "data": data}
            if obj.type.name == "Material":
                self.materials_by_name.setdefault(name, []).append(entry)
            else:
                self.textures_by_name.setdefault(name, []).append(entry)


def read_pptr(pptr: Any, resolver: DependencyResolver | None = None, expected_types: set[str] | None = None) -> Any | None:
    if not pptr_path_id(pptr):
        return None
    if resolver is not None:
        return resolver.read_pptr(pptr, expected_types=expected_types)
    try:
        return pptr.read()
    except Exception:
        return None


def object_name(data: Any, fallback: str = "", resolver: DependencyResolver | None = None) -> str:
    for attr in ("m_Name", "name"):
        value = getattr(data, attr, "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    game_object = read_pptr(getattr(data, "m_GameObject", None), resolver, {"GameObject"})
    if game_object is not None:
        name = getattr(game_object, "m_Name", "")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return fallback


def unity_to_gltf_vec3(values: Any) -> list[float]:
    x, y, z = values[:3]
    return [-float(x), float(y), float(z)]


def unity_to_gltf_quat(values: Any) -> list[float]:
    x, y, z, w = values[:4]
    return [float(x), -float(y), -float(z), float(w)]


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


def iter_game_object_component_pptrs(game_object: Any):
    for component in list(getattr(game_object, "m_Component", []) or []):
        for candidate in (
            getattr(component, "component", None),
            getattr(component, "m_Component", None),
            getattr(component, "first", None),
            getattr(component, "second", None),
        ):
            if pptr_path_id(candidate):
                yield candidate
                break


def read_game_object_transform(game_object: Any, resolver: DependencyResolver) -> Any | None:
    for component_pptr in iter_game_object_component_pptrs(game_object):
        component = read_pptr(component_pptr, resolver)
        if component is not None and type(component).__name__ in {"Transform", "RectTransform"}:
            return component
    return None


def transform_to_node_fields(transform: Any) -> dict[str, Any]:
    if transform is None:
        return {}
    return {
        "translation": unity_to_gltf_vec3(vec3_from_obj(getattr(transform, "m_LocalPosition", None), (0.0, 0.0, 0.0))),
        "rotation": unity_to_gltf_quat(quat_from_obj(getattr(transform, "m_LocalRotation", None))),
        "scale": vec3_from_obj(getattr(transform, "m_LocalScale", None), (1.0, 1.0, 1.0)),
        "transform_path_id": getattr(transform, "path_id", 0),
    }


def quaternion_to_rotation_rows(quaternion: list[float]) -> list[list[float]]:
    x, y, z, w = quaternion
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return [
        [1.0 - 2.0 * (yy + zz), 2.0 * (xy - wz), 2.0 * (xz + wy)],
        [2.0 * (xy + wz), 1.0 - 2.0 * (xx + zz), 2.0 * (yz - wx)],
        [2.0 * (xz - wy), 2.0 * (yz + wx), 1.0 - 2.0 * (xx + yy)],
    ]


def make_trs_rows(translation: list[float], rotation: list[float], scale: list[float]) -> list[list[float]]:
    rotation_rows = quaternion_to_rotation_rows(rotation)
    rows = [[0.0, 0.0, 0.0, 0.0] for _ in range(4)]
    for row in range(3):
        rows[row][0] = rotation_rows[row][0] * scale[0]
        rows[row][1] = rotation_rows[row][1] * scale[1]
        rows[row][2] = rotation_rows[row][2] * scale[2]
        rows[row][3] = translation[row]
    rows[3] = [0.0, 0.0, 0.0, 1.0]
    return rows


def multiply_rows(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [
        [sum(a[row][index] * b[index][col] for index in range(4)) for col in range(4)]
        for row in range(4)
    ]


def invert_matrix4_rows(matrix: list[list[float]]) -> list[list[float]]:
    augmented = [row[:] + [1.0 if row_index == col_index else 0.0 for col_index in range(4)] for row_index, row in enumerate(matrix)]

    for col in range(4):
        pivot = max(range(col, 4), key=lambda row: abs(augmented[row][col]))
        if abs(augmented[pivot][col]) < 1e-12:
            raise ValueError("Matrix is not invertible")
        if pivot != col:
            augmented[col], augmented[pivot] = augmented[pivot], augmented[col]

        scale = augmented[col][col]
        augmented[col] = [value / scale for value in augmented[col]]
        for row in range(4):
            if row == col:
                continue
            factor = augmented[row][col]
            if factor:
                augmented[row] = [value - factor * augmented[col][index] for index, value in enumerate(augmented[row])]

    return [row[4:] for row in augmented]


def unity_matrix_rows_from_transform(transform: Any) -> list[list[float]]:
    translation = vec3_from_obj(getattr(transform, "m_LocalPosition", None), (0.0, 0.0, 0.0))
    rotation = quat_from_obj(getattr(transform, "m_LocalRotation", None))
    scale = vec3_from_obj(getattr(transform, "m_LocalScale", None), (1.0, 1.0, 1.0))
    return make_trs_rows(translation, rotation, scale)


def rows_matrix_to_gltf_columns(rows: list[list[float]]) -> list[float]:
    signs = [-1.0, 1.0, 1.0, 1.0]
    converted = [[rows[row][col] * signs[row] * signs[col] for col in range(4)] for row in range(4)]
    return [converted[row][col] for col in range(4) for row in range(4)]


def build_transform_world_rows(transform: Any, resolver: DependencyResolver | None = None) -> list[list[float]]:
    current = transform
    world = [[1.0 if row == col else 0.0 for col in range(4)] for row in range(4)]
    chain: list[Any] = []
    while current is not None:
        chain.append(current)
        current = read_pptr(getattr(current, "m_Father", None), resolver, {"Transform"})
    for item in reversed(chain):
        world = multiply_rows(world, unity_matrix_rows_from_transform(item))
    return world


def find_game_object_by_name(env: Any, target_name: str) -> Any | None:
    for obj in env.objects:
        if obj.type.name != "GameObject":
            continue
        try:
            go = obj.read()
        except Exception:
            continue
        if getattr(go, "m_Name", "") == target_name:
            return go
    return None


def relative_node_transform_from_prefab(
    prefab_env: Any,
    assembly_root_name: str,
    assembly_target_name: str,
    resolver: DependencyResolver,
) -> dict[str, Any]:
    root_go = find_game_object_by_name(prefab_env, assembly_root_name)
    target_go = find_game_object_by_name(prefab_env, assembly_target_name)
    if root_go is None or target_go is None:
        raise RuntimeError(f"Could not resolve prefab transform source {assembly_target_name} under {assembly_root_name}")

    root_transform = read_game_object_transform(root_go, resolver)
    target_transform = read_game_object_transform(target_go, resolver)
    if root_transform is None or target_transform is None:
        raise RuntimeError(f"Missing transform for prefab source {assembly_target_name}")

    root_world = build_transform_world_rows(root_transform, resolver)
    target_world = build_transform_world_rows(target_transform, resolver)
    relative_rows = multiply_rows(invert_matrix4_rows(root_world), target_world)
    return {
        "matrix": rows_matrix_to_gltf_columns(relative_rows),
        "prefab_root_name": assembly_root_name,
        "prefab_target_name": assembly_target_name,
    }


def score_name_match(target_lower: str, *texts: str) -> int:
    target_tokens = {token for token in re.split(r"[^a-z0-9]+", target_lower) if token}
    best = 0
    for text in texts:
        text_lower = (text or "").lower().strip()
        if not text_lower:
            continue
        if text_lower == target_lower:
            best = max(best, 120)
        elif text_lower.startswith(target_lower):
            best = max(best, 80)
        elif target_lower in text_lower:
            best = max(best, 40)

        if target_tokens:
            overlap = len(target_tokens.intersection(token for token in re.split(r"[^a-z0-9]+", text_lower) if token))
            if overlap:
                token_score = overlap * 12
                if overlap == len(target_tokens):
                    token_score += 24
                best = max(best, token_score)
    return best


def find_matching_bundles(name: str, UnityPy) -> list[dict[str, Any]]:
    name_lower = name.lower()
    candidates: list[dict[str, Any]] = []
    bundles = list(iter_bundles())
    total = len(bundles)
    print(f"Searching {total} bundle(s) for '{name}'")
    for index, bundle_path in enumerate(bundles, start=1):
        try:
            env = UnityPy.load(str(bundle_path))
        except Exception as exc:
            print(f"skip {bundle_path.name}: load failed: {exc}")
            continue

        matches: list[dict[str, Any]] = []
        for obj in env.objects:
            try:
                data = obj.read()
            except Exception:
                continue
            current_name = object_name(data)
            if current_name and name_lower in current_name.lower():
                matches.append({"type": obj.type.name, "name": current_name, "path_id": obj.path_id})

        if matches:
            score = sum(3 if item["type"] in {"GameObject", "SkinnedMeshRenderer"} else 1 for item in matches)
            print(f"candidate {bundle_path.name}: {len(matches)} matching object(s)")
            candidates.append({"bundle": bundle_path, "matches": matches, "score": score})

        if index % 100 == 0 or index == total:
            print(f"  searched {index}/{total} bundles")

    candidates.sort(key=lambda item: (-item["score"], -len(item["matches"]), item["bundle"].name))
    return candidates


def load_env(bundle_path: Path, UnityPy):
    print(f"Loading bundle: {bundle_path}")
    return UnityPy.load(str(bundle_path))


def find_static_renderers(env, target_name: str, resolver: DependencyResolver) -> list[dict[str, Any]]:
    mesh_filters: dict[int, Any] = {}
    mesh_filter_names: dict[int, str] = {}
    for obj in env.objects:
        if obj.type.name != "MeshFilter":
            continue
        try:
            data = obj.read()
            game_object_pptr = getattr(data, "m_GameObject", None)
            go = read_pptr(game_object_pptr, resolver, {"GameObject"})
            if go is None:
                continue
            go_path_id = pptr_path_id(game_object_pptr)
            if not go_path_id:
                continue
            mesh_filters[go_path_id] = getattr(data, "m_Mesh", None)
            mesh_filter_names[go_path_id] = getattr(go, "m_Name", "")
        except Exception:
            continue

    candidates: list[dict[str, Any]] = []
    target_lower = target_name.lower()
    for obj in env.objects:
        if obj.type.name != "MeshRenderer":
            continue
        try:
            data = obj.read()
            game_object_pptr = getattr(data, "m_GameObject", None)
            go = read_pptr(game_object_pptr, resolver, {"GameObject"})
            if go is None:
                continue
            go_path_id = pptr_path_id(game_object_pptr)
            mesh_pptr = mesh_filters.get(go_path_id)
            mesh = read_pptr(mesh_pptr, resolver, {"Mesh"})
            if mesh is None:
                continue
            name = getattr(go, "m_Name", "") or mesh_filter_names.get(go_path_id, "") or "static_mesh"
            mesh_name = object_name(mesh, resolver=resolver)
            transform = read_game_object_transform(go, resolver)
            node_transform = transform_to_node_fields(transform)
            score = score_name_match(target_lower, name, mesh_name)
            candidates.append(
                {
                    "kind": "static",
                    "renderer": data,
                    "renderer_name": name,
                    "mesh_name": mesh_name,
                    "mesh": mesh,
                    "materials": list(getattr(data, "m_Materials", []) or []),
                    "bones": [],
                    "texture_hint_names": [name, mesh_name],
                    "node_transform": node_transform,
                    "score": score,
                }
            )
        except Exception:
            continue
    return candidates


def select_character_target(env, target_name: str, resolver: DependencyResolver) -> dict[str, Any] | None:
    target_lower = target_name.lower()
    candidates: list[dict[str, Any]] = []

    for obj in env.objects:
        if obj.type.name != "SkinnedMeshRenderer":
            continue
        try:
            data = obj.read()
            mesh = read_pptr(getattr(data, "m_Mesh", None), resolver, {"Mesh"})
            if mesh is None:
                continue
            go = read_pptr(getattr(data, "m_GameObject", None), resolver, {"GameObject"})
            renderer_name = getattr(go, "m_Name", "") if go is not None else object_name(data, "skinned_mesh", resolver)
            mesh_name = object_name(mesh, resolver=resolver)
            transform = read_game_object_transform(go, resolver) if go is not None else None
            node_transform = transform_to_node_fields(transform)
            material_count = len(getattr(data, "m_Materials", []) or [])
            bone_count = len(getattr(data, "m_Bones", []) or [])
            combined = " ".join(part for part in [renderer_name, mesh_name] if part)
            score = score_name_match(target_lower, renderer_name, mesh_name, combined)
            score += min(bone_count, 32)
            score += material_count
            candidates.append(
                {
                    "kind": "skinned",
                    "renderer": data,
                    "renderer_name": renderer_name or mesh_name or "character",
                    "mesh_name": mesh_name,
                    "mesh": mesh,
                    "materials": list(getattr(data, "m_Materials", []) or []),
                    "bones": list(getattr(data, "m_Bones", []) or []),
                    "texture_hint_names": [renderer_name, mesh_name],
                    "node_transform": node_transform,
                    "score": score,
                }
            )
        except Exception as exc:
            print(f"skip renderer {obj.path_id}: {exc}")

    candidates.extend(find_static_renderers(env, target_name, resolver))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item["score"], item["renderer_name"]))
    chosen = candidates[0]
    print(
        f"Selected {chosen['kind']} renderer '{chosen['renderer_name']}' "
        f"with {len(chosen['materials'])} material(s) and {len(chosen['bones'])} bone(s)"
    )
    return chosen


def select_static_part_target(env, target_name: str, resolver: DependencyResolver) -> dict[str, Any] | None:
    candidates = find_static_renderers(env, target_name, resolver)
    target_lower = target_name.lower()
    exact = [candidate for candidate in candidates if candidate["renderer_name"].lower() == target_lower or candidate["mesh_name"].lower() == target_lower]
    selected = exact or candidates
    if not selected:
        return None
    selected.sort(key=lambda item: (-item["score"], item["renderer_name"]))
    return selected[0]


def resolve_multi_part_targets(env, target_name: str, resolver: DependencyResolver) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest = MULTIPART_CHARACTER_PARTS.get(target_name.lower())
    if not manifest:
        raise SystemExit(f"No multi-part manifest configured for '{target_name}'")

    prefab_env = resolver.load_bundle_env(manifest["assembly_bundle_hash"])
    parts: list[dict[str, Any]] = []
    for spec in manifest["parts"]:
        target = select_static_part_target(env, spec["target_name"], resolver)
        if target is None:
            raise SystemExit(f"Could not find static part '{spec['target_name']}' for multi-part export")
        parts.append(
            {
                **target,
                "part_name": spec["part_name"],
                "node_transform": relative_node_transform_from_prefab(
                    prefab_env,
                    manifest["assembly_root_name"],
                    spec["assembly_target_name"],
                    resolver,
                ),
            }
        )
    return parts, manifest


def iter_named_entries(value: Any):
    if value is None:
        return
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key, nested
        return
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                if "first" in item and "second" in item:
                    yield item["first"], item["second"]
                elif len(item) == 1:
                    key, nested = next(iter(item.items()))
                    yield key, nested
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                yield item[0], item[1]
            elif hasattr(item, "first") and hasattr(item, "second"):
                yield item.first, item.second


def extract_texture_pptr(value: Any) -> Any | None:
    if value is None:
        return None
    for candidate in (
        getattr(value, "m_Texture", None),
        getattr(value, "texture", None),
        value,
    ):
        if pptr_path_id(candidate):
            return candidate
    if isinstance(value, dict):
        for key in ("m_Texture", "texture"):
            candidate = value.get(key)
            if pptr_path_id(candidate):
                return candidate
    return None


def export_texture_data(
    texture: Any,
    path_id: int,
    textures_dir: Path,
    texture_cache: dict[int, dict[str, Any]],
    resolver: DependencyResolver,
) -> dict[str, Any] | None:
    if not path_id:
        return None
    if path_id in texture_cache:
        return texture_cache[path_id]
    image = getattr(texture, "image", None)
    if image is None:
        return None

    filename = f"{safe_name(object_name(texture, 'texture', resolver))}_{path_id}.png"
    path = textures_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    alpha_channel = image.getchannel("A") if "A" in image.getbands() else None
    alpha_extrema = alpha_channel.getextrema() if alpha_channel is not None else None
    has_transparency = bool(alpha_extrema is not None and alpha_extrema[0] < 255)
    info = {
        "path_id": path_id,
        "name": object_name(texture, "texture", resolver),
        "file": filename,
        "uri": f"textures/{filename}",
        "has_alpha": alpha_channel is not None,
        "has_transparency": has_transparency,
    }
    texture_cache[path_id] = info
    print(f"  exported texture {path.name}")
    return info


def export_texture(
    texture_pptr: Any,
    textures_dir: Path,
    texture_cache: dict[int, dict[str, Any]],
    resolver: DependencyResolver,
) -> dict[str, Any] | None:
    path_id = pptr_path_id(texture_pptr)
    if not path_id:
        return None
    texture = read_pptr(texture_pptr, resolver, {"Texture2D"})
    if texture is None:
        return None
    return export_texture_data(texture, path_id, textures_dir, texture_cache, resolver)


def material_texture_entries(material: Any) -> list[tuple[str, Any]]:
    saved = getattr(material, "m_SavedProperties", None)
    tex_envs = getattr(saved, "m_TexEnvs", None)
    entries = list(iter_named_entries(tex_envs) or [])
    return [(str(key), value) for key, value in entries]


def preferred_texture_info(exported_infos: list[dict[str, Any]]) -> dict[str, Any] | None:
    for property_name in TEXTURE_PROPERTY_PRIORITY:
        preferred = next((item for item in exported_infos if item["property"] == property_name), None)
        if preferred is not None:
            return preferred
    return exported_infos[0] if exported_infos else None


def merge_exported_infos(existing: list[dict[str, Any]], new_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = {(item["path_id"], item["property"]) for item in existing}
    for item in new_items:
        key = (item["path_id"], item["property"])
        if key in seen:
            continue
        existing.append(item)
        seen.add(key)
    return existing


def overlay_exported_infos(existing: list[dict[str, Any]], new_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    overridden_properties = {item["property"] for item in new_items}
    preserved = [item for item in existing if item["property"] not in overridden_properties]
    return merge_exported_infos(preserved, new_items)


def collect_material_texture_infos(
    material: Any,
    textures_dir: Path,
    texture_cache: dict[int, dict[str, Any]],
    resolver: DependencyResolver,
) -> list[dict[str, Any]]:
    infos: list[dict[str, Any]] = []
    for property_name, payload in material_texture_entries(material):
        texture_pptr = extract_texture_pptr(payload)
        info = export_texture(texture_pptr, textures_dir, texture_cache, resolver)
        if info is not None:
            infos.append({**info, "property": property_name})
    return infos


def strip_emote_suffix(name: str) -> str:
    return re.sub(r"_Emote[A-Za-z0-9]+$", "", name)


def derive_texture_hint_names(names: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = value.strip()
        if not value or value in seen:
            return
        seen.add(value)
        ordered.append(value)

    for name in names:
        if not name:
            continue
        add(name)
        stripped = strip_emote_suffix(name)
        add(stripped)
        add(stripped.replace("EyeMeshSet", "EyePlateTexture"))
        add(stripped.replace("MouthMeshSet", "MouthPlateTexture"))
        add(name.replace("EyeMeshSet", "EyePlateTexture"))
        add(name.replace("MouthMeshSet", "MouthPlateTexture"))
    return ordered


def collect_named_texture_fallbacks(
    texture_hint_names: list[str],
    lookup: BundleAssetLookup,
    textures_dir: Path,
    texture_cache: dict[int, dict[str, Any]],
    resolver: DependencyResolver,
) -> list[dict[str, Any]]:
    infos: list[dict[str, Any]] = []
    suffix_property_map = {"_C": "_BaseMap", "_N": "_BumpMap", "_X": "_ComboMap"}
    for hint_name in derive_texture_hint_names(texture_hint_names):
        for suffix, property_name in suffix_property_map.items():
            for entry in lookup.textures_by_name.get(f"{hint_name}{suffix}", []):
                info = export_texture_data(entry["data"], entry["path_id"], textures_dir, texture_cache, resolver)
                if info is not None:
                    infos.append({**info, "property": property_name})
    return infos


def collect_materials(
    material_pptrs: list[Any],
    out_dir: Path,
    resolver: DependencyResolver,
    lookup: BundleAssetLookup,
    texture_hint_names: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    materials_json: list[dict[str, Any]] = []
    images_json: list[dict[str, Any]] = []
    textures_json: list[dict[str, Any]] = []
    texture_cache: dict[int, dict[str, Any]] = {}
    material_summaries: list[dict[str, Any]] = []
    textures_dir = out_dir / "textures"

    for index, material_pptr in enumerate(material_pptrs):
        material = read_pptr(material_pptr, resolver, {"Material"})
        material_name = object_name(material, f"material_{index}", resolver) if material is not None else f"material_{index}"
        exported_infos: list[dict[str, Any]] = []
        fallback_material_name = None
        if material is not None:
            exported_infos = collect_material_texture_infos(material, textures_dir, texture_cache, resolver)
        generic_material = material_name in GENERIC_MATERIAL_NAMES or any(item["name"].startswith("Default") for item in exported_infos)

        if generic_material or preferred_texture_info(exported_infos) is None:
            for hint_name in derive_texture_hint_names(texture_hint_names):
                if hint_name == material_name:
                    continue
                fallback_entries = lookup.materials_by_name.get(hint_name, [])
                if not fallback_entries:
                    continue
                for entry in fallback_entries:
                    fallback_infos = collect_material_texture_infos(entry["data"], textures_dir, texture_cache, resolver)
                    if fallback_infos:
                        if generic_material:
                            exported_infos = overlay_exported_infos(exported_infos, fallback_infos)
                        else:
                            merge_exported_infos(exported_infos, fallback_infos)
                        fallback_material_name = hint_name
                        break
                if preferred_texture_info(exported_infos) is not None:
                    break

        fallback_texture_infos = collect_named_texture_fallbacks(texture_hint_names, lookup, textures_dir, texture_cache, resolver)
        if fallback_texture_infos and (generic_material or preferred_texture_info(exported_infos) is None):
            if generic_material:
                exported_infos = overlay_exported_infos(exported_infos, fallback_texture_infos)
            else:
                merge_exported_infos(exported_infos, fallback_texture_infos)

        base_texture_index = None
        preferred = preferred_texture_info(exported_infos)
        if preferred is not None:
            image_index = len(images_json)
            images_json.append({"uri": preferred["uri"], "name": preferred["name"]})
            textures_json.append({"source": image_index, "name": preferred["name"]})
            base_texture_index = len(textures_json) - 1

        material_json = {
            "name": material_name,
            "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 1.0},
            "doubleSided": True,
        }
        if base_texture_index is not None:
            material_json["pbrMetallicRoughness"]["baseColorTexture"] = {"index": base_texture_index}
        if preferred is not None and preferred["property"] in {"_BaseMap", "_MainTex", "_BaseColorMap", "_BaseColorTex", "_Diffuse"}:
            if preferred.get("has_transparency"):
                material_json["alphaMode"] = "BLEND"
        materials_json.append(material_json)
        summary = {"name": material_name, "textures": exported_infos}
        if fallback_material_name is not None:
            summary["fallback_material_name"] = fallback_material_name
        material_summaries.append(summary)

    if not materials_json:
        materials_json.append(
            {
                "name": "default_material",
                "pbrMetallicRoughness": {"baseColorFactor": [1.0, 1.0, 1.0, 1.0], "metallicFactor": 0.0, "roughnessFactor": 1.0},
                "doubleSided": True,
            }
        )
    return material_summaries, [materials_json, images_json, textures_json]


def resolve_bone_hierarchy(smr_bones_pptrlist, resolver: DependencyResolver) -> list[dict[str, Any]]:
    bones: list[dict[str, Any]] = []
    path_id_to_bone_idx: dict[int, int] = {}

    for index, bone_pptr in enumerate(smr_bones_pptrlist):
        transform = read_pptr(bone_pptr, resolver, {"Transform"})
        if transform is None:
            bones.append(
                {
                    "name": f"bone_{index}",
                    "path_id": pptr_path_id(bone_pptr),
                    "local_pos": [0.0, 0.0, 0.0],
                    "local_rot": [0.0, 0.0, 0.0, 1.0],
                    "local_scale": [1.0, 1.0, 1.0],
                    "parent_idx": -1,
                }
            )
            continue

        go = read_pptr(getattr(transform, "m_GameObject", None), resolver, {"GameObject"})
        name = getattr(go, "m_Name", f"bone_{index}") if go is not None else f"bone_{index}"
        lp = vec3_from_obj(getattr(transform, "m_LocalPosition", None), (0.0, 0.0, 0.0))
        lr = quat_from_obj(getattr(transform, "m_LocalRotation", None))
        ls = vec3_from_obj(getattr(transform, "m_LocalScale", None), (1.0, 1.0, 1.0))
        path_id = pptr_path_id(bone_pptr)
        bones.append(
            {
                "name": name,
                "path_id": path_id,
                "local_pos": unity_to_gltf_vec3(lp),
                "local_rot": unity_to_gltf_quat(lr),
                "local_scale": ls,
                "parent_idx": -1,
            }
        )
        path_id_to_bone_idx[path_id] = index

    for index, bone_pptr in enumerate(smr_bones_pptrlist):
        transform = read_pptr(bone_pptr, resolver, {"Transform"})
        if transform is None:
            continue
        father = getattr(transform, "m_Father", None)
        father_path_id = pptr_path_id(father)
        bones[index]["parent_idx"] = path_id_to_bone_idx.get(father_path_id, -1)

    return bones


def matrix_to_gltf_columns(matrix: Any) -> list[float]:
    rows = [
        [float(getattr(matrix, "e00", 1.0)), float(getattr(matrix, "e01", 0.0)), float(getattr(matrix, "e02", 0.0)), float(getattr(matrix, "e03", 0.0))],
        [float(getattr(matrix, "e10", 0.0)), float(getattr(matrix, "e11", 1.0)), float(getattr(matrix, "e12", 0.0)), float(getattr(matrix, "e13", 0.0))],
        [float(getattr(matrix, "e20", 0.0)), float(getattr(matrix, "e21", 0.0)), float(getattr(matrix, "e22", 1.0)), float(getattr(matrix, "e23", 0.0))],
        [float(getattr(matrix, "e30", 0.0)), float(getattr(matrix, "e31", 0.0)), float(getattr(matrix, "e32", 0.0)), float(getattr(matrix, "e33", 1.0))],
    ]
    signs = [-1.0, 1.0, 1.0, 1.0]
    converted = [[rows[row][col] * signs[row] * signs[col] for col in range(4)] for row in range(4)]
    return [converted[row][col] for col in range(4) for row in range(4)]


def columns_to_rows(values: list[float]) -> list[list[float]]:
    return [[values[col * 4 + row] for col in range(4)] for row in range(4)]


def rows_to_columns(rows: list[list[float]]) -> list[float]:
    return [rows[row][col] for col in range(4) for row in range(4)]


def invert_matrix4_columns(values: list[float]) -> list[float]:
    matrix = columns_to_rows(values)
    augmented = [row[:] + [1.0 if row_index == col_index else 0.0 for col_index in range(4)] for row_index, row in enumerate(matrix)]

    for col in range(4):
        pivot = max(range(col, 4), key=lambda row: abs(augmented[row][col]))
        if abs(augmented[pivot][col]) < 1e-12:
            raise ValueError("Matrix is not invertible")
        if pivot != col:
            augmented[col], augmented[pivot] = augmented[pivot], augmented[col]

        scale = augmented[col][col]
        augmented[col] = [value / scale for value in augmented[col]]
        for row in range(4):
            if row == col:
                continue
            factor = augmented[row][col]
            if factor:
                augmented[row] = [value - factor * augmented[col][index] for index, value in enumerate(augmented[row])]

    return rows_to_columns([row[4:] for row in augmented])


def build_gltf(
    mesh: Any,
    target: dict[str, Any],
    out_dir: Path,
    resolver: DependencyResolver,
    lookup: BundleAssetLookup,
    *,
    force_static: bool = False,
    skeleton_source: str = "local",
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    from UnityPy.helpers.MeshHelper import MeshHandler  # type: ignore

    handler = MeshHandler(mesh)
    handler.process()

    vertices = list(getattr(handler, "m_Vertices", []) or [])
    if not vertices:
        raise RuntimeError("Mesh has no vertices after MeshHandler.process()")
    normals = list(getattr(handler, "m_Normals", []) or [])
    uvs = list(getattr(handler, "m_UV0", []) or [])
    bone_indices = list(getattr(handler, "m_BoneIndices", []) or [])
    bone_weights = list(getattr(handler, "m_BoneWeights", []) or [])
    triangle_groups = list(handler.get_triangles() or [])
    if not triangle_groups:
        raise RuntimeError("Mesh has no triangle data")

    print(f"Mesh '{object_name(mesh, 'mesh', resolver)}' has {len(vertices)} vertices across {len(triangle_groups)} submesh(es)")

    material_summaries, material_payload = collect_materials(
        target["materials"],
        out_dir,
        resolver,
        lookup,
        target.get("texture_hint_names", [target["renderer_name"]]),
    )
    materials_json, images_json, textures_json = material_payload

    buffer = bytearray()
    buffer_views: list[dict[str, Any]] = []
    accessors: list[dict[str, Any]] = []

    def align_buffer() -> None:
        padding = (-len(buffer)) % 4
        if padding:
            buffer.extend(b"\x00" * padding)

    def add_accessor(raw: bytes, count: int, component_type: int, accessor_type: str, *, min_vals=None, max_vals=None, target=None):
        align_buffer()
        offset = len(buffer)
        buffer.extend(raw)
        view = {"buffer": 0, "byteOffset": offset, "byteLength": len(raw)}
        if target is not None:
            view["target"] = target
        buffer_views.append(view)
        accessor = {
            "bufferView": len(buffer_views) - 1,
            "componentType": component_type,
            "count": count,
            "type": accessor_type,
        }
        if min_vals is not None:
            accessor["min"] = min_vals
        if max_vals is not None:
            accessor["max"] = max_vals
        accessors.append(accessor)
        return len(accessors) - 1

    converted_positions = [unity_to_gltf_vec3(v) for v in vertices]
    position_bytes = b"".join(struct.pack("<3f", *value) for value in converted_positions)
    mins = [min(point[i] for point in converted_positions) for i in range(3)]
    maxs = [max(point[i] for point in converted_positions) for i in range(3)]
    position_accessor = add_accessor(position_bytes, len(converted_positions), 5126, "VEC3", min_vals=mins, max_vals=maxs, target=34962)

    if normals:
        converted_normals = [unity_to_gltf_vec3(n[:3]) for n in normals]
    else:
        converted_normals = [[0.0, 1.0, 0.0] for _ in vertices]
    normal_accessor = add_accessor(
        b"".join(struct.pack("<3f", *value) for value in converted_normals),
        len(converted_normals),
        5126,
        "VEC3",
        target=34962,
    )

    converted_uvs = []
    if uvs:
        for uv in uvs:
            converted_uvs.append([float(uv[0]), 1.0 - float(uv[1])])
    else:
        converted_uvs = [[0.0, 0.0] for _ in vertices]
    uv_accessor = add_accessor(
        b"".join(struct.pack("<2f", *value) for value in converted_uvs),
        len(converted_uvs),
        5126,
        "VEC2",
        target=34962,
    )

    joints_accessor = None
    weights_accessor = None
    can_skin = bool(not force_static and target["bones"] and bone_indices and bone_weights)
    if can_skin:
        fixed_indices: list[tuple[int, int, int, int]] = []
        fixed_weights: list[tuple[float, float, float, float]] = []
        for raw_indices, raw_weights in zip(bone_indices, bone_weights):
            indices = [max(0, int(value)) for value in (list(raw_indices)[:4] + [0, 0, 0, 0])[:4]]
            weights = [max(0.0, float(value)) for value in (list(raw_weights)[:4] + [0.0, 0.0, 0.0, 0.0])[:4]]
            total = sum(weights)
            if total > 0.0:
                weights = [value / total for value in weights]
            fixed_indices.append((indices[0], indices[1], indices[2], indices[3]))
            fixed_weights.append((weights[0], weights[1], weights[2], weights[3]))
        joints_accessor = add_accessor(
            b"".join(struct.pack("<4H", *value) for value in fixed_indices),
            len(fixed_indices),
            5123,
            "VEC4",
            target=34962,
        )
        weights_accessor = add_accessor(
            b"".join(struct.pack("<4f", *value) for value in fixed_weights),
            len(fixed_weights),
            5126,
            "VEC4",
            target=34962,
        )

    primitives: list[dict[str, Any]] = []
    total_faces = 0
    for submesh_index, triangles in enumerate(triangle_groups):
        flat_indices: list[int] = []
        for a, b, c in triangles:
            flat_indices.extend([int(c), int(b), int(a)])
        total_faces += len(triangles)
        if not flat_indices:
            continue
        index_accessor = add_accessor(
            b"".join(struct.pack("<I", value) for value in flat_indices),
            len(flat_indices),
            5125,
            "SCALAR",
            target=34963,
        )
        primitive = {
            "attributes": {
                "POSITION": position_accessor,
                "NORMAL": normal_accessor,
                "TEXCOORD_0": uv_accessor,
            },
            "indices": index_accessor,
            "material": min(submesh_index, len(materials_json) - 1),
            "mode": 4,
        }
        if joints_accessor is not None and weights_accessor is not None:
            primitive["attributes"]["JOINTS_0"] = joints_accessor
            primitive["attributes"]["WEIGHTS_0"] = weights_accessor
        primitives.append(primitive)

    nodes = [{"name": target["renderer_name"], "mesh": 0}]
    scene_nodes = [0]
    skins = []
    skin_used = False
    if can_skin:
        bones = resolve_bone_hierarchy(target["bones"], resolver)
        bind_poses = list(getattr(mesh, "m_BindPose", []) or [])
        if bones and bind_poses and len(bind_poses) >= len(bones):
            converted_bind_poses = [matrix_to_gltf_columns(bind_pose) for bind_pose in bind_poses[: len(bones)]]
            if skeleton_source == "bind-pose":
                for bone, bind_pose in zip(bones, converted_bind_poses):
                    nodes.append({"name": bone["name"], "matrix": invert_matrix4_columns(bind_pose)})
                    scene_nodes.append(len(nodes) - 1)
            else:
                for bone in bones:
                    node = {
                        "name": bone["name"],
                        "translation": bone["local_pos"],
                        "rotation": bone["local_rot"],
                        "scale": bone["local_scale"],
                    }
                    nodes.append(node)
                for bone_index, bone in enumerate(bones):
                    parent_idx = bone["parent_idx"]
                    if parent_idx >= 0:
                        nodes[parent_idx + 1].setdefault("children", []).append(bone_index + 1)
                    else:
                        nodes[0].setdefault("children", []).append(bone_index + 1)

            bind_bytes = b"".join(struct.pack("<16f", *bind_pose) for bind_pose in converted_bind_poses)
            bind_accessor = add_accessor(bind_bytes, len(bones), 5126, "MAT4")
            skins.append({"name": "skin", "joints": [index + 1 for index in range(len(bones))], "inverseBindMatrices": bind_accessor})
            nodes[0]["skin"] = 0
            skin_used = True
        else:
            print("Skinning data incomplete; exporting static mesh only")
    elif force_static:
        print("Skinning disabled by request; exporting static mesh only")
    else:
        print("Bone weights/bone list unavailable; exporting static mesh only")

    gltf_json = {
        "asset": {"version": "2.0", "generator": "hkia-extract"},
        "scene": 0,
        "scenes": [{"name": "scene", "nodes": scene_nodes}],
        "nodes": nodes,
        "meshes": [{"name": object_name(mesh, "character", resolver), "primitives": primitives}],
        "materials": materials_json,
        "buffers": [{"byteLength": len(buffer)}],
        "bufferViews": buffer_views,
        "accessors": accessors,
    }
    if images_json:
        gltf_json["images"] = images_json
        gltf_json["textures"] = textures_json
    if skins:
        gltf_json["skins"] = skins

    metadata = {
        "mesh_name": object_name(mesh, "character", resolver),
        "renderer_name": target["renderer_name"],
        "vertex_count": len(vertices),
        "submesh_count": len(primitives),
        "triangle_count": total_faces,
        "skin_used": skin_used,
        "skin_requested": bool(target["bones"] and bone_indices and bone_weights),
        "skin_forced_static": force_static,
        "skeleton_source": skeleton_source if skin_used else None,
        "bone_count": len(target["bones"]),
        "bone_index_count": len(bone_indices),
        "bone_weight_count": len(bone_weights),
        "bind_pose_count": len(getattr(mesh, "m_BindPose", []) or []),
        "materials": material_summaries,
    }
    return gltf_json, bytes(buffer), metadata


def build_multi_gltf(
    parts: list[dict[str, Any]],
    out_dir: Path,
    resolver: DependencyResolver,
    lookup: BundleAssetLookup,
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    from UnityPy.helpers.MeshHelper import MeshHandler  # type: ignore

    buffer = bytearray()
    buffer_views: list[dict[str, Any]] = []
    accessors: list[dict[str, Any]] = []
    meshes_json: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = [{"name": "character", "children": []}]
    materials_json: list[dict[str, Any]] = []
    images_json: list[dict[str, Any]] = []
    textures_json: list[dict[str, Any]] = []
    parts_metadata: list[dict[str, Any]] = []
    total_vertices = 0
    total_triangles = 0

    def align_buffer() -> None:
        padding = (-len(buffer)) % 4
        if padding:
            buffer.extend(b"\x00" * padding)

    def add_accessor(raw: bytes, count: int, component_type: int, accessor_type: str, *, min_vals=None, max_vals=None, target=None):
        align_buffer()
        offset = len(buffer)
        buffer.extend(raw)
        view = {"buffer": 0, "byteOffset": offset, "byteLength": len(raw)}
        if target is not None:
            view["target"] = target
        buffer_views.append(view)
        accessor = {
            "bufferView": len(buffer_views) - 1,
            "componentType": component_type,
            "count": count,
            "type": accessor_type,
        }
        if min_vals is not None:
            accessor["min"] = min_vals
        if max_vals is not None:
            accessor["max"] = max_vals
        accessors.append(accessor)
        return len(accessors) - 1

    for part in parts:
        mesh = part["mesh"]
        handler = MeshHandler(mesh)
        handler.process()

        vertices = list(getattr(handler, "m_Vertices", []) or [])
        if not vertices:
            raise RuntimeError(f"Mesh {object_name(mesh, 'mesh', resolver)} has no vertices")
        normals = list(getattr(handler, "m_Normals", []) or [])
        uvs = list(getattr(handler, "m_UV0", []) or [])
        triangle_groups = list(handler.get_triangles() or [])
        if not triangle_groups:
            raise RuntimeError(f"Mesh {object_name(mesh, 'mesh', resolver)} has no triangle data")

        print(f"Adding multi-part mesh '{object_name(mesh, 'mesh', resolver)}' ({part['part_name']})")

        material_summaries, material_payload = collect_materials(
            part["materials"],
            out_dir,
            resolver,
            lookup,
            part.get("texture_hint_names", [part["renderer_name"]]),
        )
        local_materials, local_images, local_textures = material_payload
        image_offset = len(images_json)
        texture_offset = len(textures_json)
        material_offset = len(materials_json)

        images_json.extend(copy.deepcopy(local_images))
        for texture in local_textures:
            texture_json = copy.deepcopy(texture)
            texture_json["source"] = texture_json.get("source", 0) + image_offset
            textures_json.append(texture_json)
        for material in local_materials:
            material_json = copy.deepcopy(material)
            base_color = material_json.get("pbrMetallicRoughness", {}).get("baseColorTexture")
            if base_color is not None:
                base_color["index"] += texture_offset
            materials_json.append(material_json)

        converted_positions = [unity_to_gltf_vec3(v) for v in vertices]
        position_bytes = b"".join(struct.pack("<3f", *value) for value in converted_positions)
        mins = [min(point[i] for point in converted_positions) for i in range(3)]
        maxs = [max(point[i] for point in converted_positions) for i in range(3)]
        position_accessor = add_accessor(position_bytes, len(converted_positions), 5126, "VEC3", min_vals=mins, max_vals=maxs, target=34962)

        if normals:
            converted_normals = [unity_to_gltf_vec3(n[:3]) for n in normals]
        else:
            converted_normals = [[0.0, 1.0, 0.0] for _ in vertices]
        normal_accessor = add_accessor(
            b"".join(struct.pack("<3f", *value) for value in converted_normals),
            len(converted_normals),
            5126,
            "VEC3",
            target=34962,
        )

        if uvs:
            converted_uvs = [[float(uv[0]), 1.0 - float(uv[1])] for uv in uvs]
        else:
            converted_uvs = [[0.0, 0.0] for _ in vertices]
        uv_accessor = add_accessor(
            b"".join(struct.pack("<2f", *value) for value in converted_uvs),
            len(converted_uvs),
            5126,
            "VEC2",
            target=34962,
        )

        primitives: list[dict[str, Any]] = []
        part_triangles = 0
        for submesh_index, triangles in enumerate(triangle_groups):
            flat_indices: list[int] = []
            for a, b, c in triangles:
                flat_indices.extend([int(c), int(b), int(a)])
            part_triangles += len(triangles)
            if not flat_indices:
                continue
            index_accessor = add_accessor(
                b"".join(struct.pack("<I", value) for value in flat_indices),
                len(flat_indices),
                5125,
                "SCALAR",
                target=34963,
            )
            primitives.append(
                {
                    "attributes": {
                        "POSITION": position_accessor,
                        "NORMAL": normal_accessor,
                        "TEXCOORD_0": uv_accessor,
                    },
                    "indices": index_accessor,
                    "material": material_offset + min(submesh_index, len(local_materials) - 1),
                    "mode": 4,
                }
            )

        mesh_index = len(meshes_json)
        meshes_json.append({"name": object_name(mesh, part["renderer_name"], resolver), "primitives": primitives})

        node_index = len(nodes)
        node = {"name": part["renderer_name"], "mesh": mesh_index}
        node_transform = part.get("node_transform", {})
        if "matrix" in node_transform:
            node["matrix"] = node_transform["matrix"]
        else:
            node.update({key: value for key, value in node_transform.items() if key in {"translation", "rotation", "scale"}})
        nodes.append(node)
        nodes[0]["children"].append(node_index)

        total_vertices += len(vertices)
        total_triangles += part_triangles
        parts_metadata.append(
            {
                "part_name": part["part_name"],
                "renderer_name": part["renderer_name"],
                "mesh_name": object_name(mesh, part["renderer_name"], resolver),
                "vertex_count": len(vertices),
                "triangle_count": part_triangles,
                "transform": node_transform,
                "materials": material_summaries,
            }
        )

    gltf_json = {
        "asset": {"version": "2.0", "generator": "hkia-extract"},
        "scene": 0,
        "scenes": [{"name": "scene", "nodes": [0]}],
        "nodes": nodes,
        "meshes": meshes_json,
        "materials": materials_json,
        "buffers": [{"byteLength": len(buffer)}],
        "bufferViews": buffer_views,
        "accessors": accessors,
    }
    if images_json:
        gltf_json["images"] = images_json
        gltf_json["textures"] = textures_json

    metadata = {
        "mesh_name": "multipart_character",
        "renderer_name": "multipart_character",
        "vertex_count": total_vertices,
        "submesh_count": sum(len(mesh["primitives"]) for mesh in meshes_json),
        "triangle_count": total_triangles,
        "skin_used": False,
        "skin_requested": False,
        "skin_forced_static": True,
        "skeleton_source": None,
        "part_count": len(parts),
        "parts": parts_metadata,
    }
    return gltf_json, bytes(buffer), metadata


def collect_animation_names(env, target_name: str) -> list[str]:
    target_lower = target_name.lower()
    names: list[str] = []
    for obj in env.objects:
        if obj.type.name != "AnimationClip":
            continue
        try:
            data = obj.read()
            name = object_name(data)
        except Exception:
            continue
        if not name:
            continue
        if target_lower in name.lower() or len(names) < 10:
            names.append(name)
    return sorted(dict.fromkeys(names))


def update_index(entry: dict[str, Any]) -> None:
    index_path = EXTRACTED / "characters" / "index.json"
    payload = {"characters": []}
    if index_path.exists():
        try:
            payload = json_mod.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {"characters": []}
    characters = [item for item in payload.get("characters", []) if item.get("slug") != entry["slug"]]
    characters.append(entry)
    characters.sort(key=lambda item: item.get("name", "").lower())
    payload["characters"] = characters
    write_json(index_path, payload)


def resolve_bundle_path(bundle_hash: str) -> Path:
    bundle_hash = bundle_hash.strip()
    candidate = BUNDLES_ROOT / (bundle_hash if bundle_hash.lower().endswith(".bundle") else f"{bundle_hash}.bundle")
    if not candidate.is_file():
        raise SystemExit(f"Bundle not found: {candidate}")
    return candidate


def clear_previous_textures(textures_dir: Path) -> None:
    if textures_dir.is_dir():
        shutil.rmtree(textures_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract a Hello Kitty Island Adventure character mesh into GLB + PNG textures.")
    parser.add_argument("--name", required=True, help="Character name to search for, e.g. cinnamoroll")
    parser.add_argument("--bundle-hash", help="Optional specific bundle hash (with or without .bundle)")
    parser.add_argument("--slug", help="Optional output slug. Defaults to a safe version of --name.")
    parser.add_argument("--multi", action="store_true", help="Assemble a configured multi-part character GLB instead of exporting a single best-matching mesh.")
    parser.add_argument("--no-skin", action="store_true", help="Export geometry without skin, joints, weights, or inverse bind matrices.")
    parser.add_argument("--inventory", type=Path, default=REPORTS / "bundle_inventory.csv", help="Inventory CSV used to resolve external Mesh, Material, and Texture2D pointers.")
    parser.add_argument(
        "--skeleton-source",
        choices=("local", "bind-pose"),
        default="bind-pose",
        help="How to write joint node transforms for skinned exports.",
    )
    args = parser.parse_args()

    if not BUNDLES_ROOT.is_dir():
        raise SystemExit(f"Bundles directory not found: {BUNDLES_ROOT}")

    UnityPy = load_unitypy()
    resolver = DependencyResolver(UnityPy, args.inventory)
    if args.bundle_hash:
        bundle_path = resolve_bundle_path(args.bundle_hash)
        candidates = [{"bundle": bundle_path, "matches": []}]
    else:
        candidates = find_matching_bundles(args.name, UnityPy)
        if not candidates:
            raise SystemExit(f"No bundle contained an object name matching '{args.name}'")

    chosen = candidates[0]
    bundle_path = chosen["bundle"]
    env = load_env(bundle_path, UnityPy)
    lookup = BundleAssetLookup(env, resolver)
    target = None
    parts = None
    manifest = None
    if args.multi:
        parts, manifest = resolve_multi_part_targets(env, args.name, resolver)
    else:
        target = select_character_target(env, args.name, resolver)
        if target is None:
            raise SystemExit(f"No suitable renderer found in bundle {bundle_path.name}")

    default_slug = f"{args.name}_full" if args.multi else args.name
    slug = safe_name((args.slug or default_slug).lower())
    out_dir = EXTRACTED / "characters" / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    clear_previous_textures(out_dir / "textures")

    if args.multi:
        gltf_json, bin_data, metadata = build_multi_gltf(parts or [], out_dir, resolver, lookup)
    else:
        mesh = target["mesh"]
        gltf_json, bin_data, metadata = build_gltf(
            mesh,
            target,
            out_dir,
            resolver,
            lookup,
            force_static=args.no_skin,
            skeleton_source=args.skeleton_source,
        )
    glb_bytes = pack_glb(gltf_json, bin_data)
    glb_path = out_dir / "character.glb"
    glb_path.write_bytes(glb_bytes)
    print(f"Wrote {glb_path}")

    animation_names = collect_animation_names(env, args.name)
    metadata_path = out_dir / "metadata.json"
    metadata.update(
        {
            "bundle": bundle_path.name,
            "bundle_matches": chosen.get("matches", []),
            "animation_names": animation_names,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "glb": str(glb_path).replace("\\", "/"),
        }
    )
    if args.multi:
        metadata["assembly_bundle"] = f"{manifest['assembly_bundle_hash']}.bundle"
        metadata["assembly_root_name"] = manifest["assembly_root_name"]
    write_json(metadata_path, metadata)
    print(f"Wrote {metadata_path}")

    if args.multi:
        display_name = f"{args.name} (multi-part)"
    else:
        display_name = target["renderer_name"]
        if args.no_skin:
            display_name = f"{display_name} (static control)"
        elif args.skeleton_source == "bind-pose" and args.slug:
            display_name = f"{display_name} (bind-pose skeleton)"

    entry = {
        "name": display_name,
        "searchName": args.name,
        "slug": slug,
        "bundle": bundle_path.name,
        "glb": f"/extracted/characters/{slug}/character.glb",
        "metadata": f"/extracted/characters/{slug}/metadata.json",
        "textureCount": len(list((out_dir / 'textures').glob('*.png'))),
        "animationNames": animation_names,
        "vertexCount": metadata["vertex_count"],
        "triangleCount": metadata["triangle_count"],
    }
    update_index(entry)
    print(f"Updated {EXTRACTED / 'characters' / 'index.json'}")
    print("Extraction complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
