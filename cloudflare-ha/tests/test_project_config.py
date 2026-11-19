import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from project_config import load_project, worker_variables, node_properties

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("render_wrangler", ROOT / "tools/render-wrangler.py")
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


class ProjectConfigTests(unittest.TestCase):
    def setUp(self):
        self.project = load_project(ROOT / "project.example.json")

    def load(self, settings):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "project.json"
            path.write_text(json.dumps(settings), encoding="utf-8")
            return load_project(path)

    def test_domain_change_propagates_to_worker_and_both_nodes(self):
        changed = copy.deepcopy(self.project)
        changed.update(publicHost="photos.example.net", adminHost="control.example.net")
        changed["origins"] = {"primary": "https://node-a.example.net", "backup": "https://node-b.example.net"}
        changed["access"] = {"teamDomain": "example.cloudflareaccess.com", "audience": "new-audience", "emails": ["admin@example.net"]}
        settings = self.load(changed)
        variables = worker_variables(settings)
        self.assertEqual(variables["PUBLIC_HOST"], "photos.example.net")
        self.assertEqual(variables["ADMIN_HOST"], "control.example.net")
        self.assertEqual(variables["BACKUP_ORIGIN"], "https://node-b.example.net")
        self.assertEqual(json.loads(variables["ADMIN_EMAILS"]), ["admin@example.net"])
        self.assertEqual(renderer.render(settings)["vars"], variables)
        for node in ("primary", "backup"):
            self.assertEqual(node_properties(settings, node)["public_url"], "https://photos.example.net")
            self.assertEqual(node_properties(settings, node)["admin_url"], "https://control.example.net")
        # Domain changes preserve the deployed Worker and Durable Object binding.
        self.assertEqual(renderer.render(settings)["name"], self.project["workerName"])
        self.assertEqual(renderer.render(settings)["durable_objects"], renderer.render(self.project)["durable_objects"])

    def test_rejects_origin_pointing_back_at_public_domain(self):
        self.project["origins"]["primary"] = "https://" + self.project["publicHost"]
        with self.assertRaises(ValueError):
            self.load(self.project)

    def test_rejects_invalid_origin_and_access_domains(self):
        for value in ("http://node.example.net", "https://node.example.net/path", "https://user:password@node.example.net", "https://node.example.net:443"):
            with self.subTest(value=value):
                settings = copy.deepcopy(self.project)
                settings["origins"]["primary"] = value
                with self.assertRaises(ValueError):
                    self.load(settings)
        self.project["access"]["teamDomain"] = "untrusted.example.net"
        with self.assertRaises(ValueError):
            self.load(self.project)


if __name__ == "__main__":
    unittest.main()
