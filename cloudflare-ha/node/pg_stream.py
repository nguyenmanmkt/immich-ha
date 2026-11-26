"""Physical PostgreSQL replication adapter. Never rsync a live PGDATA.

The old 16/main cluster is retained. This adapter owns only 16/ha (5433).
Authority remains with the existing Worker epoch/lease and the local runtime lock.
"""
import datetime as dt
import gzip
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

CONFIG = Path('/etc/immich-ha/streaming.json')
DATA = Path('/var/lib/postgresql/16/ha')
GRANT = Path('/run/immich-ha-stream-grant')
LSN = re.compile(r'[0-9A-F]+/[0-9A-F]+\Z')
NETWORK_DROP_IN = Path('/etc/systemd/system/postgresql@16-ha.service.d/immich-tailnet.conf')


def configuration(enabled=True):
    if not CONFIG.exists():
        return None
    value = json.loads(CONFIG.read_text())
    if enabled and value.get('enabled') is not True:
        return None
    ip = ipaddress.ip_address(value['peer_ip'])
    if ip not in ipaddress.ip_network('100.64.0.0/10'):
        raise ValueError('Replication peer must be a Tailscale IPv4 address')
    if value.get('port') != 5433 or value.get('database') != 'immich_stream':
        raise ValueError('Unexpected replication cluster/port/database')
    if value.get('peer') not in ('primary', 'backup'):
        raise ValueError('Invalid replication peer identity')
    return value


