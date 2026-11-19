#!/usr/bin/env python3
"""Local runtime v2: snapshots, guarded reseeding and planned role handover.

No command from the network is interpreted as shell text. All paths and actions
are constrained here. Existing databases and recovery archives are preserved.
"""
import contextlib
import datetime as dt
import gzip
import hashlib
import json
import ipaddress
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import stat
import ssl
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid

try:
    import fcntl
except ImportError:
    fcntl = None  # Pure helper tests also run on Windows. Runtime is Linux-only.

ROOT = Path("/var/lib/immich-ha/runtime")
MEDIA = Path("/mnt/immich-storage")
SCHEDULED_DUMP_DISABLED = Path('/etc/immich-ha/disable-scheduled-dump')
DUMP_RE = re.compile(r"immich-\d{8}-\d{6}\.dump\Z")
DB_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
MANAGED = ("library", "upload", "thumbs", "encoded-video", "profile")
MARKERS = {name + "/.immich" for name in MANAGED}


def run(*args, timeout=120, stdin=None):
    args = postgres_args(args)
    result = subprocess.run(args, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, check=False)
    if result.returncode:
        # No environment, credentials or SQL dump contents in errors.
        message = result.stderr.decode(errors="replace")[-1500:].strip()
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): {message}")
    return result.stdout.decode(errors="replace").strip()


def postgres_args(args):
    """Pin every database utility to the HA cluster after an explicit cutover."""
    from pg_stream import configuration
    if configuration():
        args = list(args)
        for i, arg in enumerate(args):
            if arg in ('psql', 'pg_dump', 'pg_restore', 'createdb', 'dropdb'):
                args.insert(i + 1, '--port=5433')
                break
    return args


def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def atomic(path, value, group=None):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex[:8])
    temporary.write_text(value, encoding="utf-8")
    os.chmod(temporary, 0o640 if group is not None else 0o600)
    if group is not None:
        os.chown(temporary, 0, group)
    os.replace(temporary, path)


def safe_media_path(root, relative):
    pure = PurePosixPath(relative)
    if not relative or "\\" in relative or pure.is_absolute() or ".." in pure.parts or str(pure) != relative:
        raise ValueError("Invalid manifest path")
    candidate = Path(root).joinpath(*pure.parts)
    if not candidate.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError("Manifest path escapes media storage")
    return candidate


def validate_manifest(root, entries):
    if not isinstance(entries, list) or len(entries) > 1_000_000:
        raise ValueError("Invalid media manifest")
    missing = []
    total = 0
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("size"), int) or entry["size"] < 0:
            raise ValueError("Invalid manifest entry")
        target = safe_media_path(root, entry.get("path", ""))
        if not target.is_file() or target.stat().st_size != entry["size"]:
            missing.append(entry["path"])
            if len(missing) >= 5:
                break
        total += entry["size"]
    if missing:
        raise RuntimeError("Ảnh/video chưa đủ hoặc sai kích thước: " + "; ".join(missing))
    return {"files": len(entries), "bytes": total}


def ensure_media_access(root, uid, gid, progress=lambda _: None, guard=lambda: None, chown=None):
    """Repair only Immich-owned media, preserving file contents and privacy bits."""
    chown = chown or os.chown
    root = Path(root)
    changed = checked = 0
    last_guard = time.monotonic()
    def repair(path):
        nonlocal changed, checked, last_guard
        info = path.lstat()
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise RuntimeError("Media có symlink hoặc kiểu tệp không được hỗ trợ; dừng sửa quyền")
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise RuntimeError("Media có symlink/tệp đặc biệt; dừng sửa quyền tự động")
        if info.st_uid != uid or info.st_gid != gid:
            chown(path, uid, gid)
            changed += 1
        needed = 0o700 if stat.S_ISDIR(info.st_mode) else 0o600
        if info.st_mode & needed != needed:
            os.chmod(path, stat.S_IMODE(info.st_mode) | needed)
        checked += 1
        if checked % 1000 == 0:
            if time.monotonic() - last_guard >= 5:
                guard()
                last_guard = time.monotonic()
            progress(f"Đang kiểm tra quyền thư viện: {checked} mục, đã sửa {changed}")
    guard()
    for name in MANAGED:
        base = root / name
        if base.is_symlink():
            raise RuntimeError("Thư mục media là symlink; dừng sửa quyền tự động")
        if not base.exists():
            continue
        repair(base)
        marker = base / ".immich"
        if not marker.exists():
            with marker.open("xb") as stream:
                os.chmod(marker, 0o600)
                stream.write(str(int(time.time() * 1000)).encode())
        for directory, dirs, files in os.walk(base, followlinks=False):
            for name in dirs + files:
                repair(Path(directory) / name)
    # These directories must let the app create its own backup marker. Do not
    # walk recovery archives, excluded downloads, Syncthing state or lost+found.
    backups = root / "backups"
    if backups.is_dir() and not backups.is_symlink() and not (backups / ".immich").exists():
        with (backups / ".immich").open("xb") as stream:
            os.chmod(backups / ".immich", 0o600)
            stream.write(str(int(time.time() * 1000)).encode())
    for path in (backups, backups / "postgres", backups / ".immich"):
        if path.is_symlink():
            raise RuntimeError("Thư mục backup là symlink; dừng sửa quyền tự động")
        if path.exists():
            repair(path)
    guard()
    progress(f"Quyền thư viện đã kiểm tra: {checked} mục, đã sửa {changed}")
    return {"checked": checked, "ownershipChanged": changed}


