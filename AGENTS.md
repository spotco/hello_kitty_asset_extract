# HKIA Asset Extraction — Agent Notes

## Project Goal
Extract character assets (starting with Cinnamoroll) from Hello Kitty Island Adventure,
export them as GLB files, and display/animate them in a Three.js web viewer. Modeled after
`E:\dev\sword_assets_extract`.

---

## Environment

| Item | Value |
|---|---|
| Game path | `C:\Program Files (x86)\Steam\steamapps\common\Hello Kitty Island Adventure` |
| Bundles root | `Hello Kitty_Data\StreamingAssets\aa\StandaloneWindows64\*.bundle` |
| Total bundles | **6,863** |
| Unity version | `2021.3.56f2` |
| Python lib | UnityPy 1.25.0 (already installed) |
| Web framework | Three.js r164 (vendor files copied from sword project) |

**Critical UnityPy setup** (must do before any load):
```python
import warnings, UnityPy
warnings.filterwarnings("ignore")
UnityPy.config.FALLBACK_UNITY_VERSION = '2021.3.56f2'
```

**Never write to the game folder.** All output goes to `E:\dev\hello_kitty_asset_extract\`.

---

## What Has Been Built

### Python scripts (`scripts/`)
| File | Purpose | Status |
|---|---|---|
| `common.py` | Shared config, `load_unitypy()`, `iter_bundles()`, `write_csv()` | ✅ Done |
| `inventory_assets.py` | Scans bundles → `reports/bundle_inventory.csv` + `bundle_summary.txt` | ✅ Done & fast |
| `extract_character_glb.py` | Exports single-part or multi-part GLBs + PNG textures | ✅ Single-part textured exports work; ✅ current Cinnamoroll multi-part assembly works |

### Web viewer (`web_character_viewer/`)
| File | Purpose | Status |
|---|---|---|
| `index.html` | Import-map shell for Three.js | ✅ Done |
| `app.js` | OrbitControls, GLTFLoader, AnimationMixer, skeleton helper, sidebar | ✅ Written, **untested** |
| `styles.css` | Dark theme | ✅ Done |
| `vendor/three.module.js` | Three.js r164 | ✅ Copied |
| `vendor/GLTFLoader.js` | Three.js r164 GLTFLoader | ✅ Downloaded |
| `vendor/OrbitControls.js` | Three.js r164 OrbitControls | ✅ Downloaded |
| `utils/BufferGeometryUtils.js` | Minimal local `toTrianglesDrawMode` helper for GLTFLoader import | ✅ Added |

### Other
| File | Purpose | Status |
|---|---|---|
| `server.py` | HTTP server port 5173; `/api/characters` GET, `/api/extract` POST | ✅ Smoke-tested |
| `extracted/characters/index.json` | Character manifest | ✅ Contains current Cinnamoroll parts |
| `requirements.txt` | `UnityPy>=1.22.0`, `Pillow>=10.0.0` | ✅ Done |

---

## Key Technical Notes

### MeshHandler API (UnityPy 1.25.0)
```python
from UnityPy.helpers.MeshHelper import MeshHandler
handler = MeshHandler(mesh_obj)
handler.process()
# handler.m_Vertices, .m_Normals, .m_UV0
# handler.m_BoneIndices, .m_BoneWeights
# handler.get_triangles() → list of submesh tri lists [(a,b,c),...]
```

### Unity → GLTF coordinate conversion
- `gltf_x = -unity_x`, `gltf_y = unity_y`, `gltf_z = unity_z`
- Flip triangle winding: swap `a` and `c`
- UV: `gltf_v = 1.0 - unity_v`
- BindPose: Unity is row-major, GLTF wants column-major → transpose to column-major.
- BindPose also needs Unity-to-glTF handedness conversion. Current exporter mirrors X on both
  matrix rows/columns before writing column-major.

### Skeleton / Rest Pose
- Static no-skin control export for `MiniStyle_Head_Cinnamoroll` looks correct.
- `--skeleton-source local` is **not** correct for Cinnamoroll head: it is rotated ~90 degrees
  compared with the static control.
- `--skeleton-source bind-pose` matches the static control and is now the exporter default.
- Confirmed diagnosis: the renderer's `m_Bones` list does not include a complete ancestor
  hierarchy. Preserving Unity local transforms while making missing-parent bones scene roots
  changes the skeleton frame.
- Current default approach: derive joint node matrices by inverting the converted inverse bind
  matrices. This makes rest pose satisfy `jointWorld * inverseBindMatrix == identity`.
- Keep `--skeleton-source local` only as a debug mode for future full-hierarchy reconstruction.
- Current limitation: bind-pose skeleton is good for rest-pose rendering. Real animation retargeting
  may still need a complete/semantic humanoid hierarchy.

### Transparency / face plates
- Eye and mouth plate textures export as PNG RGBA and do contain real transparency.
- glTF materials must explicitly set `alphaMode: "BLEND"` when the chosen base-color texture has
  non-opaque alpha; otherwise Three.js renders the face plates as opaque quads.
- The current exporter detects transparency from the exported base-color PNG and sets
  `alphaMode: "BLEND"` for those materials.

### Animations
- HKIA uses **Humanoid MuscleClip** format
- `m_PositionCurves` / `m_RotationCurves` / `m_ScaleCurves` are **empty** for humanoid clips
- Actual data lives in `m_MuscleClip` — complex to decode; **deferred**
- GLBs exported now will have no embedded animations (skeleton present, no clips)

### MonoBehaviour hang
- Calling `obj.read()` on `MonoBehaviour` objects **hangs indefinitely** (UnityPy tries to resolve C# type)
- **Do not include MonoBehaviour or TextAsset in `NAMED_TYPES`**
- Current safe `NAMED_TYPES`:
  `Mesh, AnimationClip, AnimatorController, Material, Texture2D, Sprite, GameObject, SkinnedMeshRenderer, Animator, AssetBundle`

### Bundle scanning performance
- `inventory_assets.py` runs at ~80–120 bundles/sec with the NAMED_TYPES filter
- Full scan of 6,863 bundles completed in ~144 seconds on 2026-05-15
- Outputs now resolve to repo-root `reports/` via `scripts/common.py`

### Current Cinnamoroll Findings
- Full inventory lives at `reports/bundle_inventory.csv`.
- Base/prefab-style Cinnamoroll bundle: `222c0db735425c4fa287cca209345a62.bundle`
  - Contains `GameObject,Cinnamoroll`, `MiniStyle_Head_Cinnamoroll(Clone)`,
    `MiniStyle_Tail_Cinnamoroll(Clone)`, eye/mouth clone objects, and
    `CTIntro_Cinnamoroll`.
  - Many `SkinnedMeshRenderer.m_Mesh` pointers reference external CAB/dependency bundles;
    direct `m_Mesh.read()` fails unless the mesh exists in the loaded bundle.
- Usable Cinnamoroll mesh/source bundle: `f75e6233b13254a0a5e316a96d0d3114.bundle`
  - Contains `MiniStyle_Head_Cinnamoroll`, `MiniStyle_Tail_Cinnamoroll`, eye/mouth meshes,
    textures, materials, bones, and animation clip names.
- Current single-part exports:
    - `extracted/characters/cinnamoroll/character.glb` = head mesh, 958 vertices, skinned
      - Regenerated with `skeleton_source: bind-pose`; this now matches the static control orientation.
    - `extracted/characters/tail_cinnamoroll/character.glb` = tail mesh, 107 vertices, static fallback
    - `extracted/characters/cinnamoroll_head_static/character.glb` = head mesh, 958 vertices,
      forced static/no-skin control export
    - `extracted/characters/cinnamoroll_head_bind_pose/character.glb` = head mesh, 958 vertices,
      skinned with joint node matrices derived from inverted bind poses
- Resolved skin/rest-pose issue:
  - Original skinned head looked badly deformed; after handedness-converted bind matrices it was
    close but rotated ~90 degrees.
  - Static control and bind-pose skeleton export match.
  - Default `cinnamoroll` export now uses `skeleton_source: bind-pose`.
- Body source is still unresolved:
  - `MiniStyle_BodyShape_Cinnamoroll` in `f75e6233...` is a GameObject using a `MeshFilter`
    whose mesh pointer resolves to path id `-4862095986763463643`.
  - That path id exists as `Mesh,MiniStyle_BodyShape_Default` in
    `fd46aab3373a111f81315c76691b8c5c.bundle`.
  - Need to decide whether this default body mesh plus Cinnamoroll material/textures is the
    correct body for the mini character.
- Texture/material status:
  - Shared material pointer `6824069125264728142` resolves to `Material,Lit` in
    `680e794bd7287dcd6719ff90cfbf9cc3.bundle`.
  - Cinnamoroll texture objects exist in `f75e6233...`, including:
    `MiniStyle_Head_Cinnamoroll_C`, `_N`, `_X`, `MiniStyle_Tail_Cinnamoroll_C`,
    `MiniStyle_BodyShape_Cinnamoroll_C`, and eye/mouth plate textures.
  - Dependency/material resolution is now implemented in the exporter.
  - When shared materials such as `Lit` or `EmoteMesh_FacePlate` are textureless or generic,
    the exporter falls back to matching bundle-local character materials or plate textures.
  - Current textured single-part exports work for head, tail, body, eyes, and mouth.
  - Eye and mouth exports specifically use `Shared_EyePlateTexture_Cinnamoroll_C` and
    `Shared_MouthPlateTexture_Cinnamoroll_C` as `_BaseMap`, with glTF alpha blending enabled.
 - Multi-part export status:
   - `python scripts\extract_character_glb.py --name cinnamoroll --bundle-hash f75e6233b13254a0a5e316a96d0d3114 --multi --slug cinnamoroll_full`
     now writes one GLB containing body, head, tail, eyes, and mouth as separate nodes.
   - Body/head/tail geometry comes from the mesh source bundle `f75e6233...`, but final part placement
     must come from the assembled prefab bundle `222c0db...`.
   - Face parts (`Shared_EyeMeshSet_Cinnamoroll(Clone)`, `Shared_MouthMeshSet_Cinnamoroll(Clone)`)
     are not positioned correctly if exported using only the standalone source-bundle transforms:
     they need prefab-relative transforms from the assembled `Cinnamoroll` hierarchy.
   - Current fix: multi-part export computes node matrices relative to prefab root `Cinnamoroll`
     using the assembled clone hierarchy from `222c0db...`; this correctly places eyes and mouth
     on the head.

---

## Immediate Next Steps: Skin/Skeleton First, Then Multi-Part Rendering

End goal: the web viewer should let a player load Cinnamoroll as one complete character,
then preview available animations. Multi-part assembly is secondary until the skin/rest pose
is correct; otherwise every assembled character will inherit the same deformation bug.

### 1. Diagnose and fix current skinned-head deformation
Before assembling all parts, determine whether the raw mesh is valid and the skin/rest pose is wrong.

Do these control exports:
- Export `MiniStyle_Head_Cinnamoroll` as a **static mesh** with no `skin`, `JOINTS_0`,
  `WEIGHTS_0`, or inverse bind matrices.
- Export the same head with skinning disabled but using the same coordinate conversion.
- Compare the static export in the viewer against the current skinned export.

Interpretation:
- If the static head looks correct, fix skeleton/rest-pose export before multi-part assembly.
- If the static head is also distorted, fix vertex coordinate/index/submesh conversion first.

Specific skinning fixes to inspect:
- Bone node local `translation`, `rotation`, and `scale` conversion from Unity to glTF.
- Whether inverse bind matrices need more than a simple transpose because of handedness conversion.
- Whether the exported joints match Unity's `m_Bones` order exactly.
- Whether the head renderer depends on parent bones outside the renderer's immediate bundle hierarchy.
- Whether the bind shape matrix or mesh transform needs to be applied before export.
- Whether bone node transforms should be derived from inverted bind poses instead of Unity
  `m_LocalPosition` / `m_LocalRotation` when ancestors are missing from `m_Bones`.

Deliverable for this step:
- ✅ `extracted/characters/cinnamoroll_head_static/character.glb` static control export exists.
- ✅ `metadata.json` records `skin_used`, `skin_requested`, `skin_forced_static`,
  `bone_count`, `bone_index_count`, `bone_weight_count`, and `bind_pose_count`.
- ✅ Skinned head was regenerated with handedness-converted inverse bind matrices.
- ✅ `--skeleton-source bind-pose` confirmed to match the static control and is now the exporter default.
- Confirmed visual comparison: default skinned export and static control have matching orientation.
- Current comparison: default skinned export should now match static orientation. Keep
  `--skeleton-source local` only as a debug path for investigating full hierarchy reconstruction.
- Added test export mode `--skeleton-source bind-pose`. It writes each joint node transform as
  the inverse of its converted inverse bind matrix and places all joints in the scene as roots.
  This should make `jointWorld * inverseBindMatrix` identity at rest and tests whether missing
  ancestor transforms are the source of the 90-degree offset.
- Decision: geometry conversion is acceptable for the head control; the prior issue was
  skeleton/rest-pose export from incomplete local hierarchy.

### 2. Make Cinnamoroll's skinned single-part export correct
Status:
- ✅ Default skinned head export matches static/rest orientation using `--skeleton-source bind-pose`.
- ✅ Metadata records bone count, bind pose count, skin mode, and bind-pose conversion mode.
- Keep skeleton helper usable for inspecting bone locations.
- Keep `--skeleton-source local` only for debugging full hierarchy reconstruction.

### 3. Plan animation preview path
Animation preview is the final target, so keep these constraints visible while fixing skeletons:
- HKIA humanoid animation clips use `m_MuscleClip`; regular transform curves are empty.
- First animation milestone is **not** full MuscleClip decoding. First expose clip names and
  confirm the rest skeleton is valid.
- Second milestone can be a test animation generated from known bones (for example ear/tail
  procedural preview) to verify Three.js playback wiring.
- Only then investigate real `m_MuscleClip` decoding or retargeting from Unity humanoid data.

### 4. Inventory the Cinnamoroll source bundle structure
Create a focused inspection script first, then run it:
```powershell
cd E:\dev\hello_kitty_asset_extract
python scripts/inspect_cinnamoroll_parts.py
```
Goal: map these parts precisely:
- `MiniStyle_BodyShape_Cinnamoroll`
- `MiniStyle_Head_Cinnamoroll`
- `MiniStyle_Tail_Cinnamoroll`
- `Shared_EyeMeshSet_Cinnamoroll`
- `Shared_MouthMeshSet_Cinnamoroll`
- `Shared_EyePlateTexture_Cinnamoroll`
- `Shared_MouthPlateTexture_Cinnamoroll`

Record for each part:
- GameObject path id
- Transform path id, parent path id, local position/rotation/scale
- MeshFilter or SkinnedMeshRenderer path id
- Mesh pointer `(file_id, path_id)` and resolved bundle if external
- Material pointers and resolved bundle if external

### 5. Build dependency resolution
Current blocker: UnityPy PPtrs with nonzero `m_FileID` try to load CAB names that are not present as direct files, but the inventory can map `path_id → bundle_hash`.

Implement a helper in `extract_character_glb.py` or a new shared module:
- Load `reports/bundle_inventory.csv` once.
- Build `path_id -> bundle_hash` for `Mesh`, `Material`, and `Texture2D`.
- If `read_pptr()` fails with `FileNotFoundError`, resolve by path id from inventory, load that bundle, and find/read the object with the matching path id.
- Cache loaded dependency envs by bundle hash.

### 6. Texture the meshes
This is a required milestone, not just polish.

Plan:
- Extend dependency resolution to `Material` and `Texture2D`, not only meshes.
- Inspect material objects for texture slots/properties (`_MainTex`, `_BaseMap`, `_Diffuse`,
  `_BaseColorMap`, `_BaseColorTex`) and map them to glTF `baseColorTexture`.
- Export relevant PNGs into each character output's `textures/` directory.
- For Cinnamoroll, verify at least these diffuse/base-color textures are found and assigned:
  - `MiniStyle_Head_Cinnamoroll_C`
  - `MiniStyle_Tail_Cinnamoroll_C`
  - `MiniStyle_BodyShape_Cinnamoroll_C`
  - `Shared_EyePlateTexture_Cinnamoroll_C`
  - `Shared_MouthPlateTexture_Cinnamoroll_C`
- Normal (`_N`) and extra (`_X`) maps can be recorded in metadata first; wire them to glTF only
  after base color works.
- Viewer acceptance check: texture count in `index.json` is nonzero and the model is no longer
  plain white.
Status:
- ✅ Base-color texture export works for the current single-part head/tail/body/eye/mouth tests.
- ✅ Face-plate transparency is respected in the current exporter.

### 7. Export a multi-mesh GLB
Extend or create an exporter that accepts a character part manifest, for example:
```powershell
python scripts/extract_character_glb.py --name cinnamoroll --bundle-hash f75e6233b13254a0a5e316a96d0d3114 --multi
```
Target output:
- `extracted/characters/cinnamoroll_full/character.glb`
- One scene containing body, head, tail, eyes, and mouth as separate mesh nodes
- Shared skeleton if practical; otherwise keep static mesh nodes correctly positioned first
- Metadata listing every part, source bundle, mesh path id, material path id, and whether skinning was preserved

Pragmatic first milestone:
- Render all parts in the same GLB with correct transforms and **no visible mesh deformation**,
  even if materials are fallback.
- Then apply resolved textures/materials.
- Then unify skeletons where possible.
- Then add animation preview.
Status:
- ✅ Current exporter writes a single-scene multi-part Cinnamoroll GLB.
- ✅ Current multi-part export uses prefab-relative transforms from `222c0db...` so eyes and mouth
  are aligned to the head instead of staying at origin.
- Current limitation: the multi-part export is static assembly only; it does not yet unify a shared
  skeleton across the parts.

### 8. Viewer support for multi-part assets and animation
The existing viewer can load any GLB from `index.json`, so it may work once the GLB is valid. Still verify:
```powershell
python server.py
# Open http://localhost:5173/web_character_viewer/
```
Check:
- Multi-part GLB appears as one selectable character entry
- Parts are centered and scaled together
- Skeleton helper does not crash when some parts are static and others are skinned
- Missing textures show a clear fallback material
- Animation list distinguishes embedded clips, known-but-not-embedded Unity clips, and generated/test clips.
- Playback controls should work with embedded GLTF animations once available.

### 9. Watch-outs
- Do not read `MonoBehaviour` broadly.
- `SkinnedMeshRenderer` object names are often blank in CSV; use linked GameObject names.
- Current exporter picks one best renderer. Multi-part export must not rely on the single-target scorer.
- Body may be a static `MeshRenderer`/`MeshFilter`, not `SkinnedMeshRenderer`.
- Do not trust a skinned export just because it loads. Compare against a static/no-skin export
  to separate geometry bugs from skeleton/bind-pose bugs.
- Some GameObjects have duplicate names; use path ids, not names alone.
- GLB packing currently works for the single exports, but re-check chunk length/alignment after multi-mesh changes.
- `GLTFLoader.js` imports `../utils/BufferGeometryUtils.js`; local helper exists at
  `web_character_viewer/utils/BufferGeometryUtils.js`.

---

## Repo Layout
```
E:\dev\hello_kitty_asset_extract\
├── AGENTS.md                  ← this file
├── README.md
├── requirements.txt
├── server.py                  # dev server, port 5173
├── .gitignore
├── extracted\
│   └── characters\
│       └── index.json         # current extracted character manifest
├── reports\                   # bundle_inventory.csv + bundle_summary.txt
├── scripts\
│   ├── common.py
│   ├── inventory_assets.py
│   └── extract_character_glb.py
└── web_character_viewer\
    ├── index.html
    ├── app.js
    ├── styles.css
    ├── utils\
    │   └── BufferGeometryUtils.js
    └── vendor\
        ├── three.module.js
        ├── GLTFLoader.js
        └── OrbitControls.js
```
