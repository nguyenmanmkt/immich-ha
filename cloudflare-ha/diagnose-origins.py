#!/usr/bin/env python3
"""Read-only origin checks. Never print origin secrets."""
import json
import argparse
import http.client
import ipaddress
import socket
import sys
from pathlib import Path
import urllib.request
import urllib.error
import urllib.parse
from project_config import load_project
from service_credentials import headers as service_headers
from local_env import load_env, origin_credentials

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="backslashreplace")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the origin token to a redirect destination.


opener = urllib.request.build_opener(NoRedirect)

parser = argparse.ArgumentParser()
parser.add_argument("--resolve-primary", help="Use a public-DNS IP while keeping TLS verification/SNI")
args = parser.parse_args()
if args.resolve_primary:
    ipaddress.ip_address(args.resolve_primary)
values = origin_credentials(json.loads((Path(__file__).resolve().parent / "generated/worker-secrets.json").read_text()), load_env())
project = load_project()
for node in ("primary", "backup"):
    url = project["origins"][node].rstrip("/") + "/__ha/health"
    headers = {"X-Immich-HA-Token": values[node.upper() + "_ORIGIN_TOKEN"],
               "User-Agent": "ImmichHA-Health/1.0", "Cache-Control": "no-store"}
    headers.update(service_headers(values))
    request = urllib.request.Request(url, headers=headers)
    try:
        if node == "primary" and args.resolve_primary:
            connection = http.client.HTTPSConnection(urllib.parse.urlsplit(url).hostname, timeout=10)
            connection._create_connection = lambda _address, timeout, source_address: socket.create_connection(
                (args.resolve_primary, 443), timeout, source_address)
            connection.request("GET", "/__ha/health", headers=headers)
            response = connection.getresponse()
            print(node, "HTTP", response.status, "(public DNS IP, TLS verified)")
            response.read(1024)
            connection.close()
            continue
        with opener.open(request, timeout=10) as response:
            print(node, "HTTP", response.status)
            response.read(1024)
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            target = urllib.parse.urlsplit(error.headers.get("Location", ""))
            print(node, "HTTP", error.code, "redirect host:", target.hostname,
                  "path:", target.path)
            continue
        body = error.read(1024).decode(errors="replace")
        for secret in values.values():
            body = body.replace(secret, "<redacted>")
        print(node, "HTTP", error.code, body[:500])
    except urllib.error.URLError as error:
        print(node, "CONNECTION FAILED:", error.reason)
    except OSError as error:
        print(node, "CONNECTION FAILED:", error)
