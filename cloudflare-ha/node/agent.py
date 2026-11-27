#!/usr/bin/env python3
"""Immich HA agent. Origin authorization fails closed on expired grants.

Observe mode is the default. Enforcing requires loopback-only Immich and all
Cloudflare/public entry points routed through the origin gate.
"""
import hmac
import json
import logging
import os
import pwd
import shlex
import signal
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, "/usr/local/lib/immich-ha")
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from ha_runtime import Runtime, JobRunner
except ImportError:
    Runtime = JobRunner = None

CONFIG = Path("/etc/immich-ha/config.json")
STATE = Path("/var/lib/immich-ha")
STANDBY = Path("/var/lib/immich-standby")
MEDIA = Path("/mnt/immich-storage")
HEARTBEAT_TEST = Path('/run/immich-ha-heartbeat-outage.json')


def heartbeat_test_blocked(path=HEARTBEAT_TEST, now=None, boot_id=None):
    """Root-only, boot-bound, expiring fault injection; never changes grants."""
    try:
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>2048:return False
        value=json.loads(path.read_text())
        if value.get('version')!=1 or str(uuid.UUID(value['testId']))!=value['testId']:return False
        current_boot=boot_id if boot_id is not None else Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        current=now if now is not None else time.clock_gettime(time.CLOCK_BOOTTIME)
        start,end=value['startedBoot'],value['untilBoot']
        if type(start) not in (int,float) or type(end) not in (int,float):return False
        return value.get('bootId')==current_boot and 0<end-start<=180 and start<=current<end
    except (OSError,ValueError,KeyError,TypeError):return False


def command(*args, timeout=60):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout.strip()


def atomic_text(path, value, mode=0o600, group=None):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value + "\n")
    os.chmod(temporary, mode)
    if group is not None:
        os.chown(temporary, 0, group)
    os.replace(temporary, path)


