"""Mandatory shared admission. Cloudflare proposes; etcd admits one writer.

The production namespace is separate from observations and disposable tests.
"""
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid

from quorum_lock import Etcd, ShadowLock, b64, validate_config

CONFIG = Path('/etc/immich-ha/quorum-authority.json')
REQUIRED = Path('/etc/immich-ha/quorum-required')
GRANT = Path('/run/immich-ha-quorum-grant.json')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
MARGIN = 30


def boot_time():
    # Unlike monotonic(), BOOTTIME counts time spent with the host suspended.
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def validate_enforced_config(config):
    if config.get('mode') != 'enforced' or type(config.get('fallbackEnabled')) is not bool:
        raise ValueError('Explicit enforced admission/fallback setting required')
    validate_config({**config, 'mode': 'shadow'})
    return config


class ControlLock(ShadowLock):
    def __init__(self, client, cluster):
        super().__init__(client, cluster)
        self.prefix = '/immich-ha/' + cluster + '/control/admission/'
        self.keys = [self.prefix + 'generation', self.prefix + 'active']

    def claim(self, authority, owner, epoch, session, ttl, resume=False, policy=None, transition_from=None, handoff_nonce=None):
        if authority not in ('cloudflare', 'fallback', 'handoff') or owner not in ('primary', 'backup') or type(epoch) is not int or epoch < 1:
            raise ValueError('Invalid proposed authority')
        if not re.fullmatch('[a-zA-Z0-9_-]{16,100}', session) or type(ttl) is not int or not 5 <= ttl <= 300:
            raise ValueError('Invalid proposed session/TTL')
        proposal = dict(version=1, authority=authority, owner=owner, epoch=epoch,
                        session=session, claim=uuid.uuid4().hex)
        identity = ('authority', 'owner', 'epoch', 'session')
        for attempt in range(3):
            rows = self.read()
            previous, active = [row['value'] for row in rows]
            transition = transition_from is not None and previous == transition_from and not previous.get('released')
            if transition_from is not None and not transition:return dict(admitted=False,reason='concurrent-update')
            same_writer = previous and all(previous[k] == proposal[k] for k in ('owner','session')) and epoch >= previous['epoch']
            if transition and active and not same_writer:return dict(admitted=False,reason='lease-held')
            if previous:
                same_owner = previous['owner']==owner and previous['session']==session
                if authority in ('fallback','handoff') and (previous.get('released') or previous.get('localBlocked') or previous.get('policy',{}).get('blocked',True) or (not same_owner and not previous.get('policy',{}).get('auto',False))):
                    return dict(admitted=False,reason='fallback-blocked')
                if epoch < previous['epoch'] or (epoch == previous['epoch'] and any(proposal[k] != previous[k] for k in identity) and not (transition and same_writer)):
                    return dict(admitted=False, reason='stale-generation')
                # A durable freeze is a tombstone, including across agent reboot.
                # Only a coordinator's explicit resume of this exact writer may
                # reopen this generation. Routine heartbeats cannot thaw it.
                if previous.get('released') and epoch == previous['epoch'] and not (resume and authority == 'cloudflare'):
                    return dict(admitted=False, reason='durably-released')
            if active and any(proposal[k] != active[k] for k in identity) and not (transition and same_writer):
                return dict(admitted=False, reason='lease-held')
            old_policy = (previous or {}).get('policy',dict(sequence=0,auto=False,blocked=True,excluded={}))
            proposal['policy'] = policy if policy and policy['sequence'] >= old_policy['sequence'] else old_policy
            proposal['localBlocked'] = bool((previous or {}).get('localBlocked')) if previous and epoch == previous['epoch'] else False
            if authority in ('fallback','handoff'):
                proposal['fallbackTerm'] = previous.get('fallbackTerm') if previous and previous['authority'] in ('fallback','handoff') else uuid.uuid4().hex
            elif previous and previous.get('fallbackTerm'):
                proposal['fallbackTerm']=previous['fallbackTerm']
                if previous.get('lastHandoffNonce'):proposal['lastHandoffNonce']=previous['lastHandoffNonce']
            if handoff_nonce is not None:proposal['handoffNonce'] = handoff_nonce
            elif previous and authority == previous['authority'] and previous.get('handoffNonce'):proposal['handoffNonce'] = previous['handoffNonce']
            lease = self.client.call('/v3/lease/grant', {'TTL': ttl})
            lease_id = str(lease.get('ID', '0'))
            if not re.fullmatch('-?[0-9]{1,20}', lease_id) or int(lease_id) == 0 or int(lease.get('TTL', 0)) < ttl:
                raise RuntimeError('Invalid etcd lease')
            compares = self.compares(rows)
            encoded = b64(json.dumps(proposal, separators=(',', ':')))
            result = self.client.call('/v3/kv/txn', {'compare': compares, 'success': [
                {'request_put': {'key': b64(self.keys[0]), 'value': encoded}},
                {'request_put': {'key': b64(self.keys[1]), 'value': encoded, 'lease': lease_id}}], 'failure': []})
            confirmed = result.get('succeeded') is True
            if not confirmed:
                current = self.read()
                confirmed = all(row['value'] == proposal for row in current) and current[1]['lease'] == lease_id
            if confirmed:
                old = rows[1]['lease']
                if old != '0' and old != lease_id:
                    try: self.client.call('/v3/lease/revoke', {'ID': old})
                    except RuntimeError: pass
                return dict(admitted=True, record=proposal, ttl=int(lease['TTL']))
            # Exceptions above leave an uncertain lease intact until native TTL.
            self.client.call('/v3/lease/revoke', {'ID': lease_id})
        return dict(admitted=False, reason='concurrent-update')

    def compares(self, rows):
        return [{'target': 'MOD', 'result': 'EQUAL', 'key': b64(key), 'mod_revision': str(row['revision'])}
                for key, row in zip(self.keys, rows)]

    def metadata(self, owner, epoch, change):
        for attempt in range(3):
            rows = self.read();previous,active = [r['value'] for r in rows]
            if not previous or previous['owner'] != owner or previous['epoch'] != epoch:return False
            updated = change(dict(previous))
            if updated == previous:return True
            operations=[{'request_put':{'key':b64(self.keys[0]),'value':b64(json.dumps(updated))}}]
            if active:
                operations.append({'request_put':{'key':b64(self.keys[1]),'value':b64(json.dumps(updated)),'lease':rows[1]['lease']}})
            result=self.client.call('/v3/kv/txn',{'compare':self.compares(rows),'success':operations,'failure':[]})
            if result.get('succeeded'):return True
        raise RuntimeError('Concurrent quorum metadata update')

    def update_policy(self, owner, epoch, policy):
        def change(value):
            if value['authority']=='cloudflare' and policy['sequence']>value.get('policy',{}).get('sequence',0):value['policy']=policy
            return value
        return self.metadata(owner,epoch,change)

    def release(self, owner, epoch, session):
        rows = self.read()
        previous, active = [row['value'] for row in rows]
        if not previous or any(previous[k] != v for k, v in dict(owner=owner, epoch=epoch, session=session).items()):
            raise RuntimeError('Cannot release another authority')
        if previous.get('released') and active is None: return
        if active and any(active[k] != previous[k] for k in ('owner', 'epoch', 'session', 'authority')):
            raise RuntimeError('Active authority differs')
        tombstone = {**previous, 'released': True}
        result = self.client.call('/v3/kv/txn', {'compare': self.compares(rows), 'success': [
            {'request_put': {'key': b64(self.keys[0]), 'value': b64(json.dumps(tombstone))}},
            {'request_delete_range': {'key': b64(self.keys[1])}}], 'failure': []})
        if not result.get('succeeded'): raise RuntimeError('Authority changed during durable release')