class Syncthing:
    def __init__(self, config, endpoint):
        self.base, self.key = endpoint(config)
        self.context = ssl._create_unverified_context()

    def api(self, path, data=None, method=None, timeout=30):
        headers = {"X-API-Key": self.key, "User-Agent": "ImmichHA-Agent/2.0"}
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=self.context) as response:
                text = response.read(2 * 1024 * 1024)
                return json.loads(text) if text else None
        except TimeoutError:
            raise RuntimeError(f"Syncthing {request.get_method()} {path}: quá thời gian chờ {timeout} giây") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise RuntimeError(f"Syncthing {request.get_method()} {path}: quá thời gian chờ {timeout} giây") from None
            raise

    def folder(self, folder):
        value = self.api("/rest/config/folders/" + folder)
        expected = MEDIA if folder == "immich-library" else MEDIA / "backups/postgres"
        if Path(os.path.expanduser(value["path"])).resolve() != expected.resolve():
            raise RuntimeError("Đường dẫn Syncthing không khớp thư viện đã cấu hình")
        return value

    def scan(self, folder, progress=lambda _: None, guard=lambda: None):
        guard()
        progress(f"Đang quét {folder}; thư viện lớn có thể cần vài phút")
        # Syncthing answers only after ScanFolderSubdirs has completed. The job
        # runs on a separate thread; lease heartbeats/progress remain active.
        self.api("/rest/db/scan?" + urllib.parse.urlencode({"folder": folder}), {}, "POST", timeout=1200)
        guard()

    def configure(self, kind, peer_id=None, progress=lambda _: None, guard=lambda: None):
        guard()
        self.exclude_local_markers()
        for folder in ("immich-library", "immich-database"):
            guard()
            progress(f"Đang thiết lập {folder}: {kind}")
            config = self.folder(folder)
            if peer_id and peer_id not in [item["deviceID"] for item in config.get("devices", [])]:
                raise RuntimeError("Folder chưa chia sẻ với đúng server đích")
            change = {"type": kind, "paused": False}
            if kind == "receiveonly" and not config.get("copyOwnershipFromParent"):
                change["copyOwnershipFromParent"] = True
            if folder == "immich-library" and kind == "receiveonly":
                current = config.get("versioning", {})
                if not current.get("type"):
                    change["versioning"] = {"type": "staggered", "params": {"maxAge": "2592000", "cleanInterval": "3600"}}
            # A retry must not tear down a correct, running folder again.
            if config.get("type") != kind or config.get("paused") or "versioning" in change or "copyOwnershipFromParent" in change:
                if not config.get("paused"):
                    self.api("/rest/config/folders/" + folder, {"paused": True}, "PATCH", timeout=180)
                guard()

                self.api("/rest/config/folders/" + folder, change, "PATCH", timeout=180)
            guard()
            actual = self.folder(folder)
            if actual.get("type") != kind or actual.get("paused"):
                raise RuntimeError("Không áp dụng được chiều đồng bộ Syncthing")
            if kind == "sendonly":
                # Called only after authority checks: the serving source is authoritative.
                self.scan(folder, progress, guard)
                progress(f"Đang xác nhận {folder} lấy máy active làm nguồn")
                self.api("/rest/db/override?folder=" + folder, {}, "POST", timeout=180)
                guard()

    def exclude_local_markers(self):
        path = "/rest/db/ignores?folder=immich-library"
        existing = self.api(path).get("ignore") or []
        markers = ["/" + value for value in sorted(MARKERS)]
        updated = markers + [line for line in existing if line not in markers]
        if updated != existing:
            self.api(path, {"ignore": updated}, "POST", timeout=180)

    def pause(self):
        for folder in ("immich-library", "immich-database"):
            self.folder(folder)
            self.api("/rest/config/folders/" + folder, {"paused": True}, "PATCH")

    def status(self, folder):
        config = self.folder(folder)
        state = self.api("/rest/db/status?folder=" + folder)
        errors = self.api("/rest/folder/errors?folder=" + folder + "&perpage=1")
        if config.get("paused"):
            raise RuntimeError("Folder đồng bộ đang Pause")
        if state.get("error") or state.get("pullErrors") or errors.get("errors"):
            raise RuntimeError("Syncthing có lỗi; không thể xác nhận dữ liệu đầy đủ")
        return state

    def wait(self, progress, peer_id=None, guard=lambda: None, timeout=1200):
        deadline = time.monotonic() + timeout
        settled = 0
        while time.monotonic() < deadline:
            guard()
            remaining = 0
            for folder in ("immich-library", "immich-database"):
                value = self.status(folder)
                remaining += value.get("needTotalItems", value.get("needFiles", 0) + value.get("needDirectories", 0) + value.get("needDeletes", 0))
                if value.get("state") not in ("idle", "cleaning"):
                    remaining += 1
                if peer_id:
                    query = urllib.parse.urlencode({"folder": folder, "device": peer_id})
                    remote = self.api("/rest/db/completion?" + query)
                    if remote.get("remoteState") != "valid" or remote.get("needItems", 0) > 0:
                        remaining += max(1, remote.get("needItems", 0))
            progress(f"Đồng bộ: còn {remaining} mục hoặc tác vụ đang xử lý")
            settled = settled + 1 if remaining == 0 else 0
            if settled >= 2:
                return
            time.sleep(3)
        raise RuntimeError("Đồng bộ chưa hoàn tất trong thời gian cho phép")


