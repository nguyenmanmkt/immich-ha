import importlib.util
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).parents[1]))
import local_gateway as g


class LocalGatewayTests(unittest.TestCase):
    def test_template_keeps_gate_for_every_proxy_request_without_client_epoch(self):
        template=(Path(__file__).parents[1]/'node/immich-ha-local.nginx.conf').read_text()
        value=g.render(template,8090,'test_origin_credential_'+'a'*32)
        self.assertIn('auth_request /_immich_ha_local_authorize;',value)
        self.assertIn('9081/authorize-local',value)
        self.assertIn('internal;',value)
        self.assertIn('deny all;',value)
        self.assertIn('satisfy all;',value)
        self.assertIn('proxy_request_buffering off;',value)
        self.assertIn('proxy_set_header Upgrade $http_upgrade;',value)
        self.assertNotIn('__PORT__',value)
        self.assertNotIn('__ORIGIN_TOKEN__',value)
        self.assertNotIn('$http_x_immich_ha_token',value)
        self.assertNotIn('$http_x_immich_ha_epoch',value)

    def test_renderer_rejects_reserved_ports_or_nginx_secret_injection(self):
        template=(Path(__file__).parents[1]/'node/immich-ha-local.nginx.conf').read_text()
        for port in (80,2283,2379,5433,8080,9081,65536,True):
            with self.assertRaises(ValueError):g.render(template,port,'x'*64)
        for token in ('short','x'*40+'"; proxy_pass http://evil;',None):
            with self.assertRaises(ValueError):g.render(template,8090,token)

    def test_embedded_gateway_payloads_compile_without_ssh_or_linux(self):
        for name in ('PREFLIGHT','INSTALL','CHECK'):compile(getattr(g,name),name,'exec')
        path=Path(__file__).parents[1]/'tools/install-local-gateway.py'
        spec=importlib.util.spec_from_file_location('local_gateway_install_test',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertTrue(callable(module.main))