class Admission:
    def __init__(self, config, session, group, client=None, clock=boot_time, grant_path=GRANT):
        self.config = validate_enforced_config(config)
        self.session, self.group, self.clock = session, group, clock
        self.path = grant_path
        self.lock = ControlLock(client or Etcd(config, validate_enforced_config), config['clusterId'])
        self.mutex = threading.RLock()
        self.record = None
        self.deadline = 0
        self.state = 'waiting'

    def clear(self):
        with self.mutex:
            self.deadline = 0
            self.record = None
            self.path.unlink(missing_ok=True)

    def admit(self, view, resume=False, policy=None, ack=None):
        with self.mutex:
            remaining = view['deadline'] - time.monotonic()
            if remaining < 10: raise RuntimeError('Cloudflare lease too short for quorum admission')
            started = self.clock()
            transition=None
            if ack:
                current=self.lock.read()[1]['value']
                if current and current['authority']=='handoff' and current.get('handoffNonce')==ack and current['owner']==self.config['node'] and current['epoch']==view['epoch'] and current['session']==self.session:transition=current
            result = self.lock.claim('cloudflare', self.config['node'], view['epoch'], self.session,
                                     min(300, math.ceil(remaining) + MARGIN + 5), resume=resume,policy=policy,transition_from=transition)
            if not result['admitted']:
                self.clear()
                self.state = result['reason']
                raise RuntimeError('Quorum admission denied: ' + result['reason'])
            if ack and result['record'].get('fallbackTerm'):
                nonce=ack
                self.lock.metadata(self.config['node'],view['epoch'],lambda v:{**v,'lastHandoffNonce':nonce})
                result['record']['lastHandoffNonce']=nonce
            self.accept(result,started,min(remaining,result['ttl']-MARGIN))
            self.state = 'admitted'

    def accept(self,result,started,duration):
        self.record=result['record'];self.deadline=started+duration
        self.assert_grant(self.record['epoch'])
        value = dict(clusterId=self.config['clusterId'], node=self.config['node'], epoch=self.record['epoch'],
                     bootId=BOOT_ID.read_text().strip(), deadline=self.deadline)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value))
        os.chmod(temporary, 0o640)
        os.chown(temporary, 0, self.group)
        os.replace(temporary, self.path)

    def local_block(self, blocked):
        with self.mutex:
            rows=self.lock.read();previous=rows[0]['value']
            if not previous or previous['owner']!=self.config['node'] or previous['session']!=self.session:return
            if not blocked:self.assert_grant(previous['epoch'],fresh=True)
            self.lock.metadata(previous['owner'],previous['epoch'],lambda v:{**v,'localBlocked':blocked})

    def valid(self, epoch, margin=0):
        with self.mutex:
            return bool(self.record and self.record['epoch'] == epoch and self.clock() + margin < self.deadline)

    def assert_grant(self, epoch, margin=0, fresh=False):
        with self.mutex:
            if not self.valid(epoch, margin): raise RuntimeError('Quorum grant expired or mismatched')
            if fresh:
                active = self.lock.read()[1]
                if active['lease'] == '0' or not active['value'] or any(active['value'][k] != self.record[k] for k in ('owner', 'epoch', 'session', 'authority')):
                    self.clear()
                    raise RuntimeError('Quorum no longer admits this writer')
                if not self.valid(epoch, margin): raise RuntimeError('Quorum grant expired while checking')

    def guard_command(self, command):
        with self.mutex:
            generation = self.lock.read()[0]['value']
            if not generation or generation['authority'] != 'cloudflare' or generation['owner'] != command['source'] or generation['epoch'] != command['epoch']:
                raise RuntimeError('Command conflicts with shared authority')

    def release_frozen(self, epoch):
        with self.mutex:
            self.lock.release(self.config['node'], epoch, self.session)
            self.clear()
            self.state = 'durably-released'

    def telemetry(self):
        return dict(protocol=2, mode='enforced', state=self.state, authority=(self.record or {}).get('authority'),fallbackEnabled=self.config['fallbackEnabled'])


