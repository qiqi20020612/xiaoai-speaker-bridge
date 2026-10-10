#!/usr/bin/env python3
"""Validate the public installation contract and translation/service completeness."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/xiaoai_speaker"
manifest = json.loads((COMPONENT / "manifest.json").read_text())
assert manifest["domain"] == "xiaoai_speaker" and manifest["version"]
assert manifest["config_flow"] and manifest["requirements"] == []
assert "linn" not in manifest.get("dependencies", []) and "mqtt" not in manifest.get(
    "dependencies", []
)
services = yaml.safe_load((COMPONENT / "services.yaml").read_text())
assert {"announce", "send_text", "set_volume"} <= services.keys()
for file in [COMPONENT / "strings.json", *sorted((COMPONENT / "translations").glob("*.json"))]:
    data = json.loads(file.read_text())
    assert set(services) <= data["services"].keys()
    for name, definition in services.items():
        assert set(definition["fields"]) <= data["services"][name]["fields"].keys(), (file, name)
for file in [
    "__init__.py",
    "manifest.json",
    "const.py",
    "config_flow.py",
    "coordinator.py",
    "client.py",
    "media_player.py",
    "number.py",
    "sensor.py",
    "switch.py",
    "button.py",
    "services.yaml",
]:
    assert (COMPONENT / file).is_file()
for file in ["start.sh", "xiaoai_boot.sh", "install.sh", "src/bridge.c", "config.example.json"]:
    assert (ROOT / "speaker" / file).is_file()
print("Manifest, two-end contract, services and translations passed validation.")
