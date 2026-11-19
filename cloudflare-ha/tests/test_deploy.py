import importlib.util
from email import policy
from email.parser import BytesParser
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("ha_deploy", Path(__file__).parents[1] / "deploy.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class UploadTests(unittest.TestCase):
    def test_multipart_keeps_both_es_modules_and_json_metadata(self):
        body = module.multipart({"main_module": "worker.mjs"},
                                {"worker.mjs": b"import './controller.mjs';", "controller.mjs": b"export {};"},
                                "test-boundary")
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: multipart/form-data; boundary=test-boundary\r\n\r\n" + body)
        parts = {part.get_param("name", header="Content-Disposition"): part for part in message.iter_parts()}
        self.assertEqual(set(parts), {"metadata", "worker.mjs", "controller.mjs"})
        self.assertEqual(parts["worker.mjs"].get_content_type(), "application/javascript+module")
        self.assertIn(b'"main_module": "worker.mjs"', parts["metadata"].get_payload(decode=True))
        self.assertEqual(parts["controller.mjs"].get_payload(decode=True), b"export {};")


if __name__ == "__main__":
    unittest.main()
