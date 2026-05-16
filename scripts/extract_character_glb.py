from __future__ import annotations

import argparse
import json as json_mod
import math
import re
import shutil
import struct
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import BUNDLES_ROOT, EXTRACTED, iter_bundles, load_unitypy, safe_name, write_json

TEXTURE_PROPERTY_PRIORITY = [
    "_MainTex",
    "_BaseMap",
    "_BaseColorMap",
    "_BaseColorTex",
    "_Diffuse",
]


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


def find_static_renderers(env, target_name: str) -> list[dict[str, Any]]:
    mesh_filters: dict[int, Any] = {}
    mesh_filter_names: dict[int, str] = {}
    for obj in env.objects:
        if obj.type.name != "MeshFilter":
            continue
        try:
            data = obj.read()
            go = read_pptr(getattr(data, "m_GameObject", None))
            if go is None:
                continue
            mesh_filters[getattr(go, "path_id", 0)] = getattr(data, "m_Mesh", None)
            mesh_filter_names[getattr(go, "path_id", 0)] = getattr(go, "m_Name", "")
        except Exception:
            continue

    candidates: list[dict[str, Any]] = []
    target_lower = target_name.lower()
    for obj in env.objects:
        if obj.type.name != "MeshRenderer":
            continue
        try:
            data = obj.read()
            go = read_pptr(getattr(data, "m_GameObject", None))
            if go is None:
                continue
            go_path_id = getattr(go, "path_id", 0)
            mesh_pptr = mesh_filters.get(go_path_id)
            mesh = read_pptr(mesh_pptr)
            if mesh is None:
                continue
            name = getattr(go, "m_Name", "") or mesh_filter_names.get(go_path_id, "") or "static_mesh"
            score = 5 if target_lower in name.lower() else 0
            candidates.append(
                {
                    "kind": "static",
                    "renderer": data,
                    "renderer_name": name,
                    "mesh": mesh,
                    "materials": list(getattr(data, "m_Materials", []) or []),
                    "bones": [],
                    "score": score,
                }
            )
        except Exception:
            continue
    return candidates


def select_character_target(env, target_name: str) -> dict[str, Any] | None:
    target_lower = target_name.lower()
    target_tokens = [token for token in re.split(r"[^a-z0-9]+", target_lower) if token]
    candidates: list[dict[str, Any]] = []

    for obj in env.objects:
        if obj.type.name != "SkinnedMeshRenderer":
            continue
        try:
            data = obj.read()
            mesh = read_pptr(getattr(data, "m_Mesh", None))
            if mesh is None:
                continue
            go = read_pptr(getattr(data, "m_GameObject", None))
            renderer_name = getattr(go, "m_Name", "") if go is not None else object_name(data, "skinned_mesh")
            mesh_name = object_name(mesh)
            material_count = len(getattr(data, "m_Materials", []) or [])
            bone_count = len(getattr(data, "m_Bones", []) or [])
            combined = " ".join(part for part in [renderer_name, mesh_name] if part)
            score = 0
            if target_lower in renderer_name.lower():
                score += 10
            if mesh_name and target_lower in mesh_name.lower():
                score += 6
            if target_lower in combined.lower():
                score += 4
            combined_lower = combined.lower()
            if target_tokens and all(token in combined_lower for token in target_tokens):
                score += 40
            score += min(bone_count, 32)
            score += material_count
            candidates.append(
                {
                    "kind": "skinned",
                    "renderer": data,
                    "renderer_name": renderer_name or mesh_name or "character",
                    "mesh": mesh,
                    "materials": list(getattr(data, "m_Materials", []) or []),
                    "bones": list(getattr(data, "m_Bones", []) or []),
                    "score": score,
                }
            )
        except Exception as exc:
            print(f"skip renderer {obj.path_id}: {exc}")

    candidates.extend(find_static_renderers(env, target_name))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item["score"], item["renderer_name"]))
    chosen = candidates[0]
    print(
        f"Selected {chosen['kind']} renderer '{chosen['renderer_name']}' "
        f"with {len(chosen['materials'])} material(s) and {len(chosen['bones'])} bone(s)"
    )
    return chosen


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


