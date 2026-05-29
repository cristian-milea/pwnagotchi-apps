# CLAUDE.md

## What this is

The public curated catalog of installable Ink Cartridges. Three peer repos:

- `~/projects/pwnagotchi/` — device-ops (the host plugin `ink-cartridge.py` lives here)
- `~/projects/pwnagotchi-companion-app/` — Android companion (the Browse client)
- **`~/projects/ink-cartridges/`** *(this repo)* — published to
  `github.com/cristian-milea/ink-cartridges`; the companion app fetches
  `https://raw.githubusercontent.com/cristian-milea/ink-cartridges/main/index.json`
  on Browse and downloads each entry's files for one-tap install.

There are no built-in cartridges on the device any more — this catalog
is the only source.

## Layout

- `apps/<name>/<stem>.py` + `<stem>.manifest.json` + optional
  `<stem>.ui.json` (+ optional `icon.png`) — one folder per cartridge.
- `index.json` — **generated** from the manifests by `build_index.py`. Do not
  hand-edit it.

**The manifest is the single source of truth.** Everything intrinsic to a
cartridge — `name`, `icon` (2-char monogram), `version`, `author`,
`description`, `category`, `long_description`, `requires`, `data_source` — lives
in its `<stem>.manifest.json`. `index.json` is a denormalised cache of those
manifests plus registry-only fields the script computes (`files.*`, `icon_url`,
`size_bytes`, `updated_at`). This is the npm/registry model: edit the manifest,
regenerate the index.

Schema contract: `~/projects/pwnagotchi/ink-cartridge-catalog-schema.md`
(index shape, validation rules, URL resolution).

Cartridge contract: `~/projects/pwnagotchi/ink-cartridge-ui-schema.md`
(manifest fields, widget vocabulary, `data_source` block, the secret-slug
namespacing, the auto-SyncCard convention).

## Conventions

- File-stem uses underscores (Python module names); manifest `name` uses
  hyphens. The host normalises both before comparing
  (e.g. `tide_sun.py` ↔ `name: "tide-sun"`).
- `icon` is canonical in the manifest. The device reads it from there (falling
  back to the Python class `.icon` only when a manifest is absent), so the class
  attribute is now just a safety net — keep it matching the manifest.
- Bump `version` in BOTH the .py class and the manifest when you change
  anything in the package; the companion's "Update available" badge uses the
  manifest version.

## Publishing

Regenerate the index, then commit. CI (`.github/workflows/index.yml`) runs
`build_index.py --check` and fails if you forget.

```
python3 build_index.py            # regenerate index.json from manifests
git -C ~/projects/ink-cartridges add -A
git -C ~/projects/ink-cartridges commit -m "<msg>"
git -C ~/projects/ink-cartridges push
```

raw.githubusercontent.com has a 5-minute CDN cache; the Browse screen
shows the older index for up to that long after a push.
