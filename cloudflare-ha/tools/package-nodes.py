#!/usr/bin/env python3
"""Build private, per-node upgrade ZIPs without deploying or rotating keys."""
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from project_config import load_project, node_properties

project = load_project()
files = ("agent.py", "ha_runtime.py", "pg_stream.py", "quorum_lock.py", "quorum_authority.py", "immich_upgrade.py", "build-immich-release.sh", "maintenance-v2.py", "app-run-v2", "upgrade-v2.sh")
for node in ("primary", "backup"):
    source = ROOT / "generated" / node / "config.json"
    config = json.loads(source.read_text(encoding="utf-8"))
    if config["node"] != node:
        raise SystemExit("STOP: Generated config has the wrong node identity")
    config.update(node_properties(project, node))
    destination = ROOT / "generated" / (node + "-v2.zip")
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("config.json", json.dumps(config, indent=2) + "\n")
        for filename in files:
            archive.writestr(filename, (ROOT / "node" / filename).read_bytes().replace(b"\r\n", b"\n"))
    print("Created", destination.name)
print("Private ZIPs contain existing node keys. Transfer only to the matching LXC; do not share publicly.")
