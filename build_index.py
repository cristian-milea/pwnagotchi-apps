#!/usr/bin/env python3
"""Generate index.json from the cartridge manifests.

The manifest (`apps/<name>/<stem>.manifest.json`) is the single source of truth
for everything intrinsic to a cartridge. This script ingests every manifest and
emits index.json, adding only *registry-scoped* fields (file paths, hosted asset
URLs, byte sizes). Never hand-edit index.json — edit the manifest and re-run.

Usage:
    python3 build_index.py          # regenerate index.json in place
    python3 build_index.py --check  # exit 1 if index.json is stale (CI guard)

`updated_at` is preserved when the catalog content is unchanged, so re-running
on an up-to-date tree is a no-op (the --check guard stays stable).
"""
import glob
import json
import os
import sys
from datetime import datetime, timezone

INDEX_PATH = "index.json"
INDEX_SCHEMA_VERSION = 1

# Fields copied verbatim from the manifest into the index entry (when present).
CANONICAL_FIELDS = (
    "name", "icon", "version", "author", "description",
    "long_description", "category", "homepage", "license",
)


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _entry_for(manifest_path):
    """Build one index entry from a manifest path.

    Sibling files are discovered by glob, not by stem: the manifest name uses
    hyphens while the Python module uses underscores (e.g. `vector-racing` ↔
    `vector_racing.py`), so deriving one stem from another is wrong.
    """
    directory = os.path.dirname(manifest_path)
    manifest = _read_json(manifest_path)

    pys = [p for p in glob.glob(os.path.join(directory, "*.py"))
           if os.path.basename(p) != "__init__.py"]
    if len(pys) != 1:
        raise ValueError(f"{directory}: expected exactly one cartridge .py, found {pys}")
    py = pys[0]
    uis = glob.glob(os.path.join(directory, "*.ui.json"))
    icon_png = os.path.join(directory, "icon.png")

    entry = {}
    for field in CANONICAL_FIELDS:
        if field in manifest and manifest[field] is not None:
            entry[field] = manifest[field]

    # requires: always present so clients can rely on the shape.
    req = manifest.get("requires") or {}
    entry["requires"] = {
        "permissions": req.get("permissions") or [],
        "secrets": req.get("secrets") or [],
    }

    files = {"py": py, "manifest": manifest_path}
    if uis:
        files["ui"] = uis[0]
    entry["files"] = files

    if os.path.isfile(icon_png):
        entry["icon_url"] = icon_png

    size = sum(os.path.getsize(p) for p in files.values())
    if os.path.isfile(icon_png):
        size += os.path.getsize(icon_png)
    entry["size_bytes"] = size
    return entry


def build():
    manifests = sorted(glob.glob("apps/*/*.manifest.json"))
    apps = [_entry_for(m) for m in manifests]
    apps.sort(key=lambda e: e["name"])

    # Preserve updated_at when the app set is byte-identical to the committed
    # index, so a no-op rebuild doesn't churn the timestamp (keeps --check stable).
    updated_at = None
    if os.path.isfile(INDEX_PATH):
        old = _read_json(INDEX_PATH)
        if old.get("apps") == apps:
            updated_at = old.get("updated_at")
    if updated_at is None:
        updated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "updated_at": updated_at,
        "apps": apps,
    }


def _render(index):
    return json.dumps(index, indent=2, ensure_ascii=False) + "\n"


def main(argv):
    check = "--check" in argv
    index = build()
    rendered = _render(index)
    if check:
        current = open(INDEX_PATH, encoding="utf-8").read() if os.path.isfile(INDEX_PATH) else ""
        if current != rendered:
            sys.stderr.write(
                "index.json is stale — run `python3 build_index.py` and commit.\n")
            return 1
        print("index.json is up to date.")
        return 0
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(rendered)
    print(f"Wrote {INDEX_PATH} ({len(index['apps'])} cartridges).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
