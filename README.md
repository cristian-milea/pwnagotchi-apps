# ink-cartridges

Curated catalog of installable Ink Cartridges for the Pwnagotchi companion Android app.

The companion app fetches `index.json` and lets users browse + install
cartridges with one tap. Each cartridge is an alternate-screen plugin
for the [Ink Cartridge](https://github.com/cristian-milea/pwnagotchi)
host — they take over the e-ink while pwnagotchi keeps hunting.

## Layout

- `index.json` — catalog index (schema v1).
- `apps/<name>/` — one folder per cartridge, containing `<stem>.py`,
  `<stem>.manifest.json`, and optionally `<stem>.ui.json` + `icon.png`.

## Schema

See `ink-cartridge-catalog-schema.md` in the device-ops repo for the
full contract. Short version: each entry needs `name`, `version`,
`author`, `description`, `category`, `requires`, and `files`.

## Contributing

v1 is maintainer-curated. PRs welcome but expect review. A `validate.py`
CI check will land before opening up submissions.
