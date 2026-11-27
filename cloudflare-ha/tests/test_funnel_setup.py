import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock,patch

import funnel_setup as f


class FunnelTests(unittest.TestCase):
    name='node.example-tail.ts.net'

    def configured(self):
        return {'TCP':{'443':{'HTTPS':True}},'Web':{self.name+':443':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:8090'}}}},'AllowFunnel':{self.name+':443':True}}

    def test_new_route_and_idempotent_existing_route_are_valid(self):
        self.assertEqual(f.validate_existing({},self.name),'http://127.0.0.1:8090')
        f.validate_after({},self.configured(),self.name)
        f.validate_after(self.configured(),self.configured(),self.name)

    def test_unrelated_services_must_remain_unchanged(self):
        before={'TCP':{'8443':{'HTTPS':True}},'Web':{'other.ts.net:8443':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:9000'}}}}}
        after=copy.deepcopy(before);configured=self.configured()
        for key,value in configured.items():after.setdefault(key,{}).update(value)
        f.validate_after(before,after,self.name)
        after['TCP']['8443']={'HTTPS':False}
        with self.assertRaises(ValueError):f.validate_after(before,after,self.name)

    def test_refuses_existing_different_targets_aliases_and_direct_app_exposure(self):
        for port in (2283,8080,9081,5433):
            value=self.configured();value['Web'][self.name+':443']['Handlers']['/']['Proxy']='http://127.0.0.1:'+str(port)
            with self.assertRaises(ValueError):f.validate_existing(value,self.name)
        value=self.configured();value['Web']['other.ts.net:443']={'Handlers':{'/':{'Proxy':'http://127.0.0.1:9000'}}}
        with self.assertRaises(ValueError):f.validate_existing(value,self.name)
        for value in ({'TCP':{'443':{'TCPForward':'127.0.0.1:22'}}},{'TCP':{'443':{'HTTPS':True}}},{'Foreground':{'session':{}}}):
            with self.assertRaises(ValueError):f.validate_existing(value,self.name)

    def test_public_dns_check_rejects_magicdns_private_addresses(self):
        with patch.object(f.urllib.request,'urlopen') as urlopen,patch.object(f.json,'load',return_value={'Answer':[{'type':1,'data':'100.64.0.1'}]}):
            with self.assertRaises(RuntimeError):f.public_addresses(self.name)

    def test_configured_route_requires_actual_public_allow_flag(self):
        value=self.configured();value['AllowFunnel'][self.name+':443']=False
        with self.assertRaises(ValueError):f.validate_after({},value,self.name)
        with self.assertRaises(ValueError):f.validate_existing({},'not-a-tailscale-domain.example')
        with self.assertRaises(ValueError):f.validate_existing({},self.name,8090)

    def test_payloads_compile_and_importing_entrypoint_is_inert(self):
        for name in ('PREFLIGHT','APPLY'):compile(getattr(f,name),name,'exec')
        path=Path(__file__).parents[1]/'tools/install-funnel.py'
        spec=importlib.util.spec_from_file_location('funnel_install_test',path)
        module=importlib.util.module_from_spec(spec)
        with patch('deployment.control') as control:
            spec.loader.exec_module(module);self.assertTrue(callable(module.main));control.assert_not_called()