def builtin_backups(folder, limit=30):
    """Inventory only completed Immich SQL backups, never HA dumps or symlinks."""
    pattern = re.compile(r'(?:uploaded-|pre-restore-)?immich-db-backup-\d{8}T\d{6}-v[0-9A-Za-z.+_-]+-pg[0-9.]+\.sql(?:\.gz)?\Z')
    files = []
    try:
        for path in folder.iterdir():
            if not pattern.fullmatch(path.name):
                continue
            entry = path.lstat()
            if not stat.S_ISREG(entry.st_mode):
                continue
            files.append({'name': path.name, 'bytes': entry.st_size, 'modifiedAt': int(entry.st_mtime * 1000)})
        files.sort(key=lambda x: (x['modifiedAt'], x['name']), reverse=True)
        return {'state': 'ok', 'count': len(files), 'bytes': sum(f['bytes'] for f in files),
                'files': files[:limit], 'truncated': len(files) > limit, 'checkedAt': int(time.time() * 1000)}
    except OSError:
        return {'state': 'unavailable', 'checkedAt': int(time.time() * 1000)}


def tailnet_ip(address):
    try:
        host = urllib.parse.urlsplit(address if '://' in address else 'tcp://' + address).hostname
        ip = ipaddress.ip_address(host)
        if ip in ipaddress.ip_network('100.64.0.0/10') or ip in ipaddress.ip_network('fd7a:115c:a1e0::/48'):
            return str(ip)
    except (ValueError, TypeError):
        pass
    return None


def parse_tail_ping(output):
    # Route is taken from this successful probe, never inferred from a DERP home region.
    match = re.search(r'pong from .+ via (.+) in ([0-9.]+)(ns|µs|us|ms|s)(?:\s|$)', output)
    if not match:
        return {'state': 'unreachable'}
    route, delay, unit = match.groups()
    if route.startswith('DERP(') or 'relay' in route.lower():
        kind = 'relay'
    else:
        try:
            endpoint = urllib.parse.urlsplit('tcp://' + route).hostname
            ipaddress.ip_address(endpoint)
            kind = 'direct'
        except ValueError:
            kind = 'unknown'
    return {'state': kind, 'latencyMs': round(float(delay) * {'ns': 0.000001, 'µs': 0.001, 'us': 0.001, 'ms': 1, 's': 1000}[unit], 2)}


