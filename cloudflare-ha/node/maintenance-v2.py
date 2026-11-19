#!/usr/bin/env python3
import json
from pathlib import Path
import sys

sys.path.insert(0, "/usr/local/lib/immich-ha")
from ha_agent import request_json, syncthing_endpoint, local_healthy
from ha_runtime import Runtime

config = json.loads(Path("/etc/immich-ha/config.json").read_text())
def info():
    return request_json(config["controller_url"].rstrip("/") + "/__ha/node-info", config["agent_token"])

if len(sys.argv) != 2:
    raise SystemExit("Specify dump or refresh")
Runtime(config, syncthing_endpoint, info, local_healthy).maintenance(sys.argv[1])
