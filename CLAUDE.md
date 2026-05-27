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

- `index.json` — the catalog index (schema v1).
- `apps/<name>/<stem>.py` + `<stem>.manifest.json` + optional
  `<stem>.ui.json` — one folder per cartridge.

Schema contract: `~/projects/pwnagotchi/ink-cartridge-catalog-schema.md`
(index shape, validation rules, URL resolution).

Cartridge contract: `~/projects/pwnagotchi/ink-cartridge-ui-schema.md`
(widget vocabulary, `data_source` block, the auto-SyncCard convention —
cartridges with `data_source` don't need ui.json).

## Conventions

- File-stem uses underscores (Python module names); manifest `name` uses
  hyphens. The host normalises both before comparing
  (e.g. `tide_sun.py` ↔ `name: "tide-sun"`).
- Bump version in BOTH the .py class and the manifest when you change
  anything in the package; the companion's "Update available" badge
  uses the manifest version.
- After editing a cartridge: `git push` from this repo; the companion's
  Browse → refresh picks up the new version. No device-side deploy
  needed (the device just installs from the catalog).

## Publishing

```
git -C ~/projects/ink-cartridges add -A
git -C ~/projects/ink-cartridges commit -m "<msg>"
git -C ~/projects/ink-cartridges push
```

raw.githubusercontent.com has a 5-minute CDN cache; the Browse screen
shows the older index for up to that long after a push.
