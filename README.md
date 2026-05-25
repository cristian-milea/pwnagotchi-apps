# pwnagotchi-apps

Curated catalog of installable pwn-apps for the Buscar companion Android app.

The companion app fetches `index.json` and lets users browse + install apps
with one tap. Apps are alternate-screen plugins for the
[pwn-apps](https://github.com/) host — they take over the e-ink while
pwnagotchi keeps hunting.

## Layout

- `index.json` — catalog index (schema v1).
- `apps/<name>/` — one folder per app, containing `<stem>.py`,
  `<stem>.manifest.json`, and optionally `<stem>.ui.json` + `icon.png`.

## Schema

See `pwn-apps-catalog-schema.md` in the device-ops repo for the full
contract. Short version: each entry needs `name`, `version`, `author`,
`description`, `category`, `requires`, and `files`.

## Contributing

v1 is maintainer-curated. PRs welcome but expect review. A `validate.py`
CI check will land before opening up submissions.
