"""Reviewed native application upgrades. Never calls the generic LXC updater.

Only the 3.4.1 -> 3.4.2 CPU-ML recipe is supported: no schema, PostgreSQL,
VectorChord, plugin-core or ML runtime/dependency change in the reviewed diff.
Builds happen outside the live app; migration/start requires the durable freeze.
"""
import hashlib
import gzip
import json
import os
import platform
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time
import urllib.request
import urllib.parse

RECIPES = {'3.4.2': {'id':'native-3.4.2-r1','from':'3.4.1',
    'commit':'b6b4b80df113656bfa4872cab31c7319e89438a9',
    'sha256':'7957509737098bb21cafb23190559bb3d77b11e1c87725ca28d646f9a3ad58f2'}}
APP = Path('/opt/immich/app')
STORE = Path('/mnt/immich-storage/backups/immich-upgrades')
LOCK_NAME = 'immich-upgrade-lock.json'


def version(path=APP):
    return json.loads((path/'package.json').read_text())['version']


def tree_hash(root):
    """Hash relative names, file contents and link targets without following links."""
    value=hashlib.sha256()
    for parent,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=sorted(d for d in dirs if d not in ('__pycache__','.cache'))
        for name in sorted(dirs+files):
            p=Path(parent)/name
            if name.endswith(('.pyc','.pyo')):continue
            relative=p.relative_to(root).as_posix()
            value.update(relative.encode()+b'\0')
            if p.is_symlink():value.update(b'L'+os.readlink(p).encode()+b'\0')
            elif p.is_file():
                value.update(b'F')
                with p.open('rb') as f:
                    while block:=f.read(1024*1024):value.update(block)
            else:value.update(b'D')
    return value.hexdigest()