class Runtime:
    def __init__(self, config, endpoint, node_info, healthy):
        self.config = config
        self.node = config["node"]
        self.endpoint = endpoint
        self.info = node_info
        self.healthy = healthy
        self._metrics = None
        self._metrics_at = 0
        self._cpu_sample = None
        self._sync_sample = None
        self._observability = {}
        self._observability_at = 0
        self._observability_thread = None
        self.operation_id = None
        self._activated = False
        ROOT.mkdir(parents=True, exist_ok=True)
        self.group = pwd.getpwnam("immich").pw_gid
        from pg_stream import configuration, Streaming
        config = configuration()
        self.streaming = Streaming(self, config) if config else None

    def node_healthy(self):
        return self.streaming.healthy() if self.streaming else self.healthy()

    def repair_media_access(self, progress, guard=lambda: None):
        self.mounted()
        user = pwd.getpwnam("immich")
        return ensure_media_access(MEDIA, user.pw_uid, user.pw_gid, progress, guard)

    def read(self, name, default=""):
        try:
            return (ROOT / name).read_text().strip()
        except FileNotFoundError:
            return default

    def write(self, name, value):
        atomic(ROOT / name, value + "\n", self.group if name in ("current-db", "mode") else None)
        self._metrics_at = 0

    def mode(self):
        return self.read("mode", "fenced")

    def seed(self):
        try:
            return json.loads(self.read("seed.json", "null"))
        except ValueError:
            return None

    def database(self):
        value = self.read("current-db")
        if not DB_RE.fullmatch(value):
            raise RuntimeError("Invalid local database pointer")
        return value

    @contextlib.contextmanager
    def lock(self, nonblocking=False):
        if fcntl is None:
            raise RuntimeError("Runtime requires Linux")
        with open("/run/immich-ha-runtime.lock", "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
            except BlockingIOError:
                raise RuntimeError("Runtime đang hoàn tất một tác vụ khác") from None
            yield

    def mounted(self):
        if not os.path.ismount(MEDIA):
            raise RuntimeError("Ổ thư viện chưa mount; không ghi vào ổ hệ thống")

    def guard(self, command, source=False):
        self.mounted()
        for attempt in range(3):
            try:
                current = self.info()
                break
            except urllib.error.HTTPError as error:
                if error.code not in (429, 502, 503, 504) or attempt == 2:
                    raise
                error.close()
            except (TimeoutError, ConnectionError, urllib.error.URLError):
                if attempt == 2:
                    raise RuntimeError("Không xác nhận được quyền phục vụ sau 3 lần thử kết nối; dừng an toàn") from None
            time.sleep(attempt + 1)
        if current.get("nodeMaintenance", {}).get(self.node, {}).get("enabled"):
            raise RuntimeError("Máy đang bảo trì; dừng tác vụ ghi dữ liệu")
        if current["epoch"] != command["epoch"] or current["owner"] != command["source"]:
            raise RuntimeError("Quyền phục vụ đã đổi; dừng thao tác cũ")
        if (current["owner"] == self.node) != source:
            raise RuntimeError("Thao tác không phù hợp vai trò hiện tại")
        return current

    def can_promote(self, epoch, owner):
        if self.streaming:
            return os.path.ismount(MEDIA) and shutil.disk_usage(MEDIA).free >= 1024 ** 3 and self.streaming.can_promote(epoch, owner)
        seed = self.seed()
        return self.mode() == "standby" and bool(seed) and seed.get("sourceEpoch") == epoch and \
            seed.get("source") == owner and (ROOT / "current-db").is_file() and \
            os.path.ismount(MEDIA) and shutil.disk_usage(MEDIA).free >= 1024 ** 3

    def activate(self):
        self.mounted()
        if self.streaming:
            return self.streaming.activate()
        if self.mode() == "frozen":
            self._activated = False
            return False
        if self._activated and self.mode() == "active" and self.healthy():
            # A dump/sync job may hold the mutation lock while the serving app
            # remains healthy. Renew its lease without restarting services.
            return True
        with self.lock(nonblocking=True):
            # Recheck under the same lock as the freeze/restore job. A heartbeat
            # arriving concurrently must never thaw a final snapshot.
            mode = self.mode()
            if mode == "frozen":
                return False
            self.write("mode", "active")
            if mode not in ("active", "lease-paused"):
                Syncthing(self.config, self.endpoint).pause()
            run("systemctl", "start", "immich-ml.service")
            run("systemctl", "restart" if mode != "active" else "start", "immich-ha-app.service")
            self._activated = True
        return self.healthy()

    def fence(self, epoch, owner):
        self._activated = False
        if self.streaming:
            return self.streaming.fence(epoch, owner)
        if self.can_promote(epoch, owner):
            return  # Prepared standby is allowed to keep its private API running.
        if owner == self.node:
            if self.mode() != "frozen":
                self.write("mode", "lease-paused")
            run("systemctl", "stop", "immich-ha-app.service", "immich-ml.service")
            return
        try:
            receiving = json.loads(self.read("receiving.json", "null"))
        except ValueError:
            receiving = None
        if self.mode() == "receiving" and receiving and self.operation_id == receiving.get("jobId") and \
           receiving.get("source") == owner and receiving.get("epoch") == epoch:
            return  # An authorized reseed may receive files while its API is stopped.
        self.write("mode", "fenced")
        run("systemctl", "stop", "immich-ha-app.service", "immich-ml.service")
        Syncthing(self.config, self.endpoint).pause()

    def enter_maintenance(self):
        """Quiesce this standby under the same lock as snapshot/restore work."""
        self._activated = False
        with self.lock(nonblocking=True):
            run("systemctl", "stop", "immich-ha-app.service", "immich-ml.service")
            Syncthing(self.config, self.endpoint).pause()
            self.write("mode", "maintenance")
            if self.streaming:
                from pg_stream import ctl, GRANT
                GRANT.unlink(missing_ok=True)
                ctl('stop')
            self._metrics = None

    def wait_healthy(self, progress, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.healthy():
                return
            progress("Đang chờ Immich khởi động và trả lời kiểm tra sức khỏe")
            time.sleep(2)
        raise RuntimeError("Immich chưa sẵn sàng sau khi khởi động")

    def dump_atomic(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            run("pg_restore", "--list", str(path))
            return
        temporary = path.with_name("." + path.name + ".partial")
        try:
            with open(temporary, "wb") as destination:
                result = subprocess.run(postgres_args(["runuser", "-u", "postgres", "--", "pg_dump", "-Fc", "--dbname=" + self.database()]),
                                        stdout=destination, stderr=subprocess.PIPE, timeout=1800)
                if result.returncode:
                    raise RuntimeError("PostgreSQL dump failed: " + result.stderr.decode(errors="replace")[-1000:])
            run("pg_restore", "--list", str(temporary))
            os.chown(temporary, self.group_owner, self.group)
            os.chmod(temporary, 0o640)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @property
    def group_owner(self):
        return pwd.getpwnam("immich").pw_uid

    def manifest(self):
        entries = []
        for directory in MANAGED:
            base = MEDIA / directory
            if not base.is_dir():
                raise RuntimeError("Thiếu thư mục media " + directory)
            for root, dirs, files in os.walk(base, followlinks=False):
                for name in dirs + files:
                    if (Path(root) / name).is_symlink():
                        raise RuntimeError("Cần kiểm tra symlink trong thư viện trước khi bàn giao")
                for name in files:
                    path = Path(root) / name
                    try:
                        size = path.stat().st_size
                    except FileNotFoundError:
                        if self.mode() == "frozen":
                            raise
                        continue
                    relative = path.relative_to(MEDIA).as_posix()
                    if relative not in MARKERS:
                        entries.append({"path": relative, "size": size})
        return entries

    def snapshot(self, command, progress):
        self.guard(command, source=True)
        progress("Đang tạo database dump; thư mục ảnh giữ nguyên")
        folder = MEDIA / "backups/postgres"
        folder.mkdir(parents=True, exist_ok=True)
        record = ROOT / "snapshots" / (hashlib.sha256(command["id"].encode()).hexdigest() + ".json")
        record.parent.mkdir(exist_ok=True)
        if record.exists():
            result = json.loads(record.read_text())
            if (folder / result["name"]).is_file() and (folder / result["manifestName"]).is_file() and \
               digest(folder / result["name"]) == result["sha256"] and digest(folder / result["manifestName"]) == result["manifestSha256"]:
                return result
        stamp = dt.datetime.now()
        filename = "immich-" + stamp.strftime("%Y%m%d-%H%M%S") + ".dump"
        while (folder / filename).exists():
            stamp += dt.timedelta(seconds=1)
            filename = "immich-" + stamp.strftime("%Y%m%d-%H%M%S") + ".dump"
        destination = folder / filename
        self.dump_atomic(destination)
        progress("Đang lập danh mục ảnh/video để xác nhận máy đích nhận đủ")
        entries = self.manifest()
        manifest_name = filename + ".manifest.json.gz"
        temp_manifest = folder.parent / ("." + manifest_name + ".partial")
        with gzip.open(temp_manifest, "wt", encoding="utf-8") as stream:
            json.dump(entries, stream, ensure_ascii=False, separators=(",", ":"))
        os.chown(temp_manifest, self.group_owner, self.group)
        os.chmod(temp_manifest, 0o640)
        os.replace(temp_manifest, folder / manifest_name)
        result = {"name": filename, "sha256": digest(destination), "manifestName": manifest_name,
                  "manifestSha256": digest(folder / manifest_name), "files": len(entries),
                  "bytes": sum(item["size"] for item in entries), "source": self.node,
                  "epoch": command["epoch"], "capturedAt": int(time.time() * 1000),
                  "stopped": self.mode() == "frozen"}
        meta = folder / (filename + ".meta.json")
        atomic(meta, json.dumps(result), self.group)
        atomic(record, json.dumps(result))
        self.write("last-dump.json", json.dumps(result))
        return result

    def archive(self, command, progress):
        self.guard(command, source=False)
        self.preflight_space()
        progress("Đang giữ database trước đồng bộ; không xóa thư viện cũ")
        identity = command["jobId"]
        if command.get("autoRecovery"):
            identity = "auto-" + command["source"] + "-" + str(command["epoch"]) + "-" + hashlib.sha256(self.database().encode()).hexdigest()[:12]
        path = MEDIA / "backups/recovery" / identity / (self.node + "-before.dump")
        self.dump_atomic(path)
        return {"archive": str(path), "bytes": path.stat().st_size}

    def preflight_space(self):
        self.mounted()
        if shutil.disk_usage(MEDIA).free < 1024 ** 3:
            raise RuntimeError("Ổ ảnh còn dưới 1 GiB; bổ sung dung lượng trước khi đồng bộ")
        if shutil.disk_usage("/var/lib/postgresql").free < 1024 ** 3:
            raise RuntimeError("Ổ database còn dưới 1 GiB; không tạo database thay thế")

    def read_snapshot(self, value):
        if not isinstance(value, dict) or not DUMP_RE.fullmatch(value.get("name", "")):
            raise ValueError("Invalid snapshot")
        if value.get("manifestName") != value["name"] + ".manifest.json.gz":
            raise ValueError("Invalid manifest filename")
        for key in ("sha256", "manifestSha256"):
            if not re.fullmatch(r"[a-f0-9]{64}", value.get(key, "")):
                raise ValueError("Invalid snapshot checksum")
        folder = MEDIA / "backups/postgres"
        dump = folder / value["name"]
        manifest = folder / value["manifestName"]
        if not dump.is_file() or not manifest.is_file():
            raise RuntimeError("Chưa nhận đủ dump và danh mục ảnh")
        if digest(dump) != value["sha256"] or digest(manifest) != value["manifestSha256"]:
            raise RuntimeError("Checksum bản sao không khớp; không khôi phục")
        if manifest.stat().st_size > 32 * 1024 ** 2:
            raise ValueError("Media manifest is too large")
        with gzip.open(manifest, "rb") as source:
            raw = source.read(128 * 1024 ** 2 + 1)
        if len(raw) > 128 * 1024 ** 2:
            raise ValueError("Expanded manifest is too large")
        validate_manifest(MEDIA, json.loads(raw))
        return dump

    def restore(self, snapshot, command, progress):
        current = self.guard(command, source=False)
        if snapshot.get("source") != current["owner"] or snapshot.get("epoch") != current["epoch"]:
            raise RuntimeError("Bản sao không thuộc nguồn đang active")
        self.preflight_space()
        dump = self.read_snapshot(snapshot)
        self.repair_media_access(progress, lambda: self.guard(command, source=False))
        seed = self.seed()
        if seed and seed.get("sha256") == snapshot["sha256"] and seed.get("sourceEpoch") == command["epoch"]:
            self.write("mode", "standby")
            run("systemctl", "start", "immich-ha-app.service")
            self.wait_healthy(progress)
            return {"restored": snapshot["name"], "reused": True}
        candidate = "immich_ha_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        old = self.database()
        old_mode = self.mode()
        old_seed = self.read("seed.json", "null")
        previous = self.read("previous-db")
        created = False
        switched = False
        try:
            progress("Đang khôi phục vào database mới; giữ nguyên bản đang dùng")
            run("runuser", "-u", "postgres", "--", "createdb", "--template=template0", "--owner=immich", candidate)
            created = True
            with open(dump, "rb") as stream:
                run("runuser", "-u", "postgres", "--", "pg_restore", "--exit-on-error", "--single-transaction",
                    "--dbname=" + candidate, stdin=stream, timeout=1800)
            self.guard(command, source=False)
            run("systemctl", "stop", "immich-ha-app.service")
            switched = True
            self.write("current-db", candidate)
            self.write("seed.json", json.dumps({"source": snapshot["source"], "sourceEpoch": command["epoch"],
                "dumpName": snapshot["name"], "sha256": snapshot["sha256"], "appliedAt": int(time.time() * 1000)}))
            self.write("mode", "standby")
            run("systemctl", "start", "immich-ha-app.service")
            self.wait_healthy(progress)
            self.write("previous-db", old)
            if re.fullmatch(r"immich_ha_[0-9_]+[a-f0-9]{8}", previous or "") and previous not in (old, candidate):
                try:
                    run("runuser", "-u", "postgres", "--", "dropdb", "--if-exists", previous)
                except RuntimeError:
                    pass  # Cleanup failure cannot invalidate a successful handover.
            return {"restored": snapshot["name"], "files": snapshot.get("files"), "database": candidate}
        except Exception:
            if switched:
                run("systemctl", "stop", "immich-ha-app.service")
                self.write("current-db", old)
                self.write("seed.json", old_seed)
                self.write("mode", old_mode)
                if old_mode == "standby":
                    run("systemctl", "start", "immich-ha-app.service")
            if created:
                try:
                    run("runuser", "-u", "postgres", "--", "dropdb", "--if-exists", "--force", candidate)
                except RuntimeError:
                    pass
            raise

    def execute(self, command, progress):
        if not re.fullmatch(r"[a-f0-9-]{36}", command.get("jobId", "")):
            raise ValueError("Invalid job identity")
        if self.streaming:
            return self.streaming.execute(command, progress)
        action = command["action"]
        source_action = action in ("send", "dump", "freeze-dump", "wait-peer", "resume")
        self.guard(command, source=source_action)
        with self.lock():
            self.guard(command, source=source_action)
            sync = Syncthing(self.config, self.endpoint)
            if action == "archive":
                return self.archive(command, progress)
            if action == "receive":
                self.write("receiving.json", json.dumps({"source": command["source"], "epoch": command["epoch"], "jobId": command["jobId"]}))
                self.write("mode", "receiving")
                self.write("seed.json", "null")
                run("systemctl", "stop", "immich-ha-app.service", "immich-ml.service")
                self.repair_media_access(progress, lambda: self.guard(command, source=False))
                sync.configure("receiveonly", command.get("peerDeviceId"), progress, lambda: self.guard(command, source=False))
                return {"direction": "receiveonly"}
            if action == "send":
                sync.configure("sendonly", command.get("peerDeviceId"), progress, lambda: self.guard(command, source=True))
                return {"direction": "sendonly"}
            if action == "freeze-dump":
                self.write("mode", "frozen")
                run("systemctl", "stop", "immich-ha-app.service", "immich-ml.service")
                # Stop returns only after the service process group has exited.
                active = subprocess.run(["systemctl", "is-active", "--quiet", "immich-ha-app.service"]).returncode == 0
                if active:
                    raise RuntimeError("Không xác nhận được nguồn đã dừng ghi")
                return self.snapshot(command, progress)
            if action == "dump":
                return self.snapshot(command, progress)
            if action == "wait-peer":
                for folder in ("immich-library", "immich-database"):
                    sync.scan(folder, progress, lambda: self.guard(command, source=True))
                sync.wait(progress, command.get("peerDeviceId"), lambda: self.guard(command, source=True))
                return {"synced": True}
            if action == "sync-restore":
                sync.configure("receiveonly", command.get("peerDeviceId"), progress, lambda: self.guard(command, source=False))
                sync.wait(progress, guard=lambda: self.guard(command, source=False))
                return self.restore(command["snapshot"], command, progress)
            if action == "validate":
                self.wait_healthy(progress)
                if not self.can_promote(command["epoch"], command["source"]):
                    raise RuntimeError("Máy đích chưa có dữ liệu đúng phiên nguồn")
                return {"healthy": True, "seedEpoch": command["epoch"]}
            if action == "resume":
                self.write("mode", "active")
                run("systemctl", "start", "immich-ml.service", "immich-ha-app.service")
                self.wait_healthy(progress)
                return {"resumed": True}
            raise ValueError("Unknown fixed runtime action")

    def observability(self, peer_ip):
        """Optional read-only probes run off the lease heartbeat thread."""
        database = self.database()
        previous = self._observability
        if (self._observability_thread is None or not self._observability_thread.is_alive()) and (
                time.monotonic() - self._observability_at >= 60 or previous.get('_database') != database or previous.get('_peer') != peer_ip):
            self._observability_at = time.monotonic()
            def collect():
                result = {'_database': database, '_peer': peer_ip}
                try:
                    query = """SET statement_timeout='2000ms'; SET default_transaction_read_only=on;
SELECT json_build_object('photos',count(*) FILTER (WHERE type='IMAGE'),
'videos',count(*) FILTER (WHERE type='VIDEO')) FROM asset
WHERE "deletedAt" IS NULL AND visibility IN ('timeline','archive');"""
                    counts = json.loads(run('runuser', '-u', 'postgres', '--', 'psql', '-XqAt',
                        '--dbname=' + database, '-c', query, timeout=4))
                    result['immich'] = dict(counts, state='ok', checkedAt=int(time.time() * 1000))
                except Exception:
                    result['immich'] = {'state': 'unavailable', 'checkedAt': int(time.time() * 1000)}
                tail = {'state': 'unknown', 'peerIp': peer_ip}
                if peer_ip:
                    try:
                        status = json.loads(run('tailscale', 'status', '--json', timeout=3))
                        peer = next((p for p in status.get('Peer', {}).values() if peer_ip in p.get('TailscaleIPs', [])), None)
                        if status.get('BackendState') != 'Running':
                            tail['state'] = 'stopped'
                        elif peer is None:
                            tail['state'] = 'unknown'
                        elif not peer.get('Online'):
                            tail['state'] = 'offline'
                        else:
                            try:
                                tail.update(parse_tail_ping(run('tailscale', 'ping', '--c=1', '--timeout=2s', peer_ip, timeout=4)))
                            except Exception:
                                tail['state'] = 'unreachable'
                    except Exception:
                        tail['state'] = 'unavailable'
                tail['checkedAt'] = int(time.time() * 1000)
                result['tailscale'] = tail
                self._observability = result
            self._observability_thread = threading.Thread(target=collect, daemon=True)
            self._observability_thread.start()
        value = {}
        if previous.get('_database') == database:
            value['immich'] = previous.get('immich')
        if previous.get('_peer') == peer_ip:
            value['tailscale'] = previous.get('tailscale')
        return {k: v for k, v in value.items() if v is not None}

    def resources(self):
        value = {}
        try:
            fields = {}
            for line in Path('/proc/meminfo').read_text().splitlines():
                key, raw = line.split(':', 1)
                fields[key] = int(raw.split()[0]) * 1024
            total, available = fields['MemTotal'], fields['MemAvailable']
            if total > 0:
                value['memory'] = {'total': total, 'used': max(0, total - min(total, available)), 'checkedAt': int(time.time() * 1000)}
        except (OSError, ValueError, KeyError):
            pass
        try:
            stats = dict(line.split() for line in Path('/sys/fs/cgroup/cpu.stat').read_text().splitlines())
            usage = int(stats['usage_usec'])
            cores = float(len(os.sched_getaffinity(0)))
            quota, period = Path('/sys/fs/cgroup/cpu.max').read_text().split()
            if quota != 'max':
                cores = min(cores, int(quota) / int(period))
            now = time.monotonic()
            cpu = {'cores': cores, 'checkedAt': int(time.time() * 1000)}
            previous = self._cpu_sample
            if previous and now > previous[0] and usage >= previous[1] and cores > 0:
                cpu['percent'] = round(min(100, (usage - previous[1]) / 1000000 / (now - previous[0]) / cores * 100), 1)
            self._cpu_sample = (now, usage)
            value['cpu'] = cpu
        except (OSError, ValueError, KeyError, AttributeError):
            pass
        return value

    def telemetry(self):
        if self._metrics is not None and time.monotonic() - self._metrics_at < 25:
            return self._metrics
        def storage(path):
            value = shutil.disk_usage(path)
            return {"total": value.total, "used": value.used, "free": value.free}
        value = {"runtimeV2": True, "maintenanceControl": True, "appMode": self.mode(), "disk": storage(MEDIA),
                 "databaseDisk": storage("/var/lib/postgresql"), "seed": self.seed()}
        if self.streaming:
            try:
                value['postgresStreaming'] = self.streaming.state()
            except Exception:
                value['postgresStreaming'] = {'mode': 'streaming', 'handoverVersion': 2, 'fastHandoverVersion': 1, 'available': False}
        value.update(self.resources())
        value["builtinBackups"] = builtin_backups(MEDIA / "backups")
        value["mediaMounted"] = os.path.ismount(MEDIA)
        try:
            with self.lock(nonblocking=True):
                value["runtimeBusy"] = False
        except RuntimeError:
            value["runtimeBusy"] = True
        peer_ip = None
        try:
            value["appVersion"] = json.loads(Path("/opt/immich/app/package.json").read_text())["version"]
            if self.streaming:
                # A stopped former primary still needs a version to be reseeded.
                value["postgresVersion"] = run('/usr/lib/postgresql/16/bin/postgres', '--version', timeout=5).split()[2]
            else:
                value["postgresVersion"] = run("runuser", "-u", "postgres", "--", "psql", "-XAt", "--dbname=postgres", "-c", "SHOW server_version;", timeout=5)
            sync = Syncthing(self.config, self.endpoint)
            data = {"deviceId": sync.api("/rest/system/status")["myID"], "folders": {}}
            for folder in ("immich-library", "immich-database"):
                config = sync.folder(folder)
                status = sync.api("/rest/db/status?folder=" + folder)
                data["folders"][folder] = {"type": config["type"], "paused": config["paused"],
                    "state": status.get("state"), "needItems": status.get("needTotalItems", status.get("needFiles", 0)),
                    "error": status.get("error") or ("Có lỗi đồng bộ" if status.get("pullErrors") else "")}
            # Identify exactly the device shared with the library; never probe unrelated tailnet devices.
            peers = [x['deviceID'] for x in sync.folder('immich-library').get('devices', []) if x['deviceID'] != data['deviceId']]
            if len(peers) == 1:
                connections = sync.api('/rest/system/connections').get('connections', {})
                connection = connections.get(peers[0], {})
                data['peerConnected'] = connection.get('connected') is True
                stamp = time.monotonic()
                counters = (connection.get('inBytesTotal', 0), connection.get('outBytesTotal', 0))
                old = self._sync_sample
                if data['peerConnected'] and old and old[0] == peers[0] and stamp - old[1] > 1 and all(x >= y for x, y in zip(counters, old[2])):
                    data['receiveBps'], data['sendBps'] = [round((x - y) / (stamp - old[1])) for x, y in zip(counters, old[2])]
                self._sync_sample = (peers[0], stamp, counters) if data['peerConnected'] else None
                data['peerAddress'] = connection.get('address') if data['peerConnected'] else None
                device = next((x for x in sync.api('/rest/config/devices') if x['deviceID'] == peers[0]), {})
                candidates = [data.get('peerAddress')] + device.get('addresses', [])
                peer_ip = next((tailnet_ip(x) for x in candidates if x and tailnet_ip(x)), None)
            value['syncthing'] = data
            dumps = list((MEDIA / "backups/postgres").glob("immich-*.dump"))
            last = json.loads(self.read("last-dump.json", "null"))
            value["backup"] = {"count": len(dumps), "bytes": sum(path.stat().st_size for path in dumps),
                               "latest": max((path.name for path in dumps), default=None),
                               "lastDumpAt": last.get("capturedAt") if last else None}
        except Exception as error:
            value["lastError"] = str(error)[:300]
        try:
            value.update(self.observability(peer_ip))
        except Exception:
            pass  # Optional dashboard data must never interrupt a grant heartbeat.
        self._metrics = value
        self._metrics_at = time.monotonic()
        return value

    def prune(self):
        """Keep 48h hourly, then one per day through day 7; always newest."""
        folder = MEDIA / "backups/postgres"
        records = []
        for path in folder.glob("immich-*.dump"):
            if DUMP_RE.fullmatch(path.name) and path.is_file() and not path.is_symlink():
                try:
                    records.append((dt.datetime.strptime(path.name[7:-5], "%Y%m%d-%H%M%S"), path))
                except ValueError:
                    pass
        records.sort(reverse=True)
        keep = {records[0][1]} if records else set()
        days = set()
        now = dt.datetime.now()
        for stamp, path in records:
            age = now - stamp
            if age <= dt.timedelta(hours=48):
                keep.add(path)
            elif age <= dt.timedelta(days=7) and stamp.date() not in days:
                keep.add(path)
                days.add(stamp.date())
        for _, path in records:
            if path not in keep:
                for item in (path, path.with_name(path.name + ".meta.json"), path.with_name(path.name + ".manifest.json.gz")):
                    if item.is_file() and not item.is_symlink():
                        item.unlink()

    def maintenance(self, action, progress=print):
        if action == 'dump' and SCHEDULED_DUMP_DISABLED.exists():
            return  # Policy affects the scheduled service, never job snapshots.
        self.mounted()
        snapshot = None
        if action == "dump" and self.mode() != "active":
            return
        if action == "refresh":
            if self.streaming:
                return  # WAL replay replaces dump restores; backups still run hourly.
            if self.mode() != "standby":
                return
            records = sorted((MEDIA / "backups/postgres").glob("immich-*.dump.meta.json"), reverse=True)
            if not records:
                return
            snapshot = json.loads(records[0].read_text())
            if (self.seed() or {}).get("sha256") == snapshot.get("sha256"):
                return  # Do not call Cloudflare every minute when no new dump exists.
        current = self.info()
        if current.get("nodeMaintenance", {}).get(self.node, {}).get("enabled"):
            return
        if current.get("operation") and current["operation"].get("status") not in ("succeeded", "failed"):
            return  # Job owns pointer/snapshot changes until it finishes.
        command = {"id": "maintenance-" + uuid.uuid4().hex, "source": current["owner"], "epoch": current["epoch"]}
        with self.lock(nonblocking=True):
            if action == "dump":
                if current["owner"] != self.node or self.mode() != "active" or not self.healthy():
                    return
                self.snapshot(command, progress)
                self.prune()
            elif action == "refresh":
                seed = self.seed()
                if self.mode() != "standby" or current["owner"] == self.node or not seed or \
                   seed.get("source") != current["owner"] or seed.get("sourceEpoch") != current["epoch"]:
                    return
                if snapshot.get("source") != current["owner"] or snapshot.get("epoch") != current["epoch"]:
                    return
                if seed.get("sha256") == snapshot.get("sha256"):
                    return
                Syncthing(self.config, self.endpoint).wait(progress, guard=lambda: self.guard(command, source=False), timeout=900)
                if self.mode() != "standby":
                    return
                self.restore(snapshot, command, progress)
            else:
                raise ValueError("Unknown maintenance action")


class JobRunner:
    def __init__(self, runtime, send_result):
        self.runtime = runtime
        self.send_result = send_result
        self.thread = None
        self.progress = None
        self.command_id = None
        self.receipt = None

    def receive(self, command):
        if not command:
            return
        if command.get("node") != self.runtime.node or not re.fullmatch(r"[a-f0-9-]{36}-\d{1,3}", command.get("id", "")):
            raise ValueError("Invalid backend command")
        if self.thread and self.thread.is_alive():
            return
        if self.command_id == command["id"] and self.receipt:
            self.send_result(self.receipt)
            return
        self.command_id = command["id"]
        self.receipt = None
        self.thread = threading.Thread(target=self.execute, args=(command,), daemon=True)
        self.thread.start()

    def execute(self, command):
        receipt_file = ROOT / "jobs" / (command["id"] + ".json")
        receipt_file.parent.mkdir(exist_ok=True)
        def progress(message):
            self.progress = {"commandId": command["id"], "message": str(message)[:500]}
        if receipt_file.exists():
            receipt = json.loads(receipt_file.read_text())
        else:
            receipt = {"jobId": command["jobId"], "commandId": command["id"], "index": command["index"]}
            try:
                result = self.runtime.execute(command, progress)
                receipt.update(status="succeeded", result=result)
            except Exception as error:
                receipt.update(status="failed", error=str(error)[:1000])
            atomic(receipt_file, json.dumps(receipt))
        self.runtime._metrics_at = 0
        self.receipt = receipt
        try:
            self.send_result(receipt)
        except Exception:
            pass  # Next heartbeat resends the same durable receipt.
        self.progress = None
