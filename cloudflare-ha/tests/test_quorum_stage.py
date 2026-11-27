import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
import uuid
import ssl
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.x509.oid import ExtendedKeyUsageOID
import quorum_stage as s


class QuorumStageTests(unittest.TestCase):
    def strict_handshake(self,pki):
        server=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);server.load_cert_chain(str(pki/'witness-server.crt'),str(pki/'witness-server.key'))
        server.load_verify_locations(cafile=str(pki/'client-ca.crt'));server.verify_mode=ssl.CERT_REQUIRED;server.verify_flags|=ssl.VERIFY_X509_STRICT
        client=ssl.create_default_context(cafile=str(pki/'client-ca.crt'));client.verify_flags|=ssl.VERIFY_X509_STRICT
        client.load_cert_chain(str(pki/'primary-probe.crt'),str(pki/'primary-probe.key'))
        ci,co,si,so=[ssl.MemoryBIO() for _ in range(4)]
        endpoints=[client.wrap_bio(ci,co,server_hostname='100.64.0.3'),server.wrap_bio(si,so,server_side=True)];done=[False,False]
        for _ in range(30):
            for index,endpoint in enumerate(endpoints):
                if not done[index]:
                    try:endpoint.do_handshake();done[index]=True
                    except ssl.SSLWantReadError:pass
            for source,target in ((co,si),(so,ci)):
                data=source.read()
                if data:target.write(data)
            if all(done):return
        self.fail('Strict mutual TLS handshake did not finish')

    def plan(self):
        return {'version':1,'engine':'etcd','clusterId':str(uuid.uuid4()),
            'members':{n:{'tailscaleIp':'100.64.0.'+str(i+1),'clientPort':2379,'peerPort':2380} for i,n in enumerate(s.NODES)},
            'grantIntegrationEnabled':False,'funnelEnabled':False}

    def test_isolated_configuration_has_mtls_both_ways_and_only_private_listeners(self):
        plan=self.plan();s.validate_plan(plan)
        cfg=s.configuration(plan,'witness')
        self.assertEqual(cfg['listen-client-urls'],'https://100.64.0.3:2379')
        self.assertEqual(cfg['listen-peer-urls'],'https://100.64.0.3:2380')
        for kind in ('client','peer'):
            security=cfg[kind+'-transport-security'];self.assertTrue(security['client-cert-auth'])
            self.assertIn(kind+'-ca.crt',security['trusted-ca-file'])
        self.assertEqual(cfg['client-transport-security']['client-cert-file'],s.BASE+'/probe.crt')
        self.assertEqual(cfg['client-transport-security']['client-key-file'],s.BASE+'/probe.key')
        self.assertEqual(cfg['initial-cluster'].count('https://'),3)
        self.assertEqual(cfg['election-timeout'],10*cfg['heartbeat-interval'])
        self.assertNotIn('0.0.0.0',json.dumps(cfg))

    def test_active_or_public_or_duplicate_topology_is_rejected(self):
        for change in ['grant','funnel','public','duplicate','identity']:
            plan=self.plan()
            if change=='grant':plan['grantIntegrationEnabled']=True
            elif change=='funnel':plan['funnelEnabled']=True
            elif change=='public':plan['members']['witness']['tailscaleIp']='8.8.8.8'
            elif change=='duplicate':plan['members']['witness']['tailscaleIp']='100.64.0.1'
            else:plan['clusterId']='../other'
            with self.assertRaises(ValueError):s.validate_plan(plan)

    def test_pki_reuse_never_rotates_existing_cluster_identity_or_keys(self):
        plan=self.plan()
        with tempfile.TemporaryDirectory() as directory:
            pki=s.certificates(plan,Path(directory)/'pki')
            original={p.name:p.read_bytes() for p in pki.iterdir()}
            self.assertEqual(s.certificates(plan,pki),pki)
            self.assertEqual(original,{p.name:p.read_bytes() for p in pki.iterdir()})
            other=copy.deepcopy(plan);other['clusterId']=str(uuid.uuid4())
            with self.assertRaisesRegex(RuntimeError,'identity differs'):s.certificates(other,pki)
            (pki/'witness-peer.key').unlink()
            with self.assertRaisesRegex(RuntimeError,'Incomplete'):s.certificates(plan,pki)

    def test_peer_and_client_ca_are_separate_and_probes_are_not_peer_certificates(self):
        with tempfile.TemporaryDirectory() as directory:
            pki=s.certificates(self.plan(),Path(directory)/'pki')
            peer=x509.load_pem_x509_certificate((pki/'witness-peer.crt').read_bytes())
            server=x509.load_pem_x509_certificate((pki/'witness-server.crt').read_bytes())
            probe=x509.load_pem_x509_certificate((pki/'witness-probe.crt').read_bytes())
            self.assertNotEqual(peer.issuer,probe.issuer)
            self.assertEqual(server.issuer,probe.issuer)
            self.assertIn(ExtendedKeyUsageOID.CLIENT_AUTH,probe.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)
            self.assertNotIn(ExtendedKeyUsageOID.SERVER_AUTH,probe.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)
            self.assertEqual(str(server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)[0]),'100.64.0.3')
            self.strict_handshake(pki)

    def test_explicit_aki_repair_preserves_ca_keys_leaf_keys_peer_certs_and_validity(self):
        with tempfile.TemporaryDirectory() as directory:
            plan=self.plan();pki=s.certificates(plan,Path(directory)/'pki')
            ca_key=serialization.load_pem_private_key((pki/'client-ca.key').read_bytes(),None)
            preserved={p.name:p.read_bytes() for p in pki.iterdir() if p.suffix=='.key' or '-ca.crt' in p.name or '-peer.crt' in p.name}
            validity={};manifest=json.loads((pki/'identity.json').read_text())
            for node in s.NODES:
                for kind in ('server','probe'):
                    path=pki/f'{node}-{kind}.crt';old=x509.load_pem_x509_certificate(path.read_bytes())
                    validity[path.name]=old.not_valid_after_utc
                    builder=x509.CertificateBuilder().subject_name(old.subject).issuer_name(old.issuer).public_key(old.public_key()).serial_number(old.serial_number).not_valid_before(old.not_valid_before_utc).not_valid_after(old.not_valid_after_utc)
                    for extension in old.extensions:
                        if not isinstance(extension.value,x509.AuthorityKeyIdentifier):builder=builder.add_extension(extension.value,extension.critical)
                    path.write_bytes(builder.sign(ca_key,hashes.SHA256()).public_bytes(serialization.Encoding.PEM));manifest['hashes'][path.name]=s.digest(path)
            (pki/'identity.json').write_text(json.dumps(manifest))
            with self.assertRaises(ssl.SSLCertVerificationError):self.strict_handshake(pki)
            changes=s.add_client_key_identifiers(plan,pki);self.assertEqual(len(changes),6)
            self.strict_handshake(pki)
            for name,value in preserved.items():self.assertEqual((pki/name).read_bytes(),value)
            for name,value in validity.items():self.assertEqual(x509.load_pem_x509_certificate((pki/name).read_bytes()).not_valid_after_utc,value)
            self.assertEqual(s.add_client_key_identifiers(plan,pki),{})

    def test_upload_package_excludes_ca_private_keys_other_members_and_immich_files(self):
        with tempfile.TemporaryDirectory() as directory:
            plan=self.plan();root=Path(directory);pki=s.certificates(plan,root/'pki')
            executables={}
            for name in ('etcd','etcdctl','etcdutl'):
                executables[name]=root/name;executables[name].write_bytes(name.encode())
            content,payload=s.package(plan,'witness',pki,executables)
            self.assertNotIn('peer-ca.key',content);self.assertNotIn('client-ca.key',content)
            self.assertEqual(content['peer.key'],(pki/'witness-peer.key').read_bytes())
            self.assertFalse(payload['manifest']['grantIntegrationEnabled'])
            for path in payload['files']:
                self.assertTrue(path.startswith(('/opt/immich-ha-quorum/','/etc/immich-ha-quorum/')) or path=='/etc/systemd/system/immich-ha-quorum.service')
            self.assertEqual(set(payload['installedHashes']),set(payload['files']))

    def test_shell_free_remote_payloads_compile_and_only_check_keys_are_mutated(self):
        for name in ('PREPARE','START','CHECK','SMOKE','TLS_COMPAT'):compile(getattr(s,name),name,'exec')
        self.assertIn("'/checks/'",s.SMOKE)
        self.assertNotIn('pg_ctl',s.PREPARE+s.START+s.CHECK+s.SMOKE)
        self.assertNotIn('immich-ha-app.service',s.service())
        self.assertIn('MemoryMax=256M',s.service())

    def test_missing_endpoint_mixed_cluster_wrong_members_or_learners_do_not_pass(self):
        plan=self.plan();endpoints=[f'https://100.64.0.{i+1}:2379' for i in range(3)]
        check={'statuses':[{'Endpoint':e,'Status':{'header':{'cluster_id':999},'leader':1}} for e in endpoints],
            'health':[{'endpoint':e,'health':True} for e in endpoints],
            'members':{'members':[{'ID':i+1,'name':n,'clientURLs':[endpoints[i]]} for i,n in enumerate(s.NODES)]},
            'unauthenticatedRejected':True}
        s.validate_check(plan,check)
        majority=copy.deepcopy(check);majority['health']=majority['health'][:2];majority['statuses']=majority['statuses'][:2]
        s.validate_check(plan,majority,('primary','backup'))
        with self.assertRaises(ValueError):s.validate_check(plan,majority,('primary',))
        for mode in ('missing','cluster','learner','identity','certificate'):
            value=copy.deepcopy(check)
            if mode=='missing':value['health'].pop()
            elif mode=='cluster':value['statuses'][1]['Status']['header']['cluster_id']=444
            elif mode=='learner':value['members']['members'][0]['isLearner']=True
            elif mode=='identity':value['members']['members'][0]['name']='other'
            else:value['unauthenticatedRejected']=False
            with self.assertRaises(RuntimeError):s.validate_check(plan,value)


if __name__=='__main__':unittest.main()
