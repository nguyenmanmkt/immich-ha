import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import quorum_setup as q


class QuorumPlanTests(unittest.TestCase):
    def fixture(self):
        inventory={n:{'uid':0,'os':'debian','osVersion':'13','node':None if n=='witness' else n,
            'hostname':'immich-ha-witness' if n=='witness' else 'immich',
            'tailscaleIp':'100.64.0.'+str(i+1),'tun':True,'memoryBytes':1024**3,
            'rootFreeBytes':4*1024**3,'quorumPortsBusy':[],'agent':None if n=='witness' else 'active',
            'postgres':None if n=='witness' else {'inRecovery':n=='primary','systemId':'123','receiverConnected':True}}
            for i,n in enumerate(q.NODES)}
        links={n:{other:{'reachable':True} for other in q.NODES if other!=n} for n in q.NODES}
        return inventory,links

    def test_valid_three_node_plan_never_enables_grants_and_preserves_identity(self):
        inventory,links=self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'topology.json'
            first,owner=q.prepare_plan(inventory,links,path);second,_=q.prepare_plan(inventory,links,path)
            self.assertEqual(owner,'backup');self.assertEqual(first,second)
            self.assertEqual(first['requiredVotes'],2)
            self.assertTrue(first['cloudflareRemainsPrimary'])
            self.assertFalse(first['grantIntegrationEnabled']);self.assertFalse(first['funnelEnabled'])

    def test_changed_topology_cannot_restage_existing_cluster_identity(self):
        inventory,links=self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'topology.json';q.prepare_plan(inventory,links,path)
            original=path.read_bytes();inventory['witness']['tailscaleIp']='100.64.0.9'
            with self.assertRaisesRegex(ValueError,'topology differs'):q.prepare_plan(inventory,links,path)
            self.assertEqual(path.read_bytes(),original)

    def test_wrong_identity_duplicate_ip_busy_ports_or_missing_tun_rejected(self):
        for node,key,value in [('backup','node','primary'),('witness','hostname','proxmox3'),
            ('backup','tailscaleIp','100.64.0.1'),('witness','quorumPortsBusy',[2379]),('witness','tun',False)]:
            with self.subTest(key=key):
                inventory,links=self.fixture();inventory[node][key]=value
                with self.assertRaises(ValueError):q.validate_inventory(inventory,links)

    def test_unreachable_peer_or_multiple_writable_databases_block_plan(self):
        inventory,links=self.fixture();links['witness']['backup']['reachable']=False
        with self.assertRaisesRegex(ValueError,'reach all'):q.validate_inventory(inventory,links)
        inventory,links=self.fixture();inventory['primary']['postgres']['inRecovery']=False
        with self.assertRaisesRegex(ValueError,'exactly one'):q.validate_inventory(inventory,links)
        inventory,links=self.fixture();inventory['primary']['postgres']['systemId']='456'
        with self.assertRaisesRegex(ValueError,'same data lineage'):q.validate_inventory(inventory,links)

    def test_payloads_compile_and_common_key_does_not_borrow_another_password(self):
        for source in [q.INSPECT,q.PING]:compile(source,'quorum SSH payload','exec')
        with tempfile.TemporaryDirectory() as directory:
            key=Path(directory)/'key';key.touch()
            env={'SSH_KEY_PATH':str(key),'SSH_KEY_PASSPHRASE':'common',
                 'SSH_PRIMARY_PASSWORD':'different-password','SSH_COORDINATOR_HOST':'100.64.0.3'}
            settings=q.key_settings('coordinator',env,{'nodes':{}})
            self.assertEqual(settings['key_filename'],str(key.resolve()))
            self.assertEqual(settings['passphrase'],'common');self.assertIsNone(settings['password'])


if __name__=='__main__':unittest.main()
