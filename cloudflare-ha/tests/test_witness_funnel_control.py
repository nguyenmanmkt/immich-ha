import ast
import importlib.util
import json
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('witness_funnel_control_test',ROOT/'node/witness_funnel.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class FunnelControlTests(unittest.TestCase):
    config={'dnsName':'witness.example.ts.net','port':443,'gatewayPort':8090}

    def route(self):
        return {'TCP':{'443':{'HTTPS':True},'8443':{'HTTPS':True}},
          'Web':{'witness.example.ts.net:443':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:8090'}}},'other.example.ts.net:8443':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:9000'}}}},
          'AllowFunnel':{'witness.example.ts.net:443':True,'other.example.ts.net:8443':True}}

    def test_off_removes_only_managed_port_and_on_preserves_other_routes(self):
        settings=self.route();commands=[]
        def run(args,**kwargs):
            commands.append(args)
            if args[1:]==['serve','status','--json']:return json.dumps(settings)
            if args[-1]=='off':
                settings['TCP'].pop('443');settings['Web'].pop('witness.example.ts.net:443');settings['AllowFunnel'].pop('witness.example.ts.net:443')
            else:
                good=self.route()
                for key,item in [('TCP','443'),('Web','witness.example.ts.net:443'),('AllowFunnel','witness.example.ts.net:443')]:settings[key][item]=good[key][item]
            return ''
        m.apply(self.config,False,run)
        self.assertFalse(m.current(self.config,settings))
        self.assertEqual(commands[1],['tailscale','funnel','--https=443','off'])
        self.assertIn('other.example.ts.net:8443',settings['Web'])
        m.apply(self.config,True,run)
        self.assertEqual(settings,self.route())
        self.assertFalse(any('reset' in command for command in commands))

    def test_unmanaged_root_and_incomplete_enabled_route_are_rejected(self):
        settings=self.route();settings['Web']['witness.example.ts.net:443']['Handlers']['/']['Proxy']='http://127.0.0.1:9999'
        with self.assertRaises(ValueError):m.apply(self.config,False,lambda *a,**kw:json.dumps(settings))
        with self.assertRaises(ValueError):m.current(self.config,{'AllowFunnel':{'witness.example.ts.net:443':True}})

    def test_installer_payload_compiles_without_running_operational_code(self):
        tree=ast.parse((ROOT/'tools/install-funnel-control.py').read_text(encoding='utf-8'))
        payload=next(n.value.value for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='INSTALL' for t in n.targets))
        compile(payload,'witness control installer','exec')
