#!/usr/bin/env python3
"""Run on the Windows control machine. No secrets appear in command arguments."""
import argparse
import json
from pathlib import Path
import urllib.error
import urllib.request
import uuid

parser = argparse.ArgumentParser()
parser.add_argument("action", choices=["status", "probe", "auto-on", "auto-off", "recovery-on", "recovery-off", "preference-on", "preference-off", "promote-backup", "prepare", "switch", "resume"])
parser.add_argument("--target", choices=["primary", "backup"])
parser.add_argument("--compact", action="store_true", help="Print only operational status, without full telemetry")
args = parser.parse_args()
if args.action in ("prepare", "switch", "preference-on", "preference-off") and not args.target:
    parser.error("--target is required for prepare/switch/preference-on/preference-off")
base = Path(__file__).resolve().parent / "generated"
config = json.loads((base / "primary/config.json").read_text())
secrets = json.loads((base / "worker-secrets.json").read_text())
url = config["controller_url"]
headers = {"Authorization": "Bearer " + secrets["ADMIN_TOKEN"], "Content-Type": "application/json",
           "User-Agent": "ImmichHA-Admin/1.0"}
if args.action == "status":
    request = urllib.request.Request(url + "/__ha/status", headers=headers)
elif args.action == "probe":
    request = urllib.request.Request(url + "/__ha/probe", data=b"{}", headers=headers)
else:
    if args.action.startswith("preference-"):
        value = {"action": "set-preference", "target": args.target, "enabled": args.action == "preference-on", "actor": "local-admin"}
    elif args.action in ("prepare", "switch", "resume"):
        value = {"action": args.action, "target": args.target, "actor": "local-admin", "requestId": uuid.uuid4().hex}
    else:
        value = {"action": "promote-backup"} if args.action == "promote-backup" else {
            "action": "set-recovery" if args.action.startswith("recovery-") else "set-auto", "enabled": args.action.endswith("-on")}
    request = urllib.request.Request(url + "/__ha/admin", data=json.dumps(value).encode(), headers=headers)
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        value = json.load(response)
        if args.compact:
            operation = value.get("operation")
            value = {key: value.get(key) for key in ("owner", "epoch", "auto", "autoRequested", "ready", "leaseUntil", "serverTime", "primaryHealthy", "backupHealthy", "standbyEligible", "recovery", "preference")} | {
                "nodes": {node: {"version": agent.get("protocolVersion", 1), "localHealthy": agent.get("localHealthy"),
                    "mode": (agent.get("metrics") or {}).get("appMode"),
                    "lastError": (agent.get("metrics") or {}).get("lastError"),
                    "freeGiB": round((agent.get("metrics") or {}).get("disk", {}).get("free", 0) / 1024**3, 2)}
                    for node, agent in value.get("agents", {}).items()},
                "operation": {key: operation.get(key) for key in ("id", "kind", "status", "index", "error", "progress")} if operation else None}
        print(json.dumps(value, indent=2))
except urllib.error.HTTPError as error:
    print("HTTP", error.code, error.read(4096).decode())
    raise SystemExit(1)
except urllib.error.URLError as error:
    print("Connection failed:", error.reason)
    raise SystemExit(1)