def fsync_dir(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def fsync_file(path):
    with path.open('r+b') as f:os.fsync(f.fileno())


def blocked(runtime):
    # Presence, including a malformed record, is a fail-closed activation guard.
    return bool(runtime.read(LOCK_NAME,''))


def sync_activation_block():
    from ha_runtime import ROOT
    for name in [LOCK_NAME,'mode']:
        fsync_file(ROOT/name)
    fsync_dir(ROOT)


def adapt_native_paths(bundle):
    # A package directory such as ipaddr.js is not a JavaScript source file.
    for suffix in ('*.js','*.mjs','*.cjs'):
        for path in bundle.rglob(suffix):
            if path.is_symlink() or not path.is_file():continue
            old=path.read_text(encoding='utf-8')
            new=old.replace('/usr/src','/opt/immich').replace("'/build'","'/opt/immich/app'")
            if new!=old:path.write_text(new,encoding='utf-8')


class Upgrader:
    def __init__(self,runtime,command,progress):
        self.r=runtime;self.c=command;self.progress=progress
        self.u=command.get('upgrade') or {};self.recipe=RECIPES.get(self.u.get('version'))
        if not self.recipe or self.u.get('recipe')!=self.recipe['id']:
            raise ValueError('Phiên bản chưa có recipe được kiểm chứng')
        stage=self.u.get('stageId','')
        if not re.fullmatch(r'[a-f0-9-]{36}',stage):raise ValueError('Invalid upgrade stage identity')
        if self.u.get('source')!=command['source'] or self.u.get('epoch')!=command['epoch']:
            raise ValueError('Upgrade authority differs')
        self.folder=STORE/stage
        self.manifest=self.folder/'prepared.json'
        self.bundle=self.folder/'bundle'
        self.next=APP.with_name('.ha-next-'+stage)
        self.old=APP.with_name('.ha-previous-'+stage)

    def guard(self):
        state=self.r.guard(self.c,source=self.r.node==self.c['source'])
        operation=state.get('operation') or {}
        if operation.get('id')!=self.c['jobId'] or operation.get('status') in ('failed','succeeded'):
            raise RuntimeError('Nâng cấp không còn hiện hành')
        return state

    def run(self,args,timeout=60,**kwargs):
        p=subprocess.run(args,capture_output=True,text=True,timeout=timeout,**kwargs)
        if p.returncode:raise RuntimeError('Nâng cấp thất bại tại '+Path(args[0]).name+'; xem log cục bộ, chưa tự hoàn tác database')
        return p.stdout.strip()

    def receipt(self,**extra):
        return {'version':self.u['version'],'recipe':self.recipe['id'],**extra}

    def preflight(self,allow_installed=False):
        from pg_stream import configuration,status
        self.guard();cfg=configuration();pg=status()
        self.protect_staging()
        os_release=Path('/etc/os-release').read_text()
        if platform.machine()!='x86_64' or not re.search(r'(?m)^VERSION_ID="?13"?$',os_release):
            raise RuntimeError('Recipe hiện đã kiểm chứng cho Debian 13 amd64')
        if self.r.resources().get('memory',{}).get('total',0)<6*1024**3:
            raise RuntimeError('Build recipe cần LXC có ít nhất 6 GiB RAM')
        if not cfg or cfg['database']!='immich_stream' or pg['inRecovery']!=(self.r.node!=self.c['source']):
            raise RuntimeError('Vai trò PostgreSQL không khớp; chưa nâng cấp')
        allowed=[self.recipe['from'],self.u['version']] if allow_installed else [self.recipe['from']]
        if version() not in allowed:raise RuntimeError('Phiên bản hiện tại không thuộc recipe')
        if not APP.is_dir() or APP.is_symlink():raise RuntimeError('Native app layout không phù hợp')
        if shutil.disk_usage(APP.parent).free<2300*1024**2 or shutil.disk_usage(STORE.parent).free<12*1024**3:
            raise RuntimeError('Cần ít nhất 2,3 GiB ổ hệ thống và 12 GiB ổ media cho staging/backup')
        if not self.run(['node','--version']).startswith('v24.'):raise RuntimeError('Recipe cần Node.js 24')
        if Path('/root/.openvino').exists() or Path('/root/.intel_version').exists():
            raise RuntimeError('Recipe đầu tiên chỉ hỗ trợ CPU ML; OpenVINO cần kiểm chứng riêng')
        extension=self.run(['runuser','-u','postgres','--','psql','-p','5433','-d','immich_stream','-XAt','-c',"SELECT extversion FROM pg_extension WHERE extname='vchord'"])
        if extension!='1.1.1':raise RuntimeError('VectorChord phải là 1.1.1 trên cả hai máy')
        for tool in ['corepack','npm','gcc','make','tar']:
            if not shutil.which(tool):raise RuntimeError('Thiếu công cụ build: '+tool)
        for path in ['machine-learning/ml-venv','plugins/immich-plugin-core']:
            if not (APP/path).is_dir():raise RuntimeError('Thiếu ML/plugin native đã kiểm chứng')
        return self.receipt(checked=True,currentVersion=version(),systemId=pg['systemId'])

    def protect_staging(self):
        from ha_runtime import Syncthing
        sync=Syncthing(self.r.config,self.r.endpoint)
        folder=sync.folder('immich-library')
        if folder.get('paused') or folder.get('type')!=('sendonly' if self.r.node==self.c['source'] else 'receiveonly'):
            raise RuntimeError('Chiều Syncthing hoặc trạng thái tạm dừng không phù hợp')
        path='/rest/db/ignores?folder=immich-library'
        current=sync.api(path).get('ignore',[])
        native='!/backups/immich-db-backup-*.sql.gz'
        if native not in current or any(line in ['/backups','/backups/','/backups/*','/backups/**'] for line in current[:current.index(native)]):
            raise RuntimeError('Cần cấu hình đồng bộ backup SQL gốc trước khi nâng cấp')
        rule='/backups/immich-upgrades'
        if not current or current[0]!=rule:
            self.guard()
            sync.api(path,{'ignore':[rule]+[line for line in current if line!=rule]},method='POST')
        if sync.api(path).get('ignore',[None])[0]!=rule:
            raise RuntimeError('Chưa xác nhận staging được loại trừ khỏi Syncthing')

    def verify(self):
        self.guard()
        self.ensure_postgres()
        m=json.loads(self.manifest.read_text())
        expected=(self.u.get('receipts') or {}).get(self.r.node)
        if not expected or m.get('sha256')!=expected.get('sha256') or m.get('version')!=self.u['version'] or m.get('recipe')!=self.recipe['id']:
            raise RuntimeError('Bản chuẩn bị không khớp lần xác nhận trên dashboard')
        if m.get('nodeVersion')!=self.run(['node','--version']):
            raise RuntimeError('Node.js đã đổi sau khi build; chuẩn bị lại')
        extension=self.run(['runuser','-u','postgres','--','psql','-p','5433','-d','immich_stream','-XAt','-c',"SELECT extversion FROM pg_extension WHERE extname='vchord'"])
        if extension!='1.1.1':raise RuntimeError('VectorChord đã đổi; chưa áp dụng bản chuẩn bị')
        if tree_hash(self.bundle)!=m['sha256']:raise RuntimeError('Checksum bản chuẩn bị không khớp')
        if APP.exists():
            if APP.is_symlink() or version() not in [self.recipe['from'],self.u['version']]:raise RuntimeError('Code hiện tại đã thay đổi ngoài quy trình')
        elif not (self.old.is_dir() and not self.old.is_symlink() and version(self.old)==self.recipe['from'] and
                  self.next.is_dir() and not self.next.is_symlink() and version(self.next)==self.u['version'] and tree_hash(self.next)==m['sha256']):
            raise RuntimeError('App bị thiếu; chưa có candidate/backup đủ điều kiện tiếp tục')
        return m

    def ensure_postgres(self):
        """Resume a rebooted upgrade without promoting or replacing any PGDATA."""
        from pg_stream import status,ctl,DATA
        expected=self.u.get('systemId','')
        if not re.fullmatch(r'\d+',expected):raise RuntimeError('Thiếu định danh database trước nâng cấp')
        standby=self.r.node!=self.c['source']
        try:value=status()
        except RuntimeError:
            if (DATA/'standby.signal').is_file()!=standby:
                raise RuntimeError('Vai trò PGDATA không khớp; không tự promote để tiếp tục nâng cấp')
            if not standby:
                grant=getattr(self.r,'grant',{})
                if grant.get('role')!='active' or grant.get('owner')!=self.r.node or grant.get('epoch')!=self.c['epoch'] or time.monotonic()+30>=grant.get('deadline',0):
                    raise RuntimeError('Cần lease active hợp lệ để mở lại PostgreSQL nguồn')
            text=self.run(['/usr/lib/postgresql/16/bin/pg_controldata',str(DATA)],env={**os.environ,'LC_ALL':'C'})
            found=re.search(r'Database system identifier:\s+(\d+)',text)
            if not found or found[1]!=expected:raise RuntimeError('PGDATA không thuộc dòng database của lần nâng cấp')
            self.guard();ctl('start');value=status()
        if value['inRecovery']!=standby or value['systemId']!=expected:
            raise RuntimeError('Vai trò/dòng dữ liệu PostgreSQL không phù hợp khi tiếp tục')
        # A cold boot may have paused sync while the role was unknown. Re-enable
        # only the direction bound to this still-current owner/epoch.
        from ha_runtime import Syncthing
        sync=Syncthing(self.r.config,self.r.endpoint)
        kind='receiveonly' if standby else 'sendonly'
        folders=[sync.folder(name) for name in ['immich-library','immich-database']]
        if any(folder.get('paused') or folder.get('type')!=kind for folder in folders):
            sync.configure(kind,self.c.get('peerDeviceId'),self.progress,self.guard)
        if standby and self.r.mode()=='fenced':self.r.write('mode','standby')

    def build(self):
        self.preflight()
        if self.manifest.exists():
            m=json.loads(self.manifest.read_text())
            if m.get('recipe')==self.recipe['id'] and m.get('version')==self.u['version'] and tree_hash(self.bundle)==m.get('sha256'):
                return m
            raise RuntimeError('Staging cũ không khớp; chuẩn bị lại bằng job mới')
        self.folder.mkdir(parents=True,exist_ok=True,mode=0o750)
        archive=self.folder/'source.tar.gz'
        self.progress('Tải source đã ghim commit và SHA-256')
        request=urllib.request.Request('https://codeload.github.com/immich-app/immich/tar.gz/'+self.recipe['commit'],headers={'User-Agent':'ImmichHA-Upgrade/1'})
        temporary=archive.with_suffix('.partial')
        try:
            with urllib.request.urlopen(request,timeout=60) as response,temporary.open('wb') as f:
                size=0
                while block:=response.read(1024*1024):
                    size+=len(block)
                    if size>128*1024**2:raise RuntimeError('Source archive quá lớn')
                    f.write(block)
            if hashlib.sha256(temporary.read_bytes()).hexdigest()!=self.recipe['sha256']:
                raise RuntimeError('SHA-256 source không khớp recipe')
            os.replace(temporary,archive)
        finally:temporary.unlink(missing_ok=True)
        source=self.folder/'source'
        if source.exists():raise RuntimeError('Build trước bị gián đoạn; dùng job chuẩn bị mới để không ghi đè')
        source.mkdir()
        with tarfile.open(archive) as tar:tar.extractall(source,filter='data')
        source=source/('immich-'+self.recipe['commit'])
        # This reviewed patch changes only ML's version label, not its Python
        # implementation/dependencies, and leaves plugin-core unchanged.
        for p in (source/'machine-learning/immich_ml').rglob('*.py'):
            relative=p.relative_to(source/'machine-learning')
            expected=p.read_text().replace('"/cache"','"/opt/immich/cache"')
            if not (APP/'machine-learning'/relative).is_file() or (APP/'machine-learning'/relative).read_text()!=expected:
                raise RuntimeError('ML runtime đã tùy chỉnh hoặc không khớp recipe; chưa tái sử dụng')
        log=self.folder/'build.log'
        env={k:v for k,v in os.environ.items() if k in ['PATH','LANG','LC_ALL','TZ']}
        home=self.folder/'build-home';home.mkdir(exist_ok=True)
        env.update(HOME=str(home),COREPACK_HOME=str(home/'corepack'),PNPM_HOME=str(home/'pnpm'),
            CI='1',HUSKY='0',COREPACK_ENABLE_DOWNLOAD_PROMPT='0',NODE_OPTIONS='--max-old-space-size=3072')
        script=Path(__file__).with_name('build-immich-release.sh')
        self.progress('Build server và giao diện trong staging; app hiện tại vẫn phục vụ')
        with log.open('w') as output:
            process=subprocess.Popen(['bash',str(script),str(source),str(self.bundle)],stdout=output,stderr=subprocess.STDOUT,env=env,start_new_session=True)
            started=time.monotonic()
            try:
                while process.poll() is None:
                    if time.monotonic()-started>3600:raise RuntimeError('Build vượt 60 phút')
                    self.guard();self.progress('Đang build '+self.u['version']+' · xem build.log trên node nếu cần')
                    time.sleep(10)
                if process.returncode:raise RuntimeError('Build thất bại; giữ nguyên code hiện tại. Log: '+str(log))
            except BaseException:
                import signal
                if process.poll() is None:
                    os.killpg(process.pid,signal.SIGTERM)
                    try:process.wait(timeout=15)
                    except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
                raise
        self.guard()
        if version(self.bundle)!=self.u['version']:raise RuntimeError('Build sai phiên bản')
        self.run(['node','--check',str(self.bundle/'dist/main.js')])
        self.run(['node','--input-type=module','-e',
            "const sharp=(await import('sharp')).default; await sharp({create:{width:1,height:1,channels:3,background:'#fff'}}).jpeg().toBuffer(); await import('@extism/extism'); await import('@futo-org/backups-orchestrator-api');"],
            cwd=self.bundle,env=env,timeout=90)
        for root,dirs,files in os.walk(self.bundle,followlinks=False):
            for name in dirs+files:
                path=Path(root)/name
                if path.is_symlink() and not path.resolve().is_relative_to(self.bundle.resolve()):
                    raise RuntimeError('Build không tự chứa đủ dependencies; có symlink ra ngoài bundle')
        adapt_native_paths(self.bundle)
        # Reuse the unchanged, verified ML runtime and plugin assets. Never run
        # pip/apt/ALTER EXTENSION or the upstream updater against a live node.
        for name in ['machine-learning','plugins']:
            shutil.copytree(APP/name,self.bundle/name,symlinks=True,dirs_exist_ok=True)
        for name in ['upload','geodata']:
            if os.path.lexists(self.bundle/name):
                raise RuntimeError('Build contains unexpected storage link')
            os.symlink('/mnt/immich-storage' if name=='upload' else '/opt/immich/geodata',self.bundle/name)
        # Native paths and HTTP CSP follow the installed community-script layout.
        helmet=self.bundle/'helmet.json'
        if helmet.exists():
            data=json.loads(helmet.read_text());data.get('contentSecurityPolicy',{}).get('directives',{}).pop('upgrade-insecure-requests',None);helmet.write_text(json.dumps(data))
        account=__import__('pwd').getpwnam('immich')
        for root,dirs,files in os.walk(self.bundle,followlinks=False):
            os.chown(root,account.pw_uid,account.pw_gid)
            for name in dirs+files:os.chown(Path(root)/name,account.pw_uid,account.pw_gid,follow_symlinks=False)
        m=self.receipt(stageId=self.u['stageId'],sha256=tree_hash(self.bundle),nodeVersion=self.run(['node','--version']),builtAt=int(time.time()*1000))
        from ha_runtime import atomic
        atomic(self.manifest,json.dumps(m))
        return m

    def freeze(self):
        if self.r.node!=self.c['source']:raise RuntimeError('Chỉ nguồn active được dừng ghi')
        self.verify()
        self.r.write(LOCK_NAME,json.dumps({'stageId':self.u['stageId'],'source':self.c['source'],'epoch':self.c['epoch']}))
        self.r.write('mode','upgrade-frozen');self.r._activated=False
        sync_activation_block()
        self.run(['systemctl','stop','immich-ha-app.service','immich-ml.service'],timeout=120)
        if subprocess.run(['systemctl','is-active','--quiet','immich-ha-app.service']).returncode==0:
            raise RuntimeError('Ứng dụng chưa dừng ghi')
        return self.receipt(appStopped=True)

    def backup(self):
        if not blocked(self.r):raise RuntimeError('Chưa đóng ứng dụng để tạo backup cuối')
        path=self.folder/'before-upgrade.dump';record=self.folder/'backup.json'
        if record.exists():
            m=json.loads(record.read_text())
            if hashlib.sha256(path.read_bytes()).hexdigest()!=m['sha256']:raise RuntimeError('Backup đã thay đổi')
            return self.export_backup(m,record)
        if path.exists():
            # A crash after the dump rename must not overwrite the original.
            self.run(['pg_restore','--list',str(path)],timeout=90)
            m=self.receipt(verified=True,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),path=str(path))
            from ha_runtime import atomic
            atomic(record,json.dumps(m))
            fsync_file(record)
            fsync_dir(self.folder)
            return self.export_backup(m,record)
        temporary=path.with_suffix('.partial')
        try:
            with temporary.open('wb') as f:
                os.chmod(temporary,0o600)
                result=subprocess.run(['runuser','-u','postgres','--','pg_dump','-p','5433','-Fc','-d','immich_stream'],stdout=f,stderr=subprocess.PIPE,timeout=1800)
                if result.returncode:raise RuntimeError('Backup database thất bại; chưa cài bản mới')
                f.flush();os.fsync(f.fileno())
            self.run(['pg_restore','--list',str(temporary)],timeout=90)
            self.guard();os.replace(temporary,path);fsync_dir(self.folder)
            m=self.receipt(verified=True,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),path=str(path))
            from ha_runtime import atomic
            atomic(record,json.dumps(m))
            fsync_file(record)
            fsync_dir(self.folder)
            return self.export_backup(m,record)
        finally:temporary.unlink(missing_ok=True)

    def export_backup(self,record,path):
        """Export verified dump as a normal flat SQL.gz for existing backup sync."""
        from ha_runtime import atomic,digest,Syncthing,MEDIA
        name='immich-db-backup-'+time.strftime('%Y%m%dT%H%M%S')+'-preupgrade-'+self.u['stageId']+'.sql.gz'
        if record.get('replicaName'):name=record['replicaName']
        destination=MEDIA/'backups'/name
        if not record.get('replicaSha256'):
            temporary=destination.with_name('.'+name+'.partial')
            try:
                process=subprocess.Popen(['pg_restore','--file=-',str(self.folder/'before-upgrade.dump')],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                with temporary.open('wb') as output:
                    os.chmod(temporary,0o600)
                    with gzip.GzipFile(fileobj=output,mode='wb',mtime=0) as compressed:
                        shutil.copyfileobj(process.stdout,compressed,1024*1024)
                    output.flush();os.fsync(output.fileno())
                _,error=process.communicate(timeout=90)
                if process.returncode:raise RuntimeError('Không xuất được bản SQL dự phòng; chưa cài code')
                self.guard();os.replace(temporary,destination)
                os.chown(destination,self.r.group_owner,self.r.group);os.chmod(destination,0o640)
                fsync_dir(destination.parent)
                record.update(replicaName=name,replicaSha256=digest(destination))
                atomic(path,json.dumps(record))
                fsync_file(path)
                fsync_dir(path.parent)
            finally:temporary.unlink(missing_ok=True)
        if not destination.is_file() or digest(destination)!=record['replicaSha256']:
            raise RuntimeError('Bản SQL dùng để chép sang dự phòng đã thay đổi')
        sync=Syncthing(self.r.config,self.r.endpoint)
        self.progress('Đưa backup SQL vào hàng đợi đồng bộ')
        sync.api('/rest/db/scan?'+urllib.parse.urlencode({'folder':'immich-library','sub':'backups/'+name}),{},'POST',timeout=180)
        self.guard()
        return record

    def copy_backup(self):
        from ha_runtime import MEDIA,digest,atomic,Syncthing
        proof=self.c.get('upgradeBackup') or {}
        name=proof.get('replicaName','');sha=proof.get('replicaSha256','')
        if self.r.node==self.c['source'] or not re.fullmatch(r'immich-db-backup-[A-Za-z0-9.-]+\.sql\.gz',name) or not re.fullmatch('[a-f0-9]{64}',sha):
            raise RuntimeError('Thông tin backup từ active không hợp lệ')
        path=MEDIA/'backups'/name;deadline=time.monotonic()+1200
        sync=Syncthing(self.r.config,self.r.endpoint)
        while not path.is_file() or digest(path)!=sha:
            self.guard()
            if time.monotonic()>deadline:raise RuntimeError('Chưa nhận đủ backup SQL trên máy dự phòng')
            self.progress('Đang chờ backup SQL tới máy dự phòng qua Syncthing')
            time.sleep(3)
        with gzip.open(path,'rb') as f:
            while f.read(1024*1024):pass
        destination=self.folder/'before-upgrade.sql.gz'
        shutil.copyfile(path,destination);os.chmod(destination,0o600)
        fsync_file(destination)
        result=self.receipt(copied=True,sha256=digest(destination))
        if result['sha256']!=sha:raise RuntimeError('Backup SQL dự phòng chưa khớp checksum')
        atomic(self.folder/'backup-copy.json',json.dumps(result));fsync_dir(self.folder)
        return result

    def install(self):
        m=self.verify()
        proof=self.c.get('upgradeBackup') or {}
        if proof.get('verified') is not True or not re.fullmatch('[a-f0-9]{64}',proof.get('sha256','')) or proof.get('version')!=self.u['version']:
            raise RuntimeError('Chưa có backup cuối đã được xác minh trước khi cài')
        copy=self.c.get('upgradeBackupCopy') or {}
        if copy.get('copied') is not True or copy.get('sha256')!=proof.get('replicaSha256'):
            raise RuntimeError('Chưa xác nhận bản backup trên cả hai máy')
        if self.r.node==self.c['source'] and not blocked(self.r):raise RuntimeError('Chưa khóa active trước khi đổi code')
        self.run(['systemctl','stop','immich-ha-app.service','immich-ml.service'],timeout=120)
        if APP.exists() and version()==self.u['version']:
            if tree_hash(APP)!=m['sha256']:raise RuntimeError('Code phiên bản mới đã thay đổi; dừng kiểm tra')
            return self.receipt(installed=True)
        if not self.next.exists():shutil.copytree(self.bundle,self.next,symlinks=True)
        if tree_hash(self.next)!=m['sha256']:raise RuntimeError('Copy candidate chưa khớp checksum')
        self.guard()
        if APP.exists():
            if self.old.exists():raise RuntimeError('Bản code cũ đã có; không ghi đè')
            os.replace(APP,self.old);fsync_dir(APP.parent)
        os.replace(self.next,APP);fsync_dir(APP.parent)
        # copytree creates root-owned copies; preserve immich's ability to run ML.
        account=__import__('pwd').getpwnam('immich')
        for root,dirs,files in os.walk(APP,followlinks=False):
            os.chown(root,account.pw_uid,account.pw_gid)
            for name in dirs+files:os.chown(Path(root)/name,account.pw_uid,account.pw_gid,follow_symlinks=False)
        Path('/root/.immich').write_text(self.u['version']+'\n')
        return self.receipt(installed=True)

    def start(self):
        from pg_stream import status,GRANT
        from ha_runtime import atomic
        self.guard()
        grant=getattr(self.r,'grant',{})
        if self.r.node!=self.c['source'] or grant.get('role')!='active' or grant.get('owner')!=self.r.node or grant.get('epoch')!=self.c['epoch'] or time.monotonic()+30>=grant.get('deadline',0):
            raise RuntimeError('Không có lease active đủ hạn để khởi động bản mới')
        if status()['inRecovery'] or version()!=self.u['version']:raise RuntimeError('Sai vai trò hoặc phiên bản')
        proof=json.loads((self.folder/'backup.json').read_text())
        if not proof.get('verified') or hashlib.sha256((self.folder/'before-upgrade.dump').read_bytes()).hexdigest()!=proof.get('sha256'):
            raise RuntimeError('Backup trước nâng cấp chưa được xác minh')
        # Only the new code may run. A migration failure never selects old code
        # or restores a database automatically.
        # The ordinary activate() path cannot refresh this file while execute()
        # holds the mutation lock. app-run-v2 must see the fresh coordinator
        # grant before systemd starts, rather than an expired pre-freeze lease.
        self.guard()
        if time.monotonic()+5>=grant['deadline']:raise RuntimeError('Lease đã hết trước khi mở bản mới')
        atomic(GRANT,str(grant['deadline']),self.r.group)
        self.r.write('mode','active')
        self.r.write(LOCK_NAME,'')
        sync_activation_block()
        self.run(['systemctl','start','immich-ml.service','immich-ha-app.service'],timeout=60)
        self.r.wait_healthy(self.progress,timeout=180)
        return self.receipt(started=True)

    def execute(self):
        action=self.c['action'];self.guard()
        with self.r.lock():
            self.guard()
            if action=='upgrade-preflight':return self.preflight()
            if action=='upgrade-build':return self.build()
            if action=='upgrade-verify':self.verify();return self.receipt(checked=True)
            if action=='upgrade-freeze':return self.freeze()
            if action=='upgrade-backup':return self.backup()
            if action=='upgrade-copy-backup':return self.copy_backup()
            if action=='upgrade-install':return self.install()
            if action=='upgrade-start':return self.start()
            if action=='upgrade-check':
                if version()!=self.u['version']:raise RuntimeError('Phiên bản cài đặt không khớp')
                if self.r.node==self.c['source']:
                    if not self.r.healthy():raise RuntimeError('Immich chưa khỏe')
                elif not self.r.streaming.can_promote(self.c['epoch'],self.c['source']):raise RuntimeError('Replica chưa sẵn sàng')
                return self.receipt(checked=True)
            raise ValueError('Unknown fixed upgrade action')