def runtime_admission(runtime):
    admission = vars(runtime).get('quorum_authority')
    if REQUIRED.exists() and admission is None:
        raise RuntimeError('Mandatory quorum admission is not initialized')
    return admission


def assert_writer(runtime, margin=0, fresh=True):
    admission = runtime_admission(runtime)
    if admission: admission.assert_grant(getattr(runtime, 'grant', {}).get('epoch'), margin, fresh)


def control_policy(config, response):
    value=response.get('quorum')
    if not value or value.get('protocol')!=2 or value.get('clusterId')!=config['clusterId']:
        raise RuntimeError('Cloudflare quorum protocol/identity not confirmed')
    if type(value.get('sequence')) is not int or value['sequence']<1 or type(value.get('auto')) is not bool or type(value.get('blocked')) is not bool:
        raise RuntimeError('Invalid coordinator policy')
    excluded=value.get('excluded',{})
    if set(excluded)!= {'primary','backup'} or any(type(v) is not bool for v in excluded.values()):raise RuntimeError('Invalid maintenance policy')
    return {k:value[k] for k in ('sequence','auto','blocked','excluded')}


class Fallback:
    """Same-key fallback, with conservative automatic promotion and CF handback.

    The agents are trusted node controllers authenticated by existing CF tokens.
    Etcd mTLS/majority admits writers; CF adopts a reported writer only after a
    nonce is committed into that writer's live quorum record and echoed back.
    """
    TTL=180
    FAILURE_SECONDS=300
    CF_GRACE=15

    def __init__(self,agent):
        self.agent=agent;self.a=agent.quorum_authority;self.failed_at=None
        self.absent_since=None;self.absent_identity=None;self.warming_until=0

    def local(self):
        return (self.a.record or {}).get('authority') in ('fallback','handoff') and self.a.valid((self.a.record or {}).get('epoch'))

    def info(self):
        self.a.assert_grant(self.a.record['epoch'],fresh=True)
        return dict(owner=self.a.record['owner'],epoch=self.a.record['epoch'],pending=None,
                    nodeMaintenance={},operation=None,ready=self.agent.view.get('ready',False),auto=False)

    def report(self):
        with self.a.mutex:
            active=self.a.lock.read()[1];record=active['value']
            if not record or active['lease']=='0':return None
            own=record['owner']==self.agent.node and record['session']==self.agent.session
            return dict(clusterId=self.a.config['clusterId'],owner=record['owner'],epoch=record['epoch'],
                session=record['session'],authority=record['authority'],live=True,
                localGrant=bool(own and self.a.valid(record['epoch'])),handoffNonce=record.get('handoffNonce'),
                term=record.get('fallbackTerm'),cfAck=record.get('lastHandoffNonce'))

    def failure(self):
        if self.failed_at is None:self.failed_at=self.a.clock()
        self.agent.next_poll_seconds=5
        self.maintain()

    def publish_view(self):
        remaining=max(0,self.a.deadline-self.a.clock())
        self.agent.view=dict(role='active',owner=self.agent.node,epoch=self.a.record['epoch'],
                            deadline=time.monotonic()+remaining,ready=False)
        self.agent.runtime.operation_id=None
        self.agent.activate()
        self.agent.view['ready']=self.agent.runtime.healthy() and self.agent.grant_valid()
        self.agent.next_poll_seconds=5

    def maintain(self):
        r=self.agent.runtime
        with self.agent.grant_mutex,self.a.mutex:
            rows=self.a.lock.read();previous,active=[v['value'] for v in rows]
            if not previous:return
            policy=previous.get('policy',{})
            if previous.get('released') or previous.get('localBlocked') or policy.get('blocked',True):return
            own=previous['owner']==self.agent.node and previous['session']==self.agent.session
            if own and active:
                authority=previous['authority']
                if authority=='cloudflare' and (self.failed_at is None or self.a.clock()-self.failed_at<self.CF_GRACE):return
                if authority not in ('cloudflare','fallback','handoff'):return
                if r.mode() not in ('active','lease-paused','fenced') or r.read('immich-upgrade-lock.json',''):return
                try:state=r.streaming.state()
                except Exception:return
                if state['inRecovery']:return
                if not r.healthy() and self.a.clock()>self.warming_until:return
                started=self.a.clock()
                result=self.a.lock.claim('fallback' if authority=='cloudflare' else authority,
                    self.agent.node,previous['epoch'],self.agent.session,self.TTL,
                    transition_from=previous if authority=='cloudflare' else None)
                if not result['admitted']:raise RuntimeError('Fallback continuation denied: '+result['reason'])
                self.a.accept(result,started,self.TTL-MARGIN);self.a.state='fallback-active'
                self.publish_view();return
            if active:
                self.absent_since=None;self.absent_identity=None
                if active['authority'] in ('fallback','handoff') and active['owner']!=self.agent.node:
                    # Discard any old CF writer view immediately; preserve the
                    # standby database and forbid old network jobs via guard.
                    self.a.clear()
                    self.agent.view=dict(role='standby',owner=active['owner'],epoch=active['epoch'],deadline=0,ready=False)
                    self.agent.fence()
                return
            # No unknown-failure shortcut: native expiry, additional sustained
            # absence, valid WAL lineage and idle/error-free media are required.
            if not policy.get('auto',False) or policy.get('excluded',{}).get(self.agent.node,True) or own:return
            identity=(previous['owner'],previous['epoch'],previous['session'])
            if identity!=self.absent_identity:
                self.absent_identity=identity;self.absent_since=self.a.clock();return
            if self.a.clock()-self.absent_since<self.FAILURE_SECONDS:return
            if not self.agent.enforcing() or not r.can_promote(previous['epoch'],previous['owner']):return
            from ha_runtime import Syncthing
            sync=Syncthing(r.config,r.endpoint)
            folder=sync.folder('immich-library');media=sync.status('immich-library')
            if folder.get('type')!='receiveonly' or media.get('state')!='idle' or media.get('needTotalItems',1)!=0 or media.get('needBytes',1)!=0:return
            started=self.a.clock()
            result=self.a.lock.claim('fallback',self.agent.node,previous['epoch']+1,self.agent.session,
                                     self.TTL,transition_from=previous)
            if not result['admitted']:return
            self.a.accept(result,started,self.TTL-MARGIN);self.a.state='fallback-promoting'
            self.warming_until=self.a.clock()+120
            self.publish_view()

    def response(self,response):
        """Return True only when normal CF grants/commands may be applied."""
        self.failed_at=None
        policy=control_policy(self.a.config,response)
        with self.agent.grant_mutex,self.a.mutex:
            rows=self.a.lock.read();previous,active=[v['value'] for v in rows]
            if not previous:return True
            if previous['authority']=='cloudflare':
                if response.get('owner')==previous['owner'] and response.get('epoch')==previous['epoch']:
                    self.a.lock.update_policy(previous['owner'],previous['epoch'],policy)
                return response.get('role')!='quorum-wait'
            if response['role']=='active' and response['owner']==self.agent.node and active and active['authority']=='handoff' and response['epoch']==active['epoch'] and response['quorum'].get('ack')==active.get('handoffNonce'):
                return True
            challenge=response['quorum'].get('challenge')
            if active and active['owner']==self.agent.node and active['session']==self.agent.session and challenge:
                if challenge['owner']!=self.agent.node or challenge['session']!=self.agent.session or type(challenge.get('targetEpoch')) is not int or challenge['targetEpoch']<active['epoch'] or not re.fullmatch('[a-f0-9]{32}',challenge.get('nonce','')):raise RuntimeError('Invalid CF handback challenge')
                started=self.a.clock()
                result=self.a.lock.claim('handoff',self.agent.node,challenge['targetEpoch'],self.agent.session,
                    self.TTL,transition_from=previous,handoff_nonce=challenge['nonce'])
                if result['admitted']:
                    self.a.accept(result,started,self.TTL-MARGIN);self.a.state='reconciling'
                    self.publish_view()
            self.agent.next_poll_seconds=5
            return False
