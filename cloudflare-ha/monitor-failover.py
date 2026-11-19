#!/usr/bin/env python3
"""Observe automatic takeover and public ping without changing ownership."""
import argparse
import json
from pathlib import Path
import time
import urllib.request
import urllib.error
from project_config import load_project

parser = argparse.ArgumentParser()
parser.add_argument("--target", choices=("primary", "backup"), required=True)
parser.add_argument("--timeout", type=int, default=600)
parser.add_argument("--interval", type=int, default=20)
args = parser.parse_args()
base = Path(__file__).resolve().parent
project = load_project()
config = json.loads((base / "generated/primary/config.json").read_text())
keys = json.loads((base / "generated/worker-secrets.json").read_text())
start = time.monotonic()
deadline = start + args.timeout
while time.monotonic() < deadline:
    try:
        request = urllib.request.Request(config["controller_url"] + "/__ha/status", headers={
            "Authorization": "Bearer " + keys["ADMIN_TOKEN"], "User-Agent": "ImmichHA-Admin/1.0"})
        with urllib.request.urlopen(request, timeout=10) as response:
            state = json.load(response)
        ping = urllib.request.Request("https://" + project["publicHost"] + "/api/server/ping",
            headers={"User-Agent": "ImmichHA-Admin/1.0", "Cache-Control": "no-store"})
        node = None
        try:
            with urllib.request.urlopen(ping, timeout=10) as response:
                code = response.status
                node = response.headers.get("X-Immich-HA-Node")
                pong = json.load(response).get("res") == "pong"
        except urllib.error.HTTPError as error:
            code, pong = error.code, False
            error.close()
        since = state.get("failureSince")
        remaining = max(0, 300 - (state["serverTime"] - since) // 1000) if since is not None else None
        print(json.dumps({"elapsedSeconds": round(time.monotonic()-start), "owner": state["owner"],
            "epoch": state["epoch"], "ready": state["ready"], "auto": state["auto"],
            "failureWaitSeconds": remaining, "publicHTTP": code, "publicNode": node,
            "primaryHealthy": state["primaryHealthy"], "backupHealthy": state["backupHealthy"]}), flush=True)
        if state["owner"] == args.target and node == args.target and code == 200 and pong:
            print("TAKEOVER VERIFIED: " + args.target + " serves the production domain.", flush=True)
            raise SystemExit(0)
    except (urllib.error.URLError, TimeoutError):
        print("Temporary monitoring connection error; retrying.", flush=True)
    time.sleep(args.interval)
print("Timed out waiting for takeover; no role changes were forced.")
raise SystemExit(1)
