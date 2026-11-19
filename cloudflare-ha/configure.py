#!/usr/bin/env python3
"""Generate secrets/config locally. Values are never printed. Keep generated private."""
import argparse
import json
import os
from pathlib import Path
import secrets
import urllib.parse
from project_config import load_project, node_properties

parser = argparse.ArgumentParser()
parser.add_argument("--controller", required=True, help="https://WORKER.ACCOUNT.workers.dev")
args = parser.parse_args()
project = load_project()
url = urllib.parse.urlsplit(args.controller)
if url.scheme != "https" or not url.hostname or url.path not in ("", "/") or url.query or url.fragment:
    parser.error("Use an HTTPS origin without a path/query")
base = Path(__file__).resolve().parent
output = base / "generated"
if output.exists():
    raise SystemExit("STOP: generated already exists; preserve the current secrets")
output.mkdir(mode=0o700)
names = ["ADMIN_TOKEN", "PRIMARY_AGENT_TOKEN", "BACKUP_AGENT_TOKEN",
         "PRIMARY_ORIGIN_TOKEN", "BACKUP_ORIGIN_TOKEN"]
values = {name: secrets.token_urlsafe(48) for name in names}


def write(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, indent=2) + "\n")
    os.chmod(path, 0o600)


write(output / "worker-secrets.json", values)
for node in ("primary", "backup"):
    folder = output / node
    folder.mkdir(mode=0o700)
    write(folder / "config.json", {
        "node": node,
        "controller_url": args.controller.rstrip("/"),
        "agent_token": values[node.upper() + "_AGENT_TOKEN"],
        "origin_token": values[node.upper() + "_ORIGIN_TOKEN"],
        "enforce": False,
        "syncthing_config": "",
        **node_properties(project, node),
    })
print("Created private files under generated/. Do not share them or commit them.")
