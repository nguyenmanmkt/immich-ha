#!/usr/bin/env python3
"""Initial control-plane deployment using only Python's standard library.

Does not create/change DNS, tunnels, production routes, or LXC services.
Cloudflare API credentials are loaded from local .env or environment variables.
"""
import argparse
import getpass
import json
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from project_config import load_project, worker_variables, node_properties
from service_credentials import validate as validate_service_credentials
from local_env import load_env, origin_credentials

BASE = Path(__file__).resolve().parent


def multipart(metadata, modules, boundary):
    parts = []
    for name, content, content_type in [("metadata", json.dumps(metadata).encode(), "application/json"),
                                        *[(name, content, "application/javascript+module")
                                          for name, content in modules.items()]]:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{name}"\r\n'
                     f'Content-Type: {content_type}\r\n\r\n'.encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true", help="Update this existing Worker without resetting its DO")
    parser.add_argument("--interactive", action="store_true", help="Prompt for missing Cloudflare API credentials")
    options = parser.parse_args()
    subprocess.run([sys.executable, str(BASE / "tools/embed-ui.py")], check=True)
    project = load_project()
    name = project["workerName"]
    environment = load_env()
    account = environment.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    if not account and options.interactive:
        account = input("Cloudflare Account ID: ").strip()
    if not re.fullmatch(r"[a-fA-F0-9]{32}", account):
        raise RuntimeError("Set CLOUDFLARE_ACCOUNT_ID in .env (32 hexadecimal characters)")
    token = environment.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not token and options.interactive:
        token = getpass.getpass("Cloudflare API Token (hidden): ").strip()
    if not token:
        raise RuntimeError("Set CLOUDFLARE_API_TOKEN in .env")
    if any(char in token for char in ("\r", "\n", "\x00")):
        raise RuntimeError("Invalid CLOUDFLARE_API_TOKEN characters")
    private_values = [token]

    def api(path, method="GET", body=None, content_type="application/json", missing_ok=False):
        data = json.dumps(body).encode() if body is not None and not isinstance(body, bytes) else body
        request = urllib.request.Request("https://api.cloudflare.com/client/v4" + path,
            data=data, method=method, headers={"Authorization": "Bearer " + token, "Content-Type": content_type})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404 and missing_ok:
                return None
            text = error.read(32_768).decode(errors="replace")
            for value in private_values:
                text = text.replace(value, "<redacted>")
            raise RuntimeError(f"Cloudflare HTTP {error.code}: {text[:3000]}") from None
        if not result.get("success"):
            raise RuntimeError("Cloudflare API did not succeed; check the token permissions")
        return result.get("result")

    prefix = f"/accounts/{account}/workers"
    subdomain = api(prefix + "/subdomain").get("subdomain")
    if not subdomain or not re.fullmatch(r"[a-zA-Z0-9-]+", subdomain):
        raise RuntimeError("First register a workers.dev subdomain in the Cloudflare dashboard")
    controller_url = f"https://{name}.{subdomain}.workers.dev"
    output = BASE / "generated"
    if not output.exists():
        subprocess.run([sys.executable, str(BASE / "configure.py"), "--controller", controller_url], check=True)
    saved_config = json.loads((output / "primary/config.json").read_text())
    if saved_config["controller_url"] != controller_url:
        raise RuntimeError("Existing generated config belongs to a different controller; do not overwrite its keys")
    values = origin_credentials(json.loads((output / "worker-secrets.json").read_text()), environment)
    validate_service_credentials(values)
    private_values.extend(values.values())
    script = prefix + f"/scripts/{name}"
    existing = api(script + "/settings", missing_ok=True)
    manifest = output / "deployment.json"
    if existing is not None:
        previous = json.loads(manifest.read_text()) if manifest.exists() else {}
        if previous != {"account_id": account, "worker": name}:
            raise RuntimeError("Worker already exists; refusing to overwrite it without a matching local deployment record")
        if not options.update:
            print("Existing deployment found; completing subdomain and Cron setup without overwriting code/state.")
    if existing is None or options.update:
        variables = worker_variables(project)
        metadata = {
            "main_module": "worker.mjs", "compatibility_date": "2026-11-16",
            "bindings": [
                {"type": "durable_object_namespace", "name": "CONTROLLER", "class_name": "HAController"},
                *[{"type": "plain_text", "name": key, "text": value} for key, value in variables.items()],
                *[{"type": "secret_text", "name": key, "text": value} for key, value in values.items()],
            ],
            "migrations": {"new_tag": "v1", "new_sqlite_classes": ["HAController"]},
            "observability": {"enabled": True},
        }
        if existing is not None:
            metadata.pop("migrations")
        modules = {path.name: path.read_bytes() for path in (BASE / "src").glob("*.mjs")}
        boundary = "immich-ha-" + uuid.uuid4().hex
        print("Updating Worker code; keeping Durable Object state and routes..." if existing is not None
              else "Deploying control Worker and SQLite Durable Object; production routes remain unchanged...")
        api(script, method="PUT", body=multipart(metadata, modules, boundary),
            content_type="multipart/form-data; boundary=" + boundary)
        manifest.write_text(json.dumps({"account_id": account, "worker": name}) + "\n")
    # Refresh domain/display metadata without rotating any existing keys.
    for node in ("primary", "backup"):
        config_path = output / node / "config.json"
        config = json.loads(config_path.read_text())
        config.update(node_properties(project, node))
        config_path.write_text(json.dumps(config, indent=2) + "\n")
    api(script + "/subdomain", method="POST", body={"enabled": True, "previews_enabled": False})
    if existing is None or not options.update:
        api(script + "/schedules", method="PUT", body=[{"cron": "* * * * *"}])
    print("DEPLOYED:", controller_url)
    print("Auto takeover setting is unchanged." if existing is not None else "Auto takeover is OFF.")
    print("Production domain, DNS, tunnels and both LXC services are unchanged.")
    print("Next: install the generated per-node configs and origin gates, following README.vi.md.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print("STOP:", error, file=sys.stderr)
        raise SystemExit(1)
