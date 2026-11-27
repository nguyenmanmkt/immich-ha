"""Validated non-secret configuration shared by deployment and diagnostics."""
import ipaddress
import json
from pathlib import Path
import re
import urllib.parse

ROOT = Path(__file__).resolve().parent
HOST = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z")


def hostname(value):
    if not isinstance(value, str) or not HOST.fullmatch(value) or "." not in value or ".." in value:
        raise ValueError("Use a DNS hostname without protocol, port or path")
    if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in value.split(".")):
        raise ValueError("Invalid DNS hostname")
    return value


def load_project(path=None):
    config_path = Path(path or ROOT / "project.json")
    if not config_path.is_file():
        raise FileNotFoundError("Missing project.json; copy project.example.json to project.json and fill your deployment settings")
    settings = json.loads(config_path.read_text(encoding="utf-8"))
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", settings["workerName"]):
        raise ValueError("Invalid workerName")
    seen = {hostname(settings["publicHost"]), hostname(settings["adminHost"])}
    if len(seen) != 2:
        raise ValueError("Public and administration hostnames must differ")
    for node in ("primary", "backup"):
        parsed = urllib.parse.urlsplit(settings["origins"][node])
        if parsed.scheme != "https" or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.port:
            raise ValueError("Each origin must be an HTTPS hostname without path or port")
        host = hostname(parsed.hostname)
        if host in seen:
            raise ValueError("Origins must differ from public/admin hosts and each other")
        seen.add(host)
        description = settings["nodes"][node]
        ipaddress.IPv4Address(description["ip"])
        if not isinstance(description["label"], str) or not 1 <= len(description["label"]) <= 80:
            raise ValueError("Invalid server label")
        if not isinstance(description["containerId"], int) or description["containerId"] < 100:
            raise ValueError("Invalid Proxmox container ID")
        if description.get('tailscaleIp'):
            if ipaddress.ip_address(description['tailscaleIp']) not in ipaddress.ip_network('100.64.0.0/10'):
                raise ValueError('Use a Tailscale IPv4 address')
    access = settings.get("access", {})
    if access.get("teamDomain"):
        hostname(access["teamDomain"])
        if not access["teamDomain"].endswith(".cloudflareaccess.com"):
            raise ValueError("Use the Cloudflare Access team domain")
    if not isinstance(access.get("emails", []), list):
        raise ValueError("Access emails must be a list")
    for email in access.get("emails", []):
        if not isinstance(email, str) or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("Invalid administrator email")
    urls=[settings.get('funnelUrl')] + [settings['nodes'][n].get('funnelUrl') for n in ('primary','backup')]
    for value in filter(None,urls):
        parsed = urllib.parse.urlsplit(value)
        host = hostname(parsed.hostname or '')
        if parsed.scheme != 'https' or not host.endswith('.ts.net') or parsed.port not in (None,443,8443,10000) or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:
            raise ValueError('funnelUrl must be a public Tailscale HTTPS URL without path or credentials')
    if settings.get('witnessTailscaleIp'):
        if ipaddress.ip_address(settings['witnessTailscaleIp']) not in ipaddress.ip_network('100.64.0.0/10'):
            raise ValueError('Use a Tailscale IPv4 address for witness')
    return settings


def worker_variables(project):
    access = project.get("access", {})
    return {
        "PUBLIC_HOST": project["publicHost"], "ADMIN_HOST": project["adminHost"],
        "PRIMARY_ORIGIN": project["origins"]["primary"].rstrip("/"),
        "BACKUP_ORIGIN": project["origins"]["backup"].rstrip("/"),
        "NODE_METADATA": json.dumps(project["nodes"], ensure_ascii=False),
        "ACCESS_TEAM_DOMAIN": access.get("teamDomain", ""), "ACCESS_AUD": access.get("audience", ""),
        "ADMIN_EMAILS": json.dumps(access.get("emails", [])),
        "FUNNEL_URL": project.get('funnelUrl', '').rstrip('/'),
        "FUNNEL_WITNESS_IP": project.get('witnessTailscaleIp', ''),
    }


def node_properties(project, node):
    return {"public_url": "https://" + project["publicHost"],
            "admin_url": "https://" + project["adminHost"],
            "display_name": project["nodes"][node]["label"],
            "ip_address": project["nodes"][node]["ip"]}