def pg(sql, database='postgres', timeout=10):
    result = subprocess.run(['runuser', '-u', 'postgres', '--', '/usr/lib/postgresql/16/bin/psql',
        '-XqAt', '-p', '5433', '-d', database, '-v', 'ON_ERROR_STOP=1', '-c', sql],
        capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('HA PostgreSQL query failed')  # SQL may contain credentials.
    return result.stdout.strip()


def status():
    return json.loads(pg("""SELECT json_build_object(
      'inRecovery', pg_is_in_recovery(),
      'systemId', (SELECT system_identifier::text FROM pg_control_system()),
      'timeline', (SELECT timeline_id FROM pg_control_checkpoint()),
      'replayLsn', pg_last_wal_replay_lsn()::text,
      'receiveLsn', pg_last_wal_receive_lsn()::text,
      'pendingBytes', COALESCE(pg_wal_lsn_diff(pg_last_wal_receive_lsn(),pg_last_wal_replay_lsn()),0),
      'receiverConnected', EXISTS(SELECT 1 FROM pg_stat_wal_receiver WHERE status='streaming'),
      'receiverTimeline', (SELECT received_tli FROM pg_stat_wal_receiver LIMIT 1),
      'sender', (SELECT sender_host FROM pg_stat_wal_receiver LIMIT 1));"""))


def wait_local_address(timeout=45):
    """Do not let PostgreSQL silently start with only its loopback listener."""
    config = configuration(enabled=False)
    if config is None:
        raise RuntimeError('Missing HA streaming network configuration')
    expected = ipaddress.ip_address(config['local_ip'])
    if expected not in ipaddress.ip_network('100.64.0.0/10'):
        raise ValueError('Local replication address must be a Tailscale IPv4 address')
    deadline = time.monotonic() + timeout
    while True:
        result = subprocess.run(['ip', '-j', '-4', 'addr', 'show', 'dev', 'tailscale0'],
            capture_output=True, text=True, timeout=3)
        if result.returncode == 0:
            addresses = json.loads(result.stdout)
            if any(info.get('local') == str(expected) for interface in addresses
                   for info in interface.get('addr_info', [])):
                return
        if time.monotonic() >= deadline:
            raise RuntimeError('Tailscale chưa có IP cục bộ; chưa khởi động PostgreSQL HA')
        time.sleep(1)


def install_network_guard():
    """Install ordering and a real address check; never restart a running DB."""
    if configuration(enabled=False) is None:
        return False
    text = '''# Managed by Immich HA: wait for the configured tailnet address.
[Unit]
Wants=tailscaled.service
After=tailscaled.service

[Service]
ExecStartPre=/usr/bin/python3 -c "import sys; sys.path.insert(0, '/usr/local/lib/immich-ha'); from pg_stream import wait_local_address; wait_local_address()"
'''
    if NETWORK_DROP_IN.exists() and NETWORK_DROP_IN.read_text() == text:
        return False
    NETWORK_DROP_IN.parent.mkdir(parents=True, exist_ok=True)
    NETWORK_DROP_IN.write_text(text)
    subprocess.run(['systemctl', 'daemon-reload'], check=True, capture_output=True, timeout=30)
    return True


def ctl(action):
    if action == 'start':
        wait_local_address()
    # No arbitrary cluster identifiers from controller requests.
    result = subprocess.run(['pg_ctlcluster', '16', 'ha', action], capture_output=True, timeout=90)
    if result.returncode and result.returncode != 2:
        raise RuntimeError('HA PostgreSQL cluster action failed: ' + action)


def clone(config, guard=lambda: None, progress=lambda _: None):
    """Build a verified candidate before touching the current standby cluster."""
    guard()
    if shutil.disk_usage(DATA.parent).free < 1536 * 1024**2:
        raise RuntimeError('Cần ít nhất 1,5 GiB trống để tạo bản PostgreSQL dự phòng')
    candidate = DATA.with_name('ha-seed-' + uuid.uuid4().hex[:12])
    account = __import__('pwd').getpwnam('postgres')
    candidate.mkdir(mode=0o700)
    os.chown(candidate, account.pw_uid, account.pw_gid)
    conn = (f"host={config['peer_ip']} port=5433 user=immich_ha_repl "
        f"application_name=ha_{config['peer']} connect_timeout=10 "
        "passfile=/var/lib/postgresql/.immich-ha-replication.pass sslmode=disable")
    progress('Đang tạo bản PostgreSQL vật lý từ máy active')
    try:
        result = subprocess.run(['runuser','-u','postgres','--','/usr/lib/postgresql/16/bin/pg_basebackup',
            '-d',conn,'-D',str(candidate),'-X','stream','-R','--checkpoint=fast','--no-password'],
            capture_output=True, timeout=1200)
        if result.returncode:
            raise RuntimeError('pg_basebackup thất bại; giữ nguyên database hiện tại')
        verify = subprocess.run(['runuser','-u','postgres','--','/usr/lib/postgresql/16/bin/pg_verifybackup',
            str(candidate)], capture_output=True, timeout=180)
        if verify.returncode:
            raise RuntimeError('Bản sao vật lý chưa vượt qua pg_verifybackup')
        guard()
        ctl('stop')
        # Archives are outside all shared Syncthing folders, never deleted here.
        archive = Path('/mnt/immich-storage/backups/pg-stream')
        archive.mkdir(parents=True, exist_ok=True, mode=0o700)
        archive = archive / ('ha-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
        if DATA.exists():
            shutil.move(str(DATA), str(archive))
        try:
            os.replace(candidate, DATA)
            ctl('start')
            current = status()
            if not current['inRecovery']:
                raise RuntimeError('Bản sao chưa ở chế độ chỉ đọc')
        except Exception:
            # Do not automatically start a retired writable primary after failure.
            ctl('stop')
            raise
        guard()
        progress('PostgreSQL dự phòng đang nhận WAL')
        return current
    finally:
        # Only this invocation's random candidate, never configured PGDATA.
        if candidate.exists():
            shutil.rmtree(candidate)


class Streaming:
    def __init__(self, runtime, config):
        self.runtime = runtime
        self.config = config

    def state(self):
        value = status()
        value['mode'] = 'streaming'
        value['handoverVersion'] = 2
        value['fastHandoverVersion'] = 1
        if value['receiverConnected']:
            self.runtime.write('stream-last-connected', str(int(time.time())))
        return value

    def healthy(self):
        try:
            value = self.state()
            if value['inRecovery']:
                return True
            return self.runtime.mode() == 'active' and self.runtime.healthy()
        except Exception:
            return False

    def can_promote(self, epoch, owner):
        try:
            value = self.state()
            seed = self.runtime.seed() or {}
            recent = time.time() - int(self.runtime.read('stream-last-connected', '0')) < 900
            if value['receiverConnected'] and value.get('sender') != self.config['peer_ip']:
                return False
            return self.runtime.mode() == 'standby' and value['inRecovery'] and recent and \
                value['pendingBytes'] == 0 and seed.get('source') == owner and \
                seed.get('sourceEpoch') == epoch and seed.get('systemId') == value['systemId']
        except Exception:
            return False

    def activate(self):
        r = self.runtime
        if r.read('immich-upgrade-lock.json', ''):
            return False
        if r.mode() == 'frozen':
            return False
        grant = getattr(r, 'grant', {})
        if getattr(r, '_activated', False) is True and r.mode() == 'active' and r.healthy() and \
                grant.get('role') == 'active' and grant.get('owner') == r.node and \
                grant.get('epoch', 0) > 0 and time.monotonic() < grant.get('deadline', 0):
            # A preparation job may hold the mutation lock. Existing writers
            # keep their valid lease; do not restart or thaw anything here.
            return True
        with r.lock(nonblocking=True):
            authority = r.info()
            if authority['owner'] != r.node or authority.get('pending') or \
                    authority.get('nodeMaintenance', {}).get(r.node, {}).get('enabled'):
                raise RuntimeError('Không có quyền promote PostgreSQL trên máy này')
            grant = getattr(r, 'grant', {})
            def valid_grant(margin=0):
                if grant.get('role') != 'active' or grant.get('epoch') != authority['epoch'] or \
                        time.monotonic() + margin >= grant.get('deadline', 0):
                    raise RuntimeError('Lease đã hết hoặc không khớp epoch; không mở quyền ghi')
            valid_grant(45)
            if r.mode() == 'frozen':
                return False
            value = self.state()
            if value['inRecovery']:
                if not self.can_promote(authority['epoch'] - 1, self.config['peer']):
                    raise RuntimeError('Bản PostgreSQL dự phòng chưa đủ điều kiện promote')
                # Authority was checked immediately before the only promote operation.
                if pg('SELECT pg_promote(true,30);', timeout=40) != 't':
                    raise RuntimeError('PostgreSQL promote chưa hoàn tất')
            changed = r.mode() != 'active'
            if changed:
                from ha_runtime import Syncthing
                Syncthing(r.config, r.endpoint).pause()
            r.write('mode', 'active')
            from ha_runtime import atomic, run
            valid_grant(5)
            atomic(GRANT, str(grant['deadline']), r.group)
            run('systemctl', 'start', 'immich-ml.service')
            run('systemctl', 'restart' if changed else 'start', 'immich-ha-app.service')
            r._activated = True
        return r.healthy()

    def fence(self, epoch, owner):
        r = self.runtime
        GRANT.unlink(missing_ok=True)
        from ha_runtime import run, Syncthing
        run('systemctl','stop','immich-ha-app.service','immich-ml.service')
        try:
            value = self.state()
        except Exception:
            value = None
        if owner == r.node:
            if r.mode() != 'frozen':
                r.write('mode', 'lease-paused')
            # Retain primary DB during a brief control-plane outage; no app writers.
            return
        if value and value['inRecovery']:
            return
        if value:
            ctl('stop')  # A former primary cannot follow a new timeline writable.
        if r.mode() not in ('receiving','frozen','maintenance'):
            r.write('mode', 'fenced')
            Syncthing(r.config, r.endpoint).pause()

    def wait_replay(self, snapshot, command, progress, timeout=180):
        r = self.runtime
        lsn = snapshot.get('walLsn', '')
        system = snapshot.get('systemId', '')
        if not LSN.fullmatch(lsn) or not re.fullmatch(r'\d+', system):
            raise RuntimeError('Thiếu mốc WAL hoặc định danh PostgreSQL nguồn')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r.guard(command, source=False)
            value = self.state()
            if not value['inRecovery'] or value['systemId'] != system:
                raise RuntimeError('PostgreSQL không thuộc bản sao vật lý của nguồn')
            if snapshot.get('kind') == 'wal-manifest-v1':
                if value['receiverConnected']:
                    if value.get('sender') != self.config['peer_ip'] or value.get('receiverTimeline') != snapshot['timeline']:
                        raise RuntimeError('Nguồn hoặc timeline WAL không khớp bàn giao')
                elif not command.get('final') or (r.seed() or {}).get('timeline') != snapshot['timeline']:
                    raise RuntimeError('Chưa xác nhận timeline WAL của máy dự phòng')
            if pg(f"SELECT COALESCE(pg_last_wal_replay_lsn()>='{lsn}'::pg_lsn,false);") == 't':
                return value
            progress('Đang chờ PostgreSQL áp dụng đủ WAL')
            time.sleep(2)
        raise RuntimeError('PostgreSQL dự phòng chưa theo kịp WAL nguồn')

    def barrier(self, command, progress, frozen=False):
        """Persist a file manifest and WAL barrier, never a logical database dump."""
        from ha_runtime import ROOT, MEDIA, atomic, digest, run
        r = self.runtime
        folder = MEDIA / 'backups/postgres'
        folder.mkdir(parents=True, exist_ok=True)
        records = ROOT / 'wal-barriers'
        records.mkdir(exist_ok=True)
        record = records / (command['id'] + '.json')
        guard = lambda: r.guard(command, source=True)
        guard()
        if frozen:
            r.write('mode', 'frozen')
            GRANT.unlink(missing_ok=True)
            run('systemctl', 'stop', 'immich-ha-app.service', 'immich-ml.service')
        if record.exists():
            result = json.loads(record.read_text())
            if result.get('source') != r.node or result.get('epoch') != command['epoch'] or \
                    result.get('commandId') != command['id'] or result.get('jobId') != command['jobId']:
                raise RuntimeError('Mốc WAL đã lưu không thuộc thao tác này')
            if digest(folder / result['manifestName']) != result['manifestSha256']:
                raise RuntimeError('Danh mục bàn giao đã lưu bị thay đổi')
        else:
            progress('Đang lập danh mục file và chốt mốc WAL')
            current = self.state()
            if current['inRecovery']:
                raise RuntimeError('Chỉ nguồn active được tạo mốc WAL')
            entries = r.manifest()
            name = 'wal-' + command['id'] + '.manifest.json.gz'
            temporary = folder.parent / ('.' + name + '.partial')
            try:
                with gzip.open(temporary, 'wt', encoding='utf-8') as stream:
                    json.dump(entries, stream, ensure_ascii=False, separators=(',', ':'))
                os.chown(temporary, r.group_owner, r.group)
                os.chmod(temporary, 0o640)
                guard()
                os.replace(temporary, folder / name)
            finally:
                temporary.unlink(missing_ok=True)
            # pg_walfile_name identifies the live timeline even after promotion.
            point = json.loads(pg("SELECT json_build_object('lsn',pg_current_wal_flush_lsn()::text,'walFile',pg_walfile_name(pg_current_wal_lsn()));"))
            result = {'kind': 'wal-manifest-v1', 'commandId': command['id'], 'jobId': command['jobId'],
                'source': r.node, 'epoch': command['epoch'], 'systemId': current['systemId'],
                'timeline': int(point['walFile'][:8], 16), 'walLsn': point['lsn'],
                'manifestName': name, 'manifestSha256': digest(folder / name), 'files': len(entries),
                'bytes': sum(x['size'] for x in entries), 'capturedAt': int(time.time()*1000),
                'stopped': False, 'peerAcknowledged': False}
            atomic(record, json.dumps(result))
        if frozen:
            if not result['peerAcknowledged']:
                until = time.monotonic() + 180
                while pg(f"SELECT EXISTS(SELECT 1 FROM pg_stat_replication WHERE client_addr='{self.config['peer_ip']}'::inet AND replay_lsn>='{result['walLsn']}'::pg_lsn);") != 't':
                    guard()
                    if time.monotonic() >= until:
                        raise RuntimeError('Máy đích chưa nhận đủ WAL cuối; chưa bàn giao')
                    progress('Đang chờ máy đích xác nhận WAL cuối')
                    time.sleep(2)
                result['peerAcknowledged'] = True
                atomic(record, json.dumps(result))
            guard()
            ctl('stop')
            result['stopped'] = True
            result.update(self.frozen_proof())
            atomic(record, json.dumps(result))
        return result

    def frozen_proof(self):
        """Confirm persistent fencing before a coordinator may shorten the wait."""
        from ha_runtime import ROOT
        r = self.runtime
        if r.mode() != 'frozen' or GRANT.exists():
            raise RuntimeError('Nguồn chưa khóa quyền ghi và cổng truy cập')
        units = subprocess.run(['systemctl','show','--property=ActiveState','--value',
            'immich-ha-app.service','immich-ml.service'],capture_output=True,text=True,timeout=10)
        states = units.stdout.split()
        database = subprocess.run(['pg_ctlcluster','16','ha','status'],capture_output=True,timeout=10)
        if units.returncode or states != ['inactive','inactive'] or database.returncode != 3:
            raise RuntimeError('Chưa xác nhận Immich và PostgreSQL nguồn đã dừng')
        # Survive power loss/reboot while old grants could still exist.
        with (ROOT/'mode').open('rb') as stream:
            os.fsync(stream.fileno())
        descriptor = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return {'fenceProtocol':1,'appStopped':True,'postgresStopped':True,
                'originFenced':True,'frozenDurable':True}

    def sync_wal(self, command, progress, sync, guard):
        from ha_runtime import MEDIA, digest, validate_manifest
        r = self.runtime
        ticket = command.get('snapshot') or {}
        if ticket.get('kind') != 'wal-manifest-v1' or ticket.get('source') != command['source'] or \
                ticket.get('epoch') != command['epoch'] or ticket.get('jobId') != command['jobId']:
            raise RuntimeError('Mốc WAL không thuộc phiên bàn giao hiện tại')
        origin = ticket.get('commandId', '')
        if not re.fullmatch(re.escape(command['jobId']) + r'-\d{1,3}', origin) or \
                ticket.get('manifestName') != 'wal-' + origin + '.manifest.json.gz' or \
                not re.fullmatch(r'[a-f0-9]{64}', ticket.get('manifestSha256', '')) or \
                not isinstance(ticket.get('timeline'), int) or isinstance(ticket['timeline'], bool) or ticket['timeline'] < 1:
            raise RuntimeError('Danh mục hoặc timeline WAL không hợp lệ')
        if not LSN.fullmatch(ticket.get('walLsn', '')) or not re.fullmatch(r'\d+', ticket.get('systemId', '')):
            raise RuntimeError('Mốc WAL không hợp lệ')
        if command.get('final') and (not ticket.get('stopped') or not ticket.get('peerAcknowledged')):
            raise RuntimeError('Nguồn chưa xác nhận dừng PostgreSQL sau WAL cuối')
        sync.configure('receiveonly', command.get('peerDeviceId'), progress, guard)
        try:
            value = self.state()
        except Exception:
            value = {}
        if command.get('final'):
            # Once the source is stopped, never try a new base backup or infer a lineage.
            if not value.get('inRecovery') or value.get('systemId') != ticket['systemId'] or \
                    (r.seed() or {}).get('timeline') != ticket['timeline']:
                raise RuntimeError('Máy đích chưa thuộc replica đã xác nhận trước khi chốt ghi')
        elif not value.get('inRecovery') or value.get('systemId') != ticket['systemId'] or \
                not value.get('receiverConnected') or value.get('sender') != self.config['peer_ip'] or \
                value.get('receiverTimeline') != ticket['timeline']:
            clone(self.config, guard, progress)
        value = self.wait_replay(ticket, command, progress)
        sync.wait(progress, guard=guard)
        manifest = MEDIA / 'backups/postgres' / ticket['manifestName']
        if digest(manifest) != ticket['manifestSha256']:
            raise RuntimeError('Danh mục file không khớp nguồn')
        with gzip.open(manifest, 'rt', encoding='utf-8') as stream:
            checked = validate_manifest(MEDIA, json.load(stream))
        if checked['files'] != ticket.get('files') or checked['bytes'] != ticket.get('bytes'):
            raise RuntimeError('Số lượng hoặc kích thước file không khớp mốc WAL')
        guard()
        r.write('current-db', self.config['database'])
        r.write('seed.json', json.dumps({'source': command['source'], 'sourceEpoch': command['epoch'],
            'systemId': value['systemId'], 'timeline': ticket['timeline'], 'walLsn': ticket['walLsn'],
            'appliedAt': int(time.time()*1000), 'mode': 'streaming'}))
        # A verified final barrier remains eligible even if file transfer took >15 min.
        if command.get('final'):
            r.write('stream-last-connected', str(int(time.time())))
        r.write('mode', 'standby')
        return {'streaming': True, 'systemId': value['systemId'], 'timeline': ticket['timeline'],
            'replayLsn': value['replayLsn'], 'manifestVerified': True, 'filesVerified': checked['files']}

    def execute(self, command, progress):
        from ha_runtime import Syncthing, run, validate_manifest, digest, MEDIA
        r = self.runtime
        action = command['action']
        source = action in ('send','dump','freeze-dump','wal-barrier','freeze-wal','wait-peer','resume')
        r.guard(command, source=source)
        with r.lock():
            r.guard(command, source=source)
            sync = Syncthing(r.config, r.endpoint)
            guard = lambda: r.guard(command, source=source)
            if action in ('wal-barrier', 'freeze-wal'):
                return self.barrier(command, progress, frozen=action == 'freeze-wal')
            if action == 'sync-wal':
                return self.sync_wal(command, progress, sync, guard)
            if action == 'archive':
                return {'preserved': True, 'mode': 'streaming'}
            if action == 'receive':
                GRANT.unlink(missing_ok=True)
                run('systemctl','stop','immich-ha-app.service','immich-ml.service')
                r.write('mode','receiving')
                r.write('seed.json','null')
                r.repair_media_access(progress, guard)
                sync.configure('receiveonly', command.get('peerDeviceId'), progress, guard)
                return {'direction':'receiveonly'}
            if action == 'send':
                sync.configure('sendonly',command.get('peerDeviceId'),progress,guard)
                return {'direction':'sendonly'}
            if action in ('dump','freeze-dump'):
                if action == 'freeze-dump':
                    r.write('mode','frozen')
                    GRANT.unlink(missing_ok=True)
                    run('systemctl','stop','immich-ha-app.service','immich-ml.service')
                result = r.snapshot(command,progress)
                result.update(systemId=self.state()['systemId'],walLsn=pg('SELECT pg_current_wal_flush_lsn();'))
                if action == 'freeze-dump':
                    # Ensure the final barrier has reached the replica before shutdown.
                    until = time.monotonic() + 120
                    while pg(f"SELECT EXISTS(SELECT 1 FROM pg_stat_replication WHERE replay_lsn>='{result['walLsn']}'::pg_lsn);") != 't':
                        guard()
                        if time.monotonic() >= until:
                            raise RuntimeError('Máy đích chưa nhận đủ WAL cuối; chưa bàn giao')
                        progress('Đang chờ xác nhận WAL cuối trên máy đích')
                        time.sleep(2)
                    guard()
                    ctl('stop')
                return result
            if action == 'wait-peer':
                for folder in ('immich-library','immich-database'):
                    sync.scan(folder,progress,guard)
                sync.wait(progress,command.get('peerDeviceId'),guard)
                return {'synced':True}
            if action == 'sync-restore':
                snapshot = command['snapshot']
                if not snapshot or snapshot.get('source') != command['source'] or snapshot.get('epoch') != command['epoch']:
                    raise RuntimeError('Bản sao không thuộc phiên nguồn hiện tại')
                sync.configure('receiveonly',command.get('peerDeviceId'),progress,guard)
                try:
                    value = self.state()
                except Exception:
                    value = {}
                seed = r.seed() or {}
                if not value.get('inRecovery') or value.get('systemId') != snapshot.get('systemId') or \
                        seed.get('sourceEpoch') not in (None,command['epoch']) or not value.get('receiverConnected') and not command.get('final'):
                    clone(self.config,guard,progress)
                value = self.wait_replay(snapshot,command,progress)
                sync.wait(progress,guard=guard)
                manifest = MEDIA / 'backups/postgres' / snapshot['manifestName']
                if digest(manifest) != snapshot['manifestSha256']:
                    raise RuntimeError('Danh mục ảnh không khớp bản nguồn')
                with gzip.open(manifest,'rt',encoding='utf-8') as stream:
                    validate_manifest(MEDIA,json.load(stream))
                guard()
                r.write('current-db',self.config['database'])
                r.write('seed.json',json.dumps({'source':command['source'],'sourceEpoch':command['epoch'],
                    'systemId':value['systemId'],'appliedAt':int(time.time()*1000),'mode':'streaming'}))
                r.write('mode','standby')
                return {'streaming':True,'replayLsn':value['replayLsn']}
            if action == 'validate':
                if not self.can_promote(command['epoch'],command['source']):
                    raise RuntimeError('PostgreSQL dự phòng chưa sẵn sàng promote')
                return {'healthy':True,'seedEpoch':command['epoch'],'streaming':True}
            if action == 'resume':
                ctl('start')
                if self.state()['inRecovery']:
                    raise RuntimeError('Không tự promote qua lệnh resume')
                r.write('mode','lease-paused')
                return {'resumed':True}
            raise ValueError('Unsupported streaming action')