def request_json(url, token=None, payload=None, method=None, timeout=5, local_tls=False):
    headers = {"Cache-Control": "no-store", "User-Agent": "ImmichHA-Agent/1.0"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    context = ssl._create_unverified_context() if local_tls else None
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        body = response.read(1024 * 1024)
        return json.loads(body) if body else None


def local_healthy():
    try:
        value = request_json("http://127.0.0.1:2283/api/server/ping", timeout=2)
        return isinstance(value, dict) and value.get("res") == "pong"
    except Exception:
        return False


def isolated():
    # The persistent bind must survive application/host restarts.
    lines = Path("/opt/immich/.env").read_text().splitlines()
    hosts = [line.partition("=")[2].strip().strip("\"'") for line in lines
             if line.startswith("IMMICH_HOST=")]
    if hosts != ["127.0.0.1"]:
        return False
    listeners = command("ss", "-H", "-ltn", "sport = :2283", timeout=5)
    for line in listeners.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[3] not in ("127.0.0.1:2283", "[::1]:2283"):
            return False
    return True


def syncthing_endpoint(config):
    environments = {}
    try:
        value = command("systemctl", "show", "syncthing@immich.service", "--property=Environment", "--value")
        environments = dict(item.split("=", 1) for item in shlex.split(value) if "=" in item)
    except Exception:
        pass
    if config.get("syncthing_config"):
        candidates = [Path(config["syncthing_config"])]
    elif environments.get("STHOMEDIR"):
        candidates = [Path(environments["STHOMEDIR"]) / "config.xml"]
    else:
        home = Path(pwd.getpwnam("immich").pw_dir)
        candidates = [Path("/var/lib/syncthing-immich/config.xml"),
                      home / ".local/state/syncthing/config.xml", home / ".config/syncthing/config.xml"]
    existing = [path for path in candidates if path.is_file()]
    if len(existing) != 1:
        raise RuntimeError("Set syncthing_config to the running instance config.xml")
    tree = ET.parse(existing[0]).getroot()
    gui = tree.find("gui")
    key = gui.findtext("apikey") if gui is not None else None
    address = environments.get("STGUIADDRESS") or (gui.findtext("address") if gui is not None else None)
    if not key or not address:
        raise RuntimeError("Syncthing GUI address/API key missing")
    if "://" not in address:
        scheme = "https" if gui.get("tls") == "true" else "http"
        address = scheme + "://" + address
    parts = urllib.parse.urlsplit(address)
    host = parts.hostname
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    if parts.scheme not in ("http", "https") or not host or not parts.port:
        raise RuntimeError("Unsupported local Syncthing GUI address")
    url = f"{parts.scheme}://{host}:{parts.port}"
    return url, key


def pause_sync(config):
    base, key = syncthing_endpoint(config)
    context = ssl._create_unverified_context()
    for folder in ("immich-library", "immich-database"):
        url = base + "/rest/config/folders/" + urllib.parse.quote(folder, safe="")
        headers = {"X-API-Key": key, "Content-Type": "application/json"}
        req = urllib.request.Request(url, data=b'{"paused":true}', headers=headers, method="PATCH")
        with urllib.request.urlopen(req, timeout=5, context=context) as response:
            response.read()
        req = urllib.request.Request(url, headers={"X-API-Key": key})
        with urllib.request.urlopen(req, timeout=5, context=context) as response:
            if json.load(response).get("paused") is not True:
                raise RuntimeError("Syncthing did not pause folder " + folder)


class Agent:
    def __init__(self, config):
        self.config = config
        self.node = config["node"]
        if self.node not in ("primary", "backup"):
            raise ValueError("Invalid node")
        url = urllib.parse.urlsplit(config["controller_url"])
        if url.scheme != "https" or not url.hostname or url.path not in ("", "/") or url.query or url.fragment:
            raise ValueError("controller_url must be an HTTPS origin")
        for key in ("agent_token", "origin_token"):
            if not isinstance(config.get(key), str) or len(config[key]) < 32:
                raise ValueError("A generated secret is required: " + key)
        self.session = uuid.uuid4().hex
        self.view = {"role": "standby", "epoch": 0, "deadline": 0, "ready": False}
        self.activation_done = False
        self.was_owner = (STATE / "was-owner").exists()
        self.fenced = False
        self.sync_paused = False
        self.runtime = None
        self.jobs = None
        self.quorum_shadow = None
        self.quorum_shadow_error = False
        self.quorum_authority = None
        self.fallback = None
        self.grant_mutex = threading.RLock()
        self.next_poll_seconds = 60
        if config.get("runtime_version", 1) >= 2:
            if Runtime is None:
                raise RuntimeError("Runtime v2 module is missing")
            self.runtime = Runtime(config, syncthing_endpoint, self.node_info, local_healthy)
            self.jobs = JobRunner(self.runtime, self.send_result)
            session_path = STATE / "session-v2"
            if session_path.exists():
                self.session = session_path.read_text().strip()
            else:
                atomic_text(session_path, self.session)
        quorum_config = Path('/etc/immich-ha/quorum-authority.json')
        REQUIRED = Path('/etc/immich-ha/quorum-required')
        if REQUIRED.exists() or quorum_config.exists():
            from quorum_authority import Admission
            value = json.loads(quorum_config.read_text())
            if not self.runtime or not self.runtime.streaming or not config.get('enforce') or value['node'] != self.node or REQUIRED.read_text().strip() != value['clusterId']:
                raise RuntimeError('Mandatory quorum configuration/identity mismatch')
            self.quorum_authority = Admission(value, self.session, self.runtime.group)
            self.runtime.quorum_authority = self.quorum_authority
            if value['fallbackEnabled']:
                from quorum_authority import Fallback
                self.fallback = Fallback(self)
        # Optional shadow observer has no influence on activate/fence/grants.
        shadow_config=Path('/etc/immich-ha/quorum-shadow.json')
        if shadow_config.is_file():
            try:
                from quorum_lock import ShadowMonitor
                value=json.loads(shadow_config.read_text())
                if value.get('node')!=self.node:raise ValueError('Wrong shadow identity')
                self.quorum_shadow=ShadowMonitor(value)
            except Exception:
                self.quorum_shadow_error=True
                logging.warning('Quorum shadow could not initialize; existing HA behavior retained')

    def node_info(self):
        if self.fallback and self.fallback.local():return self.fallback.info()
        return request_json(self.config["controller_url"].rstrip("/") + "/__ha/node-info", self.config["agent_token"], timeout=10)

    def send_result(self, receipt):
        return request_json(self.config["controller_url"].rstrip("/") + "/__ha/job-result", self.config["agent_token"], receipt, timeout=10)

    def enforcing(self):
        return self.config.get("enforce") is True and isolated()

    def can_promote(self, health, enforcing):
        if self.runtime:
            return enforcing and health and self.runtime.can_promote(self.view["epoch"], self.view.get("owner"))
        return self.node == "backup" and enforcing and health and os.path.ismount(MEDIA) and \
            (STANDBY / "current-db").is_file()

    def node_healthy(self):
        return self.runtime.node_healthy() if self.runtime else local_healthy()

    def activate(self):
        if self.quorum_authority:
            self.quorum_authority.assert_grant(self.view['epoch'], fresh=True)
        if self.runtime:
            self.runtime.grant = dict(self.view)
            self.activation_done = self.runtime.activate()
            self.was_owner = True
            atomic_text(STATE / "was-owner", "yes")
            self.fenced = False
            return
        if not os.path.ismount(MEDIA):
            raise RuntimeError("Media mount is missing")
        if self.node == "primary":
            command("systemctl", "start", "immich-ml.service", "immich-web.service")
        elif not self.activation_done:
            # Persist the guard first. Even a reboot must not resume old dumps.
            atomic_text(STANDBY / "role", "active", 0o640, pwd.getpwnam("immich").pw_gid)
            command("systemctl", "stop", "immich-standby-update.timer", "immich-standby-update.service", timeout=120)
            pause_sync(self.config)
            command("systemctl", "start", "immich-ml.service")
            command("systemctl", "restart", "immich-standby.service")
        else:
            command("systemctl", "start", "immich-ml.service", "immich-standby.service")
        self.activation_done = True
        self.was_owner = True
        atomic_text(STATE / "was-owner", "yes")
        self.fenced = False

    def fence(self):
        self.view = {**self.view, "ready": False, "deadline": 0}
        self.activation_done = False
        if self.runtime:
            if self.view.get("role") == "maintenance":
                self.runtime.enter_maintenance()
            else:
                # A quorum timeout must stop a former writable primary too.
                owner = self.view.get('owner')
                if self.quorum_authority and self.view['role'] == 'active' and not self.quorum_authority.valid(self.view['epoch']):
                    owner = None
                self.runtime.fence(self.view["epoch"], owner)
            self.fenced = True
            return
        if self.node == "primary":
            if not self.fenced:
                command("systemctl", "stop", "immich-web.service", "immich-ml.service")
            # Returning old primary must never push stale files into the new owner.
            # A temporary lease timeout alone does not change the sync direction.
            if self.was_owner and self.view["role"] != "active" and not self.sync_paused:
                pause_sync(self.config)
                self.sync_paused = True
        elif not self.fenced and (self.was_owner or (STANDBY / "role").read_text().strip() == "active"):
            command("systemctl", "stop", "immich-standby.service", "immich-ml.service")
        self.fenced = True

    def tick(self):
        if self.fallback:self.fallback.maintain()
        enforcing = self.enforcing()
        if self.config.get("enforce") and not enforcing:
            self.view = {**self.view, "ready": False, "deadline": 0}
            self.fence()
            raise RuntimeError("Enforcement requires IMMICH_HOST=127.0.0.1 and loopback-only port 2283")
        if enforcing and self.view["role"] == "active" and time.monotonic() >= self.view["deadline"]:
            self.fence()
        health = self.node_healthy()
        started = time.monotonic()
        payload = {
            "session": self.session, "epoch": self.view["epoch"],
            "localHealthy": health, "enforcing": enforcing,
            "canPromote": self.can_promote(health, enforcing),
            "ready": health and self.view["ready"] and started < self.view["deadline"],
        }
        if self.runtime:
            payload.update(protocolVersion=2, metrics=self.runtime.telemetry(), progress=self.jobs.progress)
            if self.quorum_authority:payload['metrics']['quorumAuthority'] = self.quorum_authority.telemetry()
            try:
                if self.quorum_shadow:payload['metrics']['quorumShadow']=self.quorum_shadow.telemetry()
                elif self.quorum_shadow_error:payload['metrics']['quorumShadow']={'mode':'shadow','state':'unavailable'}
            except Exception:pass
        if self.fallback:payload['quorumReport']=self.fallback.report()
        try:
            if self.fallback and heartbeat_test_blocked():
                raise urllib.error.URLError('Controlled expiring heartbeat outage test')
            response = request_json(self.config["controller_url"].rstrip("/") + "/__ha/heartbeat",
                                    self.config["agent_token"], payload, timeout=5)
        except (urllib.error.URLError,TimeoutError,ConnectionError) as error:
            if not self.fallback or isinstance(error,urllib.error.HTTPError) and error.code not in (429,500,502,503,504):raise
            logging.warning('Cloudflare control unavailable; checking shared quorum authority')
            self.fallback.failure();return
        self.next_poll_seconds = min(60, max(5, response.get('nextPollSeconds',60)))
        if self.fallback and not self.fallback.response(response):return
        role = response["role"]
        epoch = int(response["epoch"])
        previous_epoch = self.view["epoch"]
        # Conservative local expiry: time spent waiting for the response is not added.
        deadline = started + max(0, int(response["leaseMs"]) / 1000 - 10)
        proposed = {"role": role, "owner": response.get("owner"), "epoch": epoch, "deadline": deadline, "ready": False}
        with self.grant_mutex:
            if self.quorum_authority:
                if role == 'active':
                    command = response.get('command') or {}
                    resume = command.get('action') == 'resume' and command.get('source') == self.node and command.get('epoch') == epoch
                    policy=None
                    if response.get('quorum'):
                        from quorum_authority import control_policy
                        policy=control_policy(self.quorum_authority.config,response)
                    try:self.quorum_authority.admit(proposed, resume=resume,policy=policy,ack=(response.get('quorum') or {}).get('ack'))
                    except RuntimeError:
                        if self.quorum_authority.state != 'durably-released' or self.runtime.mode() != 'frozen':raise
                        deadline = proposed['deadline'] = 0
                else:self.quorum_authority.clear()
            self.view = proposed
        try:
            if self.quorum_shadow:self.quorum_shadow.submit(self.view,self.session)
        except Exception:pass
        self.next_poll_seconds = min(60, max(5, response.get("nextPollSeconds", 60)))
        if self.runtime and self.runtime.streaming and previous_epoch != epoch:
            self.next_poll_seconds = 5  # Acknowledge the new epoch/replica readiness promptly.
        if self.runtime:
            self.runtime.operation_id = (response.get("operation") or {}).get("id")
        if role == "active" and time.monotonic() < deadline:
            if enforcing:
                self.activate()
            self.view = {**self.view, "ready": local_healthy() and time.monotonic() < deadline}
        elif enforcing and not (self.quorum_authority and self.runtime.mode() == 'frozen' and role == 'active'):
            self.fence()
        if self.jobs:
            self.jobs.receive(response.get("command"))

    def run(self):
        while True:
            try:
                self.tick()
            except Exception as error:
                # Do not log bearer tokens, secret-bearing config, or request bodies.
                logging.warning("Heartbeat/action failed: %s", str(error))
                if self.config.get("enforce") and time.monotonic() >= self.view["deadline"]:
                    try:
                        self.fence()
                    except Exception:
                        logging.error("Could not finish local fencing; origin authorization remains closed")
            time.sleep(self.next_poll_seconds)

    def grant_valid(self):
        return time.monotonic() < self.view['deadline'] and (self.quorum_authority is None or self.quorum_authority.valid(self.view['epoch']))

    def watch_grant(self):
        # Independent of slow heartbeats/jobs: close and fence on local expiry.
        while True:
            with self.grant_mutex:
                frozen = self.quorum_authority and self.quorum_authority.state == 'durably-released' and self.runtime.mode() == 'frozen'
                if self.config.get('enforce') and self.view['role'] == 'active' and not frozen and not self.grant_valid():
                    try:self.fence()
                    except Exception:logging.error('Quorum watchdog could not finish fencing; origin remains closed')
            time.sleep(1)


def handler_for(agent):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            supplied = self.headers.get("X-Immich-HA-Token", "")
            if not hmac.compare_digest(supplied, agent.config["origin_token"]):
                self.send_error(403)
                return
            if self.path == "/health":
                ok = agent.node_healthy() and os.path.ismount(MEDIA)
            elif self.path in ("/authorize", "/authorize-local"):
                view = agent.view
                ok = view["role"] == "active" and view["ready"] and \
                    agent.grant_valid() and \
                    (agent.runtime is None or agent.runtime.mode() == 'active')
                if self.path == '/authorize-local':
                    ok = ok and agent.config.get('enforce') is True and agent.quorum_authority is not None and os.path.ismount(MEDIA)
                else:ok = ok and self.headers.get("X-Immich-HA-Epoch") == str(view["epoch"])
            else:
                self.send_error(404)
                return
            self.send_response(200 if ok else 403 if self.path.startswith('/authorize') else 503)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass
    return Handler


def main():
    import fcntl  # Linux service only; prevent two processes sharing a lease session.
    singleton = open("/run/immich-ha-agent.lock", "a")
    fcntl.flock(singleton, fcntl.LOCK_EX | fcntl.LOCK_NB)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = json.loads(CONFIG.read_text())
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    agent = Agent(config)
    def reload_mode(_signal, _frame):
        try:
            updated = json.loads(CONFIG.read_text())
            agent.config["enforce"] = updated.get("enforce") is True
            logging.info("Reloaded enforce=%s", agent.config["enforce"])
        except Exception:
            logging.error("Could not reload enforcement mode")
    signal.signal(signal.SIGHUP, reload_mode)
    server = ThreadingHTTPServer(("127.0.0.1", 9081), handler_for(agent))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if agent.quorum_authority:threading.Thread(target=agent.watch_grant, name='quorum-fencing', daemon=True).start()
    logging.info("Agent started for %s; enforce=%s", agent.node, config.get("enforce", False))
    agent.run()


if __name__ == "__main__":
    main()
