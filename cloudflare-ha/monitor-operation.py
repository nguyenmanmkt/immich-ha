#!/usr/bin/env python3
"""Read-only monitoring of an existing manual or automatic operation."""
import argparse
import json
from pathlib import Path
import time
import urllib.request
import urllib.error
from project_config import load_project

parser = argparse.ArgumentParser()
parser.add_argument("--job", required=True)
parser.add_argument("--timeout", type=int, default=2400)
parser.add_argument("--interval", type=int, default=20)
args = parser.parse_args()
root = Path(__file__).resolve().parent
config = json.loads((root / "generated/primary/config.json").read_text())
keys = json.loads((root / "generated/worker-secrets.json").read_text())
project = load_project()
started = time.monotonic()
while time.monotonic() - started < args.timeout:
    try:
        request = urllib.request.Request(config["controller_url"] + "/__ha/status", headers={
            "Authorization": "Bearer " + keys["ADMIN_TOKEN"], "User-Agent": "ImmichHA-Admin/1.0"})
        with urllib.request.urlopen(request, timeout=12) as response: state = json.load(response)
        job = state.get("operation")
        if not job or job["id"] != args.job:
            print("Operation changed; monitoring stopped without making changes.", flush=True)
            raise SystemExit(2)
        request = urllib.request.Request("https://" + project["publicHost"] + "/api/server/ping",
            headers={"User-Agent": "ImmichHA-Admin/1.0", "Cache-Control": "no-store"})
        public_node = None
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                public_code = response.status; public_node = response.headers.get("X-Immich-HA-Node")
                pong = json.load(response).get("res") == "pong"
        except urllib.error.HTTPError as error:
            public_code, pong = error.code, False
            error.close()
        step = job["steps"][job["index"]] if job["index"] < len(job["steps"]) else None
        print(json.dumps({"elapsedSeconds": round(time.monotonic()-started), "status": job["status"],
            "step": job["index"], "action": step.get("action") if step else None,
            "stepNode": step.get("node") if step else None, "progress": job.get("progress"),
            "error": job.get("error"), "owner": state["owner"], "epoch": state["epoch"],
            "publicHTTP": public_code, "publicNode": public_node, "standbyEligible": state["standbyEligible"]}), flush=True)
        if job["status"] == "failed": raise SystemExit(1)
        expected_owner = job["target"] if job["kind"] == "switch" else job["source"]
        if job["status"] == "succeeded" and pong and public_node == expected_owner and state["standbyEligible"]:
            print("OPERATION VERIFIED: completed, correct public owner and standby ready.", flush=True)
            raise SystemExit(0)
    except (urllib.error.URLError, TimeoutError):
        print("Temporary monitoring connection error; retrying.", flush=True)
    time.sleep(args.interval)
print("Monitoring timed out; no role changes were forced.", flush=True)
raise SystemExit(1)
