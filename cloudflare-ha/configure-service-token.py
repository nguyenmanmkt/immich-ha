#!/usr/bin/env python3
"""Store origin service credentials privately, preserving all existing HA keys."""
import getpass
import argparse
import json
import os
from pathlib import Path
import sys
import subprocess
from service_credentials import validate, ID_KEY, SECRET_KEY
from local_env import load_env, origin_credentials


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deploy", action="store_true", help="Deploy the existing Worker after saving the Service Token")
    parser.add_argument("--interactive", action="store_true", help="Enter/replace the Service Token manually")
    options = parser.parse_args()
    path = Path(__file__).resolve().parent / "generated/worker-secrets.json"
    values = json.loads(path.read_text(encoding="utf-8"))
    if options.interactive:
        client_id = input("Service Token Client ID: ").strip()
        secret = getpass.getpass("Service Token Client Secret (hidden): ").strip()
        updated = {**values, ID_KEY: client_id, SECRET_KEY: secret}
        validate(updated)
    else:
        updated = origin_credentials(values, load_env())
        if not validate(updated):
            raise ValueError("Set the Service Token in .env")
    temporary = path.with_name(".worker-secrets.service-token.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            os.chmod(temporary, 0o600)
            stream.write(json.dumps(updated, indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    print("Saved origin Service Token locally. Existing HA keys are unchanged.")
    if options.deploy:
        subprocess.run([sys.executable, str(path.parents[1] / "deploy.py"), "--update"], check=True)
    else:
        print("Next: python deploy.py --update")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, EOFError):
        print("STOP: Could not save credentials. Check the input/private file and retry; no credential values are logged.", file=sys.stderr)
        raise SystemExit(1)
    except subprocess.CalledProcessError:
        print("STOP: Service Token was saved, but deployment did not finish. Run python deploy.py --update to retry.", file=sys.stderr)
        raise SystemExit(1)
