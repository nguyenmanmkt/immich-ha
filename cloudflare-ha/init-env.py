#!/usr/bin/env python3
"""Create a private .env once, reusing locally saved credentials when available."""
import json
import os
from pathlib import Path

root = Path(__file__).resolve().parent
target = root / ".env"
if target.exists():
    print(".env already exists; existing values were preserved.")
    raise SystemExit(0)
saved_path = root / "generated/worker-secrets.json"
saved = json.loads(saved_path.read_text()) if saved_path.exists() else {}
manifest = root / "generated/deployment.json"
deployment = json.loads(manifest.read_text()) if manifest.exists() else {}
values = {
    "CLOUDFLARE_ACCOUNT_ID": deployment.get("account_id", ""),
    "CLOUDFLARE_API_TOKEN": "",
    "ORIGIN_ACCESS_CLIENT_ID": saved.get("ORIGIN_ACCESS_CLIENT_ID", ""),
    "ORIGIN_ACCESS_CLIENT_SECRET": saved.get("ORIGIN_ACCESS_CLIENT_SECRET", ""),
}
with target.open("x", encoding="utf-8", newline="\n") as stream:
    os.chmod(target, 0o600)
    stream.write("# Private credentials. Do not commit or share this file.\n")
    for key, value in values.items():
        stream.write(key + "=" + json.dumps(value) + "\n")
print("Created .env; reused saved Account ID and Service Token where present. No credential values printed.")
print("Fill missing values once. Then run: python deploy.py --update")
