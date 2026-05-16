# Hello Kitty Island Adventure asset extraction

This project scans and extracts assets from **Hello Kitty Island Adventure** without modifying the installed game files.

- Game path: `C:\Program Files (x86)\Steam\steamapps\common\Hello Kitty Island Adventure`
- Unity version fallback: `2021.3.56f2`
- Bundles path: `Hello Kitty_Data\StreamingAssets\aa\StandaloneWindows64\`

## Install

```bash
python -m pip install -r requirements.txt
```

## Inventory bundles

```bash
python scripts\inventory_assets.py --limit 500
```

This writes:
- `reports\bundle_inventory.csv`
- `reports\bundle_summary.txt`

## Extract a character

```bash
python scripts\extract_character_glb.py --name cinnamoroll
```

This writes extracted files under `extracted\characters\<name>\` and updates `extracted\characters\index.json`.
Run `python scripts\inventory_assets.py` first so external mesh, material, and texture pointers can be resolved from `reports\bundle_inventory.csv`.
When a shared material such as `Lit` or `EmoteMesh_FacePlate` is textureless, the extractor falls back to matching bundle-local character materials or plate textures when available.

## Extract a multi-part Cinnamoroll GLB

```bash
python scripts\extract_character_glb.py --name cinnamoroll --bundle-hash f75e6233b13254a0a5e316a96d0d3114 --multi --slug cinnamoroll_full
```

This writes one GLB containing the current body, head, tail, eyes, and mouth parts as separate nodes in a single scene.

## Inspect the Cinnamoroll source bundle

```bash
python scripts\inspect_cinnamoroll_parts.py
```

This writes `reports\cinnamoroll_parts_inspection.json` with the key Cinnamoroll part GameObjects, their transform hierarchy, and mesh/material pointer resolution using `reports\bundle_inventory.csv`.

## Run the web viewer

```bash
python server.py
```

Then open:

- <http://localhost:5173/web_character_viewer/>

## Safety note

Never modify or write files inside the game installation folder. This project only reads bundle data and writes outputs to this repository workspace.
