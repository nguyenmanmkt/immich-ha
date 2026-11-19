import unittest
from service_credentials import validate, headers, ID_KEY, SECRET_KEY


class CredentialsTests(unittest.TestCase):
    def test_legacy_optional_and_partial_rejected(self):
        self.assertFalse(validate({"ADMIN_TOKEN": "existing-key"}))
        for value in ({ID_KEY: "gateway.access"}, {SECRET_KEY: "s" * 48}):
            with self.assertRaises(ValueError):
                validate(value)

    def test_accepts_both_secret_formats_and_preserves_input(self):
        for secret in ("a" * 64, "cfast_" + "a" * 48):
            values = {ID_KEY: "gateway.access", SECRET_KEY: secret, "ADMIN_TOKEN": "existing-key"}
            self.assertTrue(validate(values))
            self.assertEqual(headers(values)["CF-Access-Client-Secret"], secret)
            self.assertEqual(values["ADMIN_TOKEN"], "existing-key")

    def test_rejects_header_injection_without_echoing_secret(self):
        secret = "s" * 48 + "\r\nInjected: value"
        with self.assertRaises(ValueError) as error:
            headers({ID_KEY: "gateway.access", SECRET_KEY: secret})
        self.assertNotIn(secret, str(error.exception))
