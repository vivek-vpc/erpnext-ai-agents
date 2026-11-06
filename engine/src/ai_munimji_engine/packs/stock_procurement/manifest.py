"""The stock pack's manifest, which ships inside the engine so that the standalone service and the Frappe app read the same declaration: the tools, the ERPNext roles and permissions the
pack needs, the kinds of approval request, and whether the pack is on by default. The file is `manifest.yaml` beside this module (package data of the engine's wheel)."""
from __future__ import annotations

from pathlib import Path

from ...core.packs import PackManifest, load_manifest

MANIFEST_PATH = Path(__file__).resolve().with_name("manifest.yaml")


def manifest() -> PackManifest:
    """The parsed and checked manifest. Raises `PackError` with the file named if it is wrong."""
    return load_manifest(MANIFEST_PATH)
