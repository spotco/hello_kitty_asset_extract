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

## Run the web viewer

```bash
python server.py
```

Then open:

- <http://localhost:5173/web_character_viewer/>

## Safety note

Never modify or write files inside the game installation folder. This project only reads bundle data and writes outputs to this repository workspace.
