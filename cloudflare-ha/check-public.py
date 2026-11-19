#!/usr/bin/env python3
"""Read-only check of public Immich routing; no credentials or changes."""
import urllib.request
import urllib.error
from project_config import load_project

request = urllib.request.Request("https://" + load_project()["publicHost"] + "/api/server/ping", headers={
    "User-Agent": "ImmichHA-Check/1.0", "Cache-Control": "no-cache"})
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        print("HTTP:", response.status)
        print("HA node:", response.headers.get("X-Immich-HA-Node", "not present"))
        print("HA epoch:", response.headers.get("X-Immich-HA-Epoch", "not present"))
        print("Response:", response.read(1024).decode(errors="replace"))
except urllib.error.HTTPError as error:
    print("HTTP:", error.code)
    print("HA node:", error.headers.get("X-Immich-HA-Node", "not present"))
    print("HA epoch:", error.headers.get("X-Immich-HA-Epoch", "not present"))
    print("Response:", error.read(1024).decode(errors="replace"))
    raise SystemExit(1)
except urllib.error.URLError as error:
    print("Connection failed:", error.reason)
    raise SystemExit(1)