def export_texture(texture_pptr: Any, textures_dir: Path, texture_cache: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    path_id = pptr_path_id(texture_pptr)
    if not path_id:
        return None
    if path_id in texture_cache:
        return texture_cache[path_id]

    texture = read_pptr(texture_pptr)
    if texture is None:
        return None
    image = getattr(texture, "image", None)
    if image is None:
        return None

    filename = f"{safe_name(object_name(texture, 'texture'))}_{path_id}.png"
    path = textures_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    info = {"path_id": path_id, "name": object_name(texture, "texture"), "file": filename, "uri": f"textures/{filename}"}
    texture_cache[path_id] = info
    print(f"  exported texture {path.name}")
    return info


def material_texture_entries(material: Any) -> list[tuple[str, Any]]:
    saved = getattr(material, "m_SavedProperties", None)
    tex_envs = getattr(saved, "m_TexEnvs", None)
    entries = list(iter_named_entries(tex_envs) or [])
    return [(str(key), value) for key, value in entries]


def collect_materials(material_pptrs: list[Any], out_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    materials_json: list[dict[str, Any]] = []
    images_json: list[dict[str, Any]] = []
    textures_json: list[dict[str, Any]] = []
    texture_cache: dict[int, dict[str, Any]] = {}
    material_summaries: list[dict[str, Any]] = []
    textures_dir = out_dir / "textures"

    for index, material_pptr in enumerate(material_pptrs):
        material = read_pptr(material_pptr)
        material_name = object_name(material, f"material_{index}") if material is not None else f"material_{index}"
        exported_infos: list[dict[str, Any]] = []
        if material is not None:
            for property_name, payload in material_texture_entries(material):
                texture_pptr = extract_texture_pptr(payload)
                info = export_texture(texture_pptr, textures_dir, texture_cache)
                if info is not None:
                    info = {**info, "property": property_name}
                    exported_infos.append(info)

        base_texture_index = None
        preferred = None
        for property_name in TEXTURE_PROPERTY_PRIORITY:
            preferred = next((item for item in exported_infos if item["property"] == property_name), None)
            if preferred is not None:
                break
        if preferred is None and exported_infos:
            preferred = exported_infos[0]
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
        materials_json.append(material_json)
        material_summaries.append({"name": material_name, "textures": exported_infos})

    if not materials_json:
        materials_json.append(
            {
                "name": "default_material",
                "pbrMetallicRoughness": {"baseColorFactor": [1.0, 1.0, 1.0, 1.0], "metallicFactor": 0.0, "roughnessFactor": 1.0},
                "doubleSided": True,
            }
        )
    return material_summaries, [materials_json, images_json, textures_json]


def resolve_bone_hierarchy(smr_bones_pptrlist) -> list[dict[str, Any]]:
    bones: list[dict[str, Any]] = []
    path_id_to_bone_idx: dict[int, int] = {}

    for index, bone_pptr in enumerate(smr_bones_pptrlist):
        transform = read_pptr(bone_pptr)
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

        go = read_pptr(getattr(transform, "m_GameObject", None))
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
        transform = read_pptr(bone_pptr)
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

    print(f"Mesh '{object_name(mesh, 'mesh')}' has {len(vertices)} vertices across {len(triangle_groups)} submesh(es)")

    material_summaries, material_payload = collect_materials(target["materials"], out_dir)
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
        bones = resolve_bone_hierarchy(target["bones"])
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
        "meshes": [{"name": object_name(mesh, "character"), "primitives": primitives}],
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
        "mesh_name": object_name(mesh, "character"),
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
    parser.add_argument("--no-skin", action="store_true", help="Export geometry without skin, joints, weights, or inverse bind matrices.")
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
    target = select_character_target(env, args.name)
    if target is None:
        raise SystemExit(f"No suitable renderer found in bundle {bundle_path.name}")

    mesh = target["mesh"]
    slug = safe_name((args.slug or args.name).lower())
    out_dir = EXTRACTED / "characters" / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    clear_previous_textures(out_dir / "textures")

    gltf_json, bin_data, metadata = build_gltf(mesh, target, out_dir, force_static=args.no_skin, skeleton_source=args.skeleton_source)
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
    write_json(metadata_path, metadata)
    print(f"Wrote {metadata_path}")

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
